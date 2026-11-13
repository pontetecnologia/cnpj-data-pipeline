"""Filtro de prospecção e exportação contra um PostgreSQL real (porta 5435, docker compose)."""

from pathlib import Path
from threading import Event

import pytest

from config import Config
from database import Database
from estado_prospeccao import CNPJ_INEDITO, EstadoProspeccao
from exportar_prospeccao import exportar
from filtro import ContadoresFiltro, FiltroProspeccao
from main import main
from tests.test_prospeccao import HOJE
from tests.test_prospeccao import source_files as source_files  # noqa: F401 - fixture com os CSVs do cenário


@pytest.mark.parametrize("workers", [1, 2])
def test_postgres_filtrado_carrega_so_o_que_passa_e_exporta(
    workers: int,
    source_server: tuple[str, Event, list[str]],
    empty_db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url, _fail, downloads = source_server
    monkeypatch.setattr("sys.argv", ["cnpj-pipeline", "--month", "2024-01"])
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    cfg = Config(
        database_url=empty_db.database_url,
        base_url=base_url,
        share_token="test",
        temp_dir=str(tmp_path / "downloads"),
        download_workers=1,
        process_workers=workers,
        loading_strategy="upsert",  # o filtro força replace
        retry_attempts=1,
        retry_delay=0,
        connect_timeout=2,
        read_timeout=2,
        stall_timeout=2,
    )
    contadores = ContadoresFiltro()

    filtro = FiltroProspeccao(uf="SC", municipios_config=["JOINVILLE"], hoje=HOJE)
    assert main(cfg, filtro=filtro, contadores=contadores) == "2024-01"
    assert cfg.loading_strategy == "replace"
    assert "/2024-01/Socios0.zip" not in downloads

    def contar(tabela: str) -> int:
        return empty_db.fetch_rows(f"SELECT COUNT(*) FROM {tabela}")[0][0]

    assert (contar("estabelecimentos"), contar("empresas"), contar("dados_simples"), contar("socios")) == (2, 1, 1, 0)
    assert contadores.mantidas["estabelecimentos"] == 2
    assert "Socios0.zip" not in {r[0] for r in empty_db.fetch_rows("SELECT filename FROM processed_files")}

    with EstadoProspeccao(tmp_path / "estado.sqlite") as estado:
        _linhas, selecionadas, mes = exportar(
            cfg, FiltroProspeccao(uf="SC", municipios_config=["Joinville"], hoje=HOJE), estado
        )
    assert mes == "2024-01"
    assert [(linha["cnpj"], linha["novidade"]) for linha in selecionadas] == [
        ("11111111000100", CNPJ_INEDITO),
        ("11111111000211", CNPJ_INEDITO),
    ]
    matriz = selecionadas[0]
    assert (matriz["razao_social"], matriz["porte"], matriz["simples_nacional"], matriz["mei"]) == (
        "MERCADO UM LTDA",
        "ME",
        True,
        False,
    )
    assert matriz["data_inicio_atividade"] == "2026-06-01"
    assert (matriz["cep"], matriz["capital_social"]) == ("89201-000", 10000.0)
    assert matriz["natureza_juridica"] == "Sociedade Empresária Limitada"
