#!/usr/bin/env python3
"""Execução mensal completa: baixa o mês mais recente da Receita, carrega com o filtro de
prospecção, exporta, envia os novos ao GestaoRevenda, limpa os temporários e grava o log.

Uso:
    python rodar_mensal.py                 # mês mais recente, só os novos
    python rodar_mensal.py --tudo          # envia também os já enviados (o GestaoRevenda deduplica)
    python rodar_mensal.py --sem-envio     # tudo menos o envio (gera o CSV em saida/)
    python rodar_mensal.py --mes 2026-10   # um mês específico
    python rodar_mensal.py --limite 20     # envia só 20 (primeiro teste real)
    just rodar-mensal [args]

Configuração (filtro, envio ligado/desligado): da tela do GestaoRevenda, Configurações →
Prospecção Receita (ver config_prospeccao.py). Sem resposta, a última guardada; nunca obtida,
valores padrão com envio desligado. Envio só com o envio ligado na tela e GR_* preenchidos no
.env; senão a execução roda como --sem-envio e registra o motivo.

Janela de início de atividade: até a PRIMEIRA execução com envio completo (status OK) entram todas
as ativas da região (0 meses); depois, a "data de abertura mínima" da tela. Execuções com --limite
(PARCIAL) e --sem-envio não contam.

Filtro mudou desde a carga do mês (outra assinatura no Parquet): o mês é baixado e carregado de
novo, como com --force.

Uma execução por vez: a trava estado/.prospeccao.lock cobre carga, exportação e envio. Quem
chega com outra em andamento sai com código 2 sem tocar em nada.

Retomada: se o processo morrer no meio, a próxima execução marca a anterior como INTERROMPIDA,
retoma downloads (.part) e tabelas Parquet já gravadas, e não reenvia o que o GestaoRevenda já
aceitou. Os temporários (temp/) só são apagados quando a execução termina bem.

Grava em Parquet (OUTPUT_FORMAT=parquet é o padrão aqui; o serviço roda sem o Postgres do Docker).
Log: logs/rodar_mensal_AAAA-MM-DD_HHMMSS.log.
Código de saída: 0 = OK / PARCIAL / SEM_ENVIO, 1 = falha, 2 = outra execução em andamento,
130 = interrompida (Ctrl+C, parada do serviço).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import config_prospeccao
from config import Config
from config_prospeccao import PRIMEIRA_EXECUCAO_MESES, ConfigProspeccao
from estado_prospeccao import CNPJ_INEDITO, FILIAL_NOVA, EstadoProspeccao
from filtro import ContadoresFiltro, FiltroProspeccao

logger = logging.getLogger("rodar_mensal")

CODIGO_OK = 0
CODIGO_FALHA = 1
CODIGO_OCUPADO = 2
CODIGO_INTERROMPIDA = 130
STATUS_CONCLUIDOS = ("OK", "PARCIAL", "SEM_ENVIO")


def configurar_log(pasta: Path) -> tuple[Path, list[logging.Handler]]:
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / f"rodar_mensal_{time.strftime('%Y-%m-%d_%H%M%S')}.log"
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s", "%Y-%m-%d %H:%M:%S")
    raiz = logging.getLogger()
    raiz.setLevel(logging.INFO)
    handlers: list[logging.Handler] = [logging.FileHandler(caminho, encoding="utf-8"), logging.StreamHandler()]
    for handler in handlers:
        handler.setFormatter(fmt)
        raiz.addHandler(handler)
    return caminho, handlers


def caminho_trava() -> Path:
    """Arquivo de trava da prospecção, ao lado do estado local."""
    return Path(os.getenv("PROSPECCAO_ESTADO", "estado/prospeccao.sqlite")).parent / ".prospeccao.lock"


@contextmanager
def trava_prospeccao(caminho: Path) -> Generator[bool]:
    """True se obteve a trava. O sistema operacional solta a trava quando o processo termina,
    inclusive se for morto ou o computador desligar."""
    from main import _try_lock  # pyright: ignore[reportPrivateUsage] - mesma trava do pipeline

    caminho.parent.mkdir(parents=True, exist_ok=True)
    with open(caminho, "a") as arquivo:
        yield _try_lock(arquivo)


def limpar_temporarios(temp_dir: str) -> int:
    """Apaga ZIPs, CSVs e downloads parciais de TEMP_DIR (mantém o arquivo de lock)."""
    apagados = 0
    pasta = Path(temp_dir)
    if not pasta.exists():
        return 0
    for arquivo in pasta.rglob("*"):
        if arquivo.is_file() and arquivo.name != ".pipeline.lock":
            try:
                arquivo.unlink()
                apagados += 1
            except OSError as e:
                logger.warning(f"Não consegui apagar {arquivo}: {e}")
    return apagados


def _meses_janela(estado: EstadoProspeccao, cfg: ConfigProspeccao) -> int:
    ja_rodou = estado.conn.execute("SELECT 1 FROM execucoes WHERE status = 'OK' LIMIT 1").fetchone() is not None
    return cfg.abertura_meses if ja_rodou else PRIMEIRA_EXECUCAO_MESES


def assinatura_carregada(pasta_mes: Path) -> str | None:
    """Assinatura do filtro com que o Parquet do mês foi gravado ("" se não tiver; None se a
    pasta não tem nenhuma tabela ainda)."""
    import pyarrow.parquet as pq

    for arquivo in sorted(pasta_mes.glob("*.parquet")):
        metadados = pq.read_metadata(arquivo).metadata or {}
        valor = metadados.get(b"cnpj.prospeccaoFiltro")
        return valor.decode() if valor else ""
    return None


def config_pipeline() -> Config:
    """Config do pipeline para a prospecção: Parquet, salvo se OUTPUT_FORMAT disser outra coisa."""
    config = Config.from_env()  # carrega o .env
    if not os.getenv("OUTPUT_FORMAT"):
        config = replace(config, output_format="parquet")
    return config


def executar(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execução mensal da prospecção Receita → GestaoRevenda")
    parser.add_argument("--mes", help="Mês da Receita (AAAA-MM). Padrão: o mais recente")
    parser.add_argument("--tudo", action="store_true", help="Envia também os CNPJs já enviados antes")
    parser.add_argument("--sem-envio", action="store_true", help="Não envia ao GestaoRevenda (só gera o CSV)")
    parser.add_argument("--force", action="store_true", help="Recarrega o mês mesmo se já foi carregado")
    parser.add_argument(
        "--limite", type=int, help="Envia só as N primeiras (teste); as demais continuam novas para a próxima"
    )
    args = parser.parse_args(argv)

    if hasattr(signal, "SIGBREAK"):
        # Ctrl+Break (o serviço pede a parada assim) vira KeyboardInterrupt: a execução fecha o
        # estado como INTERROMPIDA em vez de o processo sumir sem registrar nada.
        signal.signal(signal.SIGBREAK, signal.default_int_handler)

    caminho_log, handlers = configurar_log(Path(os.getenv("PROSPECCAO_LOGS", "logs")))
    try:
        return _executar(args, caminho_log)
    finally:
        for handler in handlers:
            logging.getLogger().removeHandler(handler)
            handler.close()


def _executar(args: argparse.Namespace, caminho_log: Path) -> int:
    if not sys.stderr.isatty():
        os.environ.setdefault("TQDM_DISABLE", "1")  # agendador/serviço: sem barra de progresso no log

    config = config_pipeline()
    with trava_prospeccao(caminho_trava()) as obtida:
        if not obtida:
            logger.error("Já há uma execução da prospecção em andamento (serviço ou manual) — nada foi feito")
            return CODIGO_OCUPADO
        return _executar_travado(args, caminho_log, config)


def _executar_travado(args: argparse.Namespace, caminho_log: Path, config: Config) -> int:
    from enviar_gestaorevenda import motivo_envio_desligado

    inicio = time.monotonic()
    contagens: dict = {"mes_base": None, "modo": "tudo" if args.tudo else "novos", "log": str(caminho_log)}
    cfg = config_prospeccao.obter()
    logger.info(f"Configuração: {cfg.descrever_origem()}")
    contagens.update(
        config_versao=cfg.versao,
        filtro_versao=cfg.filtro_versao,
        origem_config=cfg.origem,
        filtro_chave=cfg.chave_filtro(),
    )
    desligado = None if args.sem_envio else motivo_envio_desligado(cfg)
    sem_envio = args.sem_envio or desligado is not None
    if desligado:
        logger.warning(f"{desligado} — rodando como --sem-envio")
        contagens["envio_desligado"] = desligado
    elif args.sem_envio:
        contagens["envio_desligado"] = "--sem-envio"

    estado = EstadoProspeccao()
    orfas = estado.marcar_interrompidas()
    if orfas:
        logger.warning(f"{orfas} execução(ões) anterior(es) não terminaram (desligamento?) — marcadas INTERROMPIDA")
    execucao_id: int | None = None
    status = "FALHOU"
    try:
        meses = _meses_janela(estado, cfg)

        def novo_filtro() -> FiltroProspeccao:
            return cfg.filtro(meses)

        filtro = novo_filtro()
        contagens["janela_inicio_atividade_meses"] = meses
        logger.info(f"Filtro: {filtro.descrever()}")

        # 1+2. Baixa e carrega com filtro (main do pipeline)
        from downloader import Downloader
        from main import main as pipeline_main

        mes = args.mes or Downloader(config).get_latest_directory()
        contagens["mes_base"] = mes
        if config.output_format == "parquet":
            # Uma pasta por mês: retomar um mês nunca mistura arquivos de outro mês/filtro
            pasta_mes = Path(config.parquet_output_dir) / mes
            recarregar = args.force
            if not recarregar and pasta_mes.exists():
                carregada = assinatura_carregada(pasta_mes)
                if carregada is not None and carregada != filtro.assinatura():
                    logger.info(f"Filtro mudou desde a carga de {mes} — baixando e carregando o mês de novo")
                    contagens["recarga_por_filtro"] = True
                    recarregar = True
            if recarregar and pasta_mes.exists():
                shutil.rmtree(pasta_mes)  # --force no Parquet = exportar o mês do zero
            config = replace(config, parquet_output_dir=str(pasta_mes))
        contadores = ContadoresFiltro()
        logger.info(f"Carga do mês {mes} ({config.output_format})")
        try:
            pipeline_main(
                config,
                args=argparse.Namespace(list=False, month=mes, force=args.force),
                filtro=filtro,
                contadores=contadores,
            )
        except SystemExit as e:
            if e.code not in (0, None):
                # main() loga o erro e sai com sys.exit(1): a causa fica no contexto do SystemExit
                causa = f": {e.__context__}" if e.__context__ is not None else " — ver mensagens acima"
                raise RuntimeError(f"Carga falhou (código {e.code}){causa}") from e
        contagens["linhas_lidas"] = contadores.lidas
        contagens["linhas_filtradas"] = contadores.mantidas
        if not contadores.lidas:
            logger.info("Mês já estava carregado — exportando do que está no banco")

        # 3. Exporta + marca novos
        from exportar_prospeccao import escrever_csv, exportar

        filtro_exportacao = novo_filtro()
        linhas, selecionadas, mes_base = exportar(config, filtro_exportacao, estado, tudo=args.tudo)
        nao_reconhecidos = sorted(
            set(filtro.municipios_nao_reconhecidos) | set(filtro_exportacao.municipios_nao_reconhecidos)
        )
        if nao_reconhecidos:
            logger.warning(f"Municípios não reconhecidos (ficaram fora do filtro): {', '.join(nao_reconhecidos)}")
        contagens["municipios_nao_reconhecidos"] = nao_reconhecidos
        mes_base = mes_base or mes
        novas = sum(1 for linha in linhas if linha["novidade"] in (CNPJ_INEDITO, FILIAL_NOVA))
        contagens.update(
            exportadas=len(linhas),
            novas=novas,
            novas_cnpj_inedito=sum(1 for linha in linhas if linha["novidade"] == CNPJ_INEDITO),
            novas_filial=sum(1 for linha in linhas if linha["novidade"] == FILIAL_NOVA),
            selecionadas=len(selecionadas),
            a_enviar=len(selecionadas),  # antes do --limite: o que uma execução com envio mandaria
        )
        cortada = False
        if args.limite is not None and len(selecionadas) > args.limite:
            logger.info(f"--limite {args.limite}: enviando {args.limite} de {len(selecionadas):,}")
            selecionadas = selecionadas[: args.limite]
            contagens["limite"] = args.limite
            cortada = True
        csv_lista = escrever_csv(selecionadas, Path("saida") / f"prospeccao_{mes_base}.csv")
        contagens["csv_lista"] = str(csv_lista)
        logger.info(f"Exportação: {len(linhas):,} estabelecimentos, {novas:,} novos, {len(selecionadas):,} a enviar")

        # 4. Envia
        execucao_id = estado.iniciar_execucao(mes_base, "tudo" if args.tudo else "novos")
        if sem_envio:
            logger.info(f"Nada foi enviado ao GestaoRevenda ({contagens['envio_desligado']})")
            contagens["envio"] = None
        elif not selecionadas:
            logger.info("Nada novo para enviar")
            contagens["envio"] = {"enviadas": 0}
        else:
            from enviar_gestaorevenda import ConfigEnvio, enviar

            resultado = enviar(selecionadas, mes_base, estado, execucao_id, ConfigEnvio.from_env())
            contagens["envio"] = resultado.como_dict()
        # PARCIAL não conta como "mês concluído" nem como 1ª execução: o resto vai na próxima
        status = "SEM_ENVIO" if sem_envio else ("PARCIAL" if cortada else "OK")
        return CODIGO_OK
    except KeyboardInterrupt:
        logger.warning("Execução interrompida — a próxima retoma de onde parou")
        contagens["erro"] = "interrompida"
        status = "INTERROMPIDA"
        return CODIGO_INTERROMPIDA
    except Exception as e:
        logger.exception(f"Execução mensal falhou: {e}")
        contagens["erro"] = str(e)
        return CODIGO_FALHA
    finally:
        # 5. Limpa temporários só se terminou bem: numa falha, os downloads parciais ficam para retomar
        if status in STATUS_CONCLUIDOS:
            contagens["temporarios_apagados"] = limpar_temporarios(config.temp_dir)
        else:
            contagens["temporarios_apagados"] = 0
            logger.info(f"Temporários mantidos em {config.temp_dir} para a próxima execução retomar")
        contagens["duracao_min"] = round((time.monotonic() - inicio) / 60, 1)
        if execucao_id is None:
            execucao_id = estado.iniciar_execucao(contagens.get("mes_base"), contagens["modo"])
        estado.finalizar_execucao(execucao_id, status, contagens)
        estado.close()
        logger.info(f"Resumo ({status}): {json.dumps(contagens, ensure_ascii=False)}")
        logger.info(f"Log: {caminho_log}")


if __name__ == "__main__":
    sys.exit(executar())
