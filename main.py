#!/usr/bin/env python3
"""
CNPJ Data Pipeline - Download and process Brazilian company data from Receita Federal.

Usage:
    python main.py                    # Process latest month once
    python main.py --list             # List available months
    python main.py --month 2024-11    # Process specific month
    python main.py --month 2024-11 --force   # Force PostgreSQL re-processing
    docker compose up                 # Run once with Docker
"""

import argparse
import logging
import shlex
import subprocess
import sys
from collections.abc import Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from tqdm import tqdm

from config import Config
from downloader import Downloader
from file_types import DEPENDENCY_GROUPS, FILE_MAPPINGS, FILTERED_DEPENDENCY_GROUPS, get_file_type, get_zip_file_type
from file_types import get_file_priority as get_file_priority
from file_types import group_files_by_dependency as group_files_by_dependency
from filtro import ContadoresFiltro, FiltroProspeccao
from processor import process_file

if TYPE_CHECKING:
    from database import Database
    from parquet_writer import ParquetWriter


logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="CNPJ Data Pipeline - Download and process Brazilian company data")
    parser.add_argument("--list", "-l", action="store_true", help="List available months without processing")
    parser.add_argument("--month", "-m", type=str, help="Specific month to process (format: YYYY-MM, e.g., 2024-11)")
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force PostgreSQL re-processing even if already processed",
    )
    return parser.parse_args()


def pg_worker(
    zip_filename: str,
    directory: str,
    downloader: Downloader,
    cfg: Config,
    pre_truncated: set[str] | None = None,
    filtro: FiltroProspeccao | None = None,
    contadores: ContadoresFiltro | None = None,
) -> None:
    """Worker: download, process, and load one file to PostgreSQL."""
    from database import Database

    db = Database(cfg.database_url, pre_truncated=pre_truncated, retry_attempts=cfg.retry_attempts)
    try:
        paths = downloader.download_file(directory, zip_filename)
        load_postgres_archive(paths, zip_filename, directory, db, cfg, filtro=filtro, contadores=contadores)
    except Exception as e:
        logger.error(f"Error processing {zip_filename}: {e}")
        raise
    finally:
        db.disconnect()


def parquet_worker(
    zip_filename: str,
    directory: str,
    downloader: Downloader,
    parquet: "ParquetWriter",
    cfg: Config,
    filtro: FiltroProspeccao | None = None,
    contadores: ContadoresFiltro | None = None,
) -> None:
    """Worker: download, process, and write one file to Parquet."""
    for csv_path in downloader.download_file(directory, zip_filename):
        try:
            write_parquet_csv(csv_path, parquet, cfg, filtro=filtro, contadores=contadores)
        except Exception as e:
            logger.error(f"Error: {csv_path.name}: {e}")
            raise


def load_postgres_archive(
    paths: list[Path],
    zip_filename: str,
    directory: str,
    db: "Database",
    cfg: Config,
    progress: Callable[[int], None] | None = None,
    filtro: FiltroProspeccao | None = None,
    contadores: ContadoresFiltro | None = None,
) -> None:
    for path in paths:
        load_postgres_csv(path, db, cfg, progress, filtro=filtro, contadores=contadores)
    db.mark_processed(directory, zip_filename)
    if not cfg.keep_files:
        for path in paths:
            path.unlink(missing_ok=True)


def load_postgres_csv(
    csv_path: Path,
    db: "Database",
    cfg: Config,
    progress: Callable[[int], None] | None = None,
    filtro: FiltroProspeccao | None = None,
    contadores: ContadoresFiltro | None = None,
) -> None:
    rows = 0
    read = 0
    file_type = get_file_type(csv_path.name)
    load = db.bulk_insert if cfg.loading_strategy == "replace" else db.bulk_upsert
    for batch, table_name, columns in process_file(csv_path, cfg.batch_size):
        read += len(batch)
        kept = filtro.aplicar(batch, file_type) if filtro is not None else batch
        if contadores is not None:
            contadores.registrar(table_name, len(batch), len(kept))
        if not kept.is_empty():
            load(kept, table_name, columns)
        rows += len(kept)
        if progress is not None:
            progress(rows)

    # "Empty" means the SOURCE had no rows; a filter dropping every row is normal.
    if read == 0:
        raise ValueError(f"Empty source: {csv_path.name}")

    logger.info(f"  {csv_path.name}: {rows:,} rows" + (f" (of {read:,} read)" if filtro is not None else ""))


def write_parquet_csv(
    csv_path: Path,
    parquet: "ParquetWriter",
    cfg: Config,
    filtro: FiltroProspeccao | None = None,
    contadores: ContadoresFiltro | None = None,
) -> None:
    rows = 0
    read = 0
    file_type = get_file_type(csv_path.name)
    for batch, table_name, _columns in process_file(csv_path, cfg.batch_size, typed=cfg.parquet_typed_output):
        read += len(batch)
        kept = filtro.aplicar(batch, file_type) if filtro is not None else batch
        if contadores is not None:
            contadores.registrar(table_name, len(batch), len(kept))
        if not kept.is_empty():
            parquet.write_batch(kept, table_name)
        rows += len(kept)
        if filtro is None and rows % 1_000_000 == 0:
            logger.info(f"  {csv_path.name}: {rows:,} rows")

    # "Empty" means the SOURCE had no rows; a filter dropping every row is normal.
    if read == 0:
        raise ValueError(f"Empty source: {csv_path.name}")

    logger.info(f"  {csv_path.name}: {rows:,} rows" + (f" (of {read:,} read)" if filtro is not None else ""))

    if csv_path.exists() and not cfg.keep_files:
        csv_path.unlink()


def wait_for_workers(futures: dict[Future[None], str], failure_message: str) -> None:
    failed = False
    with tqdm(total=len(futures), desc="Processing", unit="file") as pbar:
        for future in as_completed(futures):
            pbar.set_postfix_str(futures[future][:30])
            try:
                future.result()
            except Exception:
                failed = True
            pbar.update(1)
    if failed:
        raise RuntimeError(failure_message)


def prepare_filter_for_group(
    filtro: FiltroProspeccao,
    group_files: list[str],
    load_municipios: Callable[[], dict[str, str]],
    load_cnpjs_basicos: Callable[[], set[str]],
) -> None:
    """Between dependency groups: resolve municipality names (after the reference tables) and load
    the cnpj_basico set that survived the estabelecimentos filter (before empresas/dados_simples).

    The set is read back from the output (DB table or Parquet file) instead of collected in memory,
    so a run resumed after estabelecimentos finished still gets it.
    """
    types = {get_zip_file_type(f) for f in group_files}
    if "ESTABELE" in types and filtro.municipios_config and filtro.municipios is None:
        filtro.resolver_municipios(load_municipios() if filtro.precisa_tabela_municipios else None)
    if types & {"EMPRECSV", "SIMPLESCSV"}:
        filtro.cnpjs_basicos = load_cnpjs_basicos()
        logger.info(f"Filtro: {len(filtro.cnpjs_basicos):,} CNPJs básicos para empresas/dados_simples")


def run_post_file_command(path: Path, command: list[str]) -> None:
    """Run an idempotent hook; resuming an export can repeat it."""
    if command:
        logger.info(f"  Running post-file command for {path.name}")
        subprocess.run([*command, str(path)], check=True)


def _parquet_loaders(output_dir: str) -> tuple[Callable[[], dict[str, str]], Callable[[], set[str]]]:
    import polars as pl

    def municipios() -> dict[str, str]:
        path = Path(output_dir) / "municipios.parquet"
        if not path.exists():
            return {}
        df = pl.read_parquet(path, columns=["codigo", "descricao"])
        return dict(zip(df["codigo"].to_list(), df["descricao"].to_list(), strict=True))

    def cnpjs() -> set[str]:
        path = Path(output_dir) / "estabelecimentos.parquet"
        if not path.exists():  # filter kept no estabelecimento: nothing was written
            return set()
        return set(pl.read_parquet(path, columns=["cnpj_basico"])["cnpj_basico"].to_list())

    return municipios, cnpjs


def _postgres_loaders(db: "Database") -> tuple[Callable[[], dict[str, str]], Callable[[], set[str]]]:
    def municipios() -> dict[str, str]:
        return {codigo: descricao for codigo, descricao in db.fetch_rows("SELECT codigo, descricao FROM municipios")}

    def cnpjs() -> set[str]:
        return {row[0] for row in db.fetch_rows("SELECT DISTINCT cnpj_basico FROM estabelecimentos")}

    return municipios, cnpjs


def run_parquet(
    pending_files: list[str],
    directory: str,
    downloader: Downloader,
    parquet: "ParquetWriter",
    config: Config,
    filtro: FiltroProspeccao | None = None,
    contadores: ContadoresFiltro | None = None,
) -> None:
    command = shlex.split(config.post_file_command)
    existing_tables: set[str] = set()
    # Validate all existing tables before downloading or publishing any new ones.
    for filename in pending_files:
        file_type = get_zip_file_type(filename)
        if file_type and file_type in FILE_MAPPINGS:
            table = FILE_MAPPINGS[file_type]
            if (Path(config.parquet_output_dir) / f"{table}.parquet").exists():
                if table not in parquet.stats:
                    parquet.include_existing_table(table)
                existing_tables.add(table)

    for table in sorted(existing_tables):
        run_post_file_command(Path(config.parquet_output_dir) / f"{table}.parquet", command)

    file_groups = group_files_by_dependency(
        pending_files, FILTERED_DEPENDENCY_GROUPS if filtro is not None else DEPENDENCY_GROUPS
    )
    workers = config.process_workers
    load_municipios, load_cnpjs = _parquet_loaders(config.parquet_output_dir)

    for group_files in file_groups:
        if not group_files:
            continue

        # Existing tables were validated before processing the first group.
        files_to_process: list[str] = []
        tables_in_group: set[str] = set()
        skipped_tables: set[str] = set()
        for f in group_files:
            ft = get_zip_file_type(f)
            if not ft or ft not in FILE_MAPPINGS:
                continue
            table_name = FILE_MAPPINGS[ft]
            parquet_path = Path(config.parquet_output_dir) / f"{table_name}.parquet"
            if parquet_path.exists():
                if table_name not in skipped_tables:
                    logger.info(f"Skipping {table_name} (already exported)")
                    skipped_tables.add(table_name)
                continue
            files_to_process.append(f)
            tables_in_group.add(table_name)

        if not files_to_process:
            continue

        if filtro is not None:
            prepare_filter_for_group(filtro, files_to_process, load_municipios, load_cnpjs)

        logger.info(f"Processing {len(files_to_process)} files ({', '.join(sorted(tables_in_group))})...")

        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(parquet_worker, f, directory, downloader, parquet, config, filtro, contadores): f
                    for f in files_to_process
                }
                wait_for_workers(futures, "One or more workers failed, aborting to prevent incomplete export")
        else:
            for zip_filename in files_to_process:
                # closing() stops in-flight downloads before a failure releases the run lock.
                with closing(downloader.download_files(directory, [zip_filename])) as csv_files:
                    for csv_path, _ in csv_files:
                        try:
                            write_parquet_csv(csv_path, parquet, config, filtro=filtro, contadores=contadores)

                        except Exception as e:
                            logger.error(f"Error: {csv_path.name}: {e}")
                            raise

        # Flush tables in this group and run post-file commands
        for table_name in tables_in_group:
            parquet_path = parquet.flush_table(table_name)
            if parquet_path:
                logger.info(f"  {table_name}: flushed → {parquet_path.name}")
                run_post_file_command(parquet_path, command)


def run_postgres(
    pending_files: list[str],
    directory: str,
    downloader: Downloader,
    db: "Database",
    config: Config,
    filtro: FiltroProspeccao | None = None,
    contadores: ContadoresFiltro | None = None,
) -> None:
    resumed_tables: set[str] = set()
    if config.loading_strategy == "replace":
        resumed_tables = {
            FILE_MAPPINGS[ft]
            for filename in db.get_processed_files(directory)
            if (ft := get_zip_file_type(filename)) and ft in FILE_MAPPINGS
        }
        db.preserve_tables(resumed_tables)

    # Database mode: process files by dependency group
    file_groups = group_files_by_dependency(
        pending_files, FILTERED_DEPENDENCY_GROUPS if filtro is not None else DEPENDENCY_GROUPS
    )
    workers = config.process_workers
    load_municipios, load_cnpjs = _postgres_loaders(db)

    for group_files in file_groups:
        if not group_files:
            continue

        if filtro is not None:
            prepare_filter_for_group(filtro, group_files, load_municipios, load_cnpjs)

        if workers > 1:
            # Pre-truncate for replace strategy before spawning workers
            pre_truncated: set[str] = set()
            if config.loading_strategy == "replace":
                pre_truncated = {
                    FILE_MAPPINGS[ft] for f in group_files if (ft := get_zip_file_type(f)) and ft in FILE_MAPPINGS
                }
                db.truncate_tables(pre_truncated - resumed_tables)

            logger.info(f"Processing {len(group_files)} files with {workers} workers...")
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(pg_worker, f, directory, downloader, config, pre_truncated, filtro, contadores): f
                    for f in group_files
                }
                wait_for_workers(futures, "One or more workers failed, aborting to prevent data corruption")
        else:
            # Sequential: download in parallel, process one at a time
            # closing() stops in-flight downloads before a failure releases the run lock.
            file_iterator = downloader.download_archives(directory, group_files)
            with (
                closing(file_iterator),
                tqdm(file_iterator, total=len(group_files), desc="Processing", unit="file") as pbar,
            ):
                for paths, zip_filename in pbar:
                    pbar.set_postfix_str(zip_filename[:30])
                    try:
                        load_postgres_archive(
                            paths,
                            zip_filename,
                            directory,
                            db,
                            config,
                            lambda rows: pbar.set_postfix_str(f"{zip_filename[:20]} {rows:,} rows"),
                            filtro=filtro,
                            contadores=contadores,
                        )

                    except Exception as e:
                        logger.error(f"Error: {zip_filename}: {e}")
                        raise


def _try_lock(lock_file: TextIO) -> bool:
    """Return False only when another process holds the lock; other lock failures propagate."""
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except PermissionError:  # EACCES: the byte is locked by another process
            return False
    else:
        import fcntl

        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:  # EWOULDBLOCK: another process holds the lock
            return False
    return True


@contextmanager
def run_lock(temp_dir: str) -> Generator[None]:
    """Lock TEMP_DIR for one run; runs sharing it would delete and append to each other's downloads.

    The OS releases the lock when the file closes or the process exits, including when it is killed.
    """
    Path(temp_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(temp_dir) / ".pipeline.lock", "a") as lock_file:
        if not _try_lock(lock_file):
            logger.error(f"Another run is using TEMP_DIR {temp_dir}; exiting.")
            sys.exit(1)
        yield


def main(
    cfg: Config | None = None,
    *,
    args: argparse.Namespace | None = None,
    filtro: FiltroProspeccao | None = None,
    contadores: ContadoresFiltro | None = None,
) -> str | None:
    """Main pipeline entry point. Returns the processed month (None for --list / nothing pending).

    From the command line (no `cfg`), the prospecting filter comes from the environment
    (PROSPECCAO_FILTRO). A caller passing `cfg` passes `filtro` explicitly too (rodar_mensal does);
    without it the run is exactly the upstream behavior, whatever the environment says.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = args if args is not None else parse_args()
    config = cfg if cfg is not None else Config.from_env()
    if filtro is None and cfg is None:
        filtro = FiltroProspeccao.from_env()
    if filtro is not None:
        logger.info(f"Filtro de prospecção ligado: {filtro.descrever()}")
        if config.output_format == "postgres" and config.loading_strategy != "replace":
            # upsert would keep last month's estabelecimentos that no longer pass the filter
            logger.info("Filtro de prospecção: LOADING_STRATEGY forçada para replace")
            config.loading_strategy = "replace"

    # Handle --list mode
    if args.list:
        available = Downloader(config).get_available_directories()
        print("Available months:")
        for month in available:
            print(f"  {month}")
        return

    with run_lock(config.temp_dir):
        downloader = Downloader(config)
        is_parquet = config.output_format == "parquet"

        if not is_parquet and not config.database_url:
            logger.error("DATABASE_URL not set (required for postgres output)")
            sys.exit(1)

        db = None
        parquet = None

        if not is_parquet:
            from database import Database

            db = Database(config.database_url, retry_attempts=config.retry_attempts)
            db.ensure_schema()

        try:
            if args.month:
                available = downloader.get_available_directories()
                if args.month not in available:
                    logger.error(f"Month {args.month} not available. Use --list to see options.")
                    sys.exit(1)
                directory = args.month
            else:
                directory = downloader.get_latest_directory()

            if is_parquet:
                from parquet_writer import ParquetWriter

                parquet = ParquetWriter(
                    config.parquet_output_dir,
                    source_month=directory,
                    typed=config.parquet_typed_output,
                    extra_provenance={"cnpj.prospeccaoFiltro": filtro.assinatura()} if filtro is not None else None,
                )
                logger.info(f"Parquet mode: output to {config.parquet_output_dir}")

            # Handle --force mode (database only)
            if args.force and db:
                logger.info(f"Force mode: clearing processed files for {directory}")
                db.clear_processed_files(directory)

            all_files = downloader.get_directory_files(directory)
            unsupported = [name for name in all_files if get_zip_file_type(name) is None]
            if unsupported:
                logger.warning("Skipping unsupported source files: %s", ", ".join(unsupported))

            if db:
                processed = db.get_processed_files(directory)
                pending_files = [f for f in all_files if f not in processed]
            else:
                pending_files = list(all_files)

            if filtro is not None:
                skipped = [f for f in pending_files if filtro.ignora_tipo(get_zip_file_type(f))]
                if skipped:
                    logger.info(f"Filtro de prospecção: sócios não são baixados ({len(skipped)} arquivos)")
                pending_files = [f for f in pending_files if f not in skipped]

            if not pending_files:
                logger.info("All files already processed!")
                return directory

            logger.info(f"Processing {len(pending_files)} files from {directory}")

            pending_files.sort(key=get_file_priority)

            if parquet is not None:
                run_parquet(pending_files, directory, downloader, parquet, config, filtro, contadores)
            else:
                assert db is not None
                run_postgres(pending_files, directory, downloader, db, config, filtro, contadores)

            if is_parquet:
                assert parquet is not None
                parquet.close()
                manifest = parquet.write_manifest(source_month=directory)
                total_rows = manifest["totals"]["rows"]
                total_size = manifest["totals"]["sizeBytes"] / 1024 / 1024 / 1024
                logger.info(f"Parquet export complete: {total_rows:,} rows, {total_size:.2f} GB")

            logger.info("Done!")
            return directory

        except Exception as e:
            logger.error(f"Failed: {e}")
            sys.exit(1)

        finally:
            if parquet is not None:
                parquet.abort()
            if db:
                db.disconnect()
            downloader.cleanup()


if __name__ == "__main__":
    main()
