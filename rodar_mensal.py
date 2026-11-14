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

Janela de início de atividade: na PRIMEIRA execução bem-sucedida (estado local vazio) usa
FILTRO_PRIMEIRA_EXECUCAO_MESES (padrão 0 = todas as ativas da região); nas seguintes,
FILTRO_INICIO_ATIVIDADE_MESES (padrão 12).

Log: logs/rodar_mensal_AAAA-MM-DD_HHMMSS.log. Código de saída 0 = OK, 1 = falha (para o agendador).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

from config import Config
from estado_prospeccao import CNPJ_INEDITO, FILIAL_NOVA, EstadoProspeccao
from filtro import ContadoresFiltro, FiltroProspeccao

logger = logging.getLogger("rodar_mensal")


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


def _meses_janela(estado: EstadoProspeccao) -> int:
    ja_rodou = estado.conn.execute("SELECT 1 FROM execucoes WHERE status = 'OK' LIMIT 1").fetchone() is not None
    if ja_rodou:
        return int(os.getenv("FILTRO_INICIO_ATIVIDADE_MESES", "12"))
    return int(os.getenv("FILTRO_PRIMEIRA_EXECUCAO_MESES", "0"))


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

    caminho_log, handlers = configurar_log(Path(os.getenv("PROSPECCAO_LOGS", "logs")))
    try:
        return _executar(args, caminho_log)
    finally:
        for handler in handlers:
            logging.getLogger().removeHandler(handler)
            handler.close()


def _executar(args: argparse.Namespace, caminho_log: Path) -> int:
    inicio = time.monotonic()
    if not sys.stderr.isatty():
        os.environ.setdefault("TQDM_DISABLE", "1")  # agendador: sem barra de progresso no log

    config = Config.from_env()  # carrega o .env
    if FiltroProspeccao.from_env() is None:
        logger.error("PROSPECCAO_FILTRO precisa estar ligado (true) no .env para a execução mensal")
        return 1

    contagens: dict = {"mes_base": None, "modo": "tudo" if args.tudo else "novos"}
    estado = EstadoProspeccao()
    execucao_id: int | None = None
    status = "FALHOU"
    try:
        meses = _meses_janela(estado)

        def novo_filtro() -> FiltroProspeccao:
            f = FiltroProspeccao.from_env()
            assert f is not None
            f.meses_inicio_atividade = meses  # sem mexer no ambiente do processo
            return f

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
            if args.force and pasta_mes.exists():
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
                raise RuntimeError(f"Carga falhou (código {e.code}) — ver mensagens acima") from e
        contagens["linhas_lidas"] = contadores.lidas
        contagens["linhas_filtradas"] = contadores.mantidas
        if not contadores.lidas:
            logger.info("Mês já estava carregado — exportando do que está no banco")

        # 3. Exporta + marca novos
        from exportar_prospeccao import escrever_csv, exportar

        linhas, selecionadas, mes_base = exportar(config, novo_filtro(), estado, tudo=args.tudo)
        mes_base = mes_base or mes
        novas = sum(1 for linha in linhas if linha["novidade"] in (CNPJ_INEDITO, FILIAL_NOVA))
        contagens.update(
            exportadas=len(linhas),
            novas=novas,
            novas_cnpj_inedito=sum(1 for linha in linhas if linha["novidade"] == CNPJ_INEDITO),
            novas_filial=sum(1 for linha in linhas if linha["novidade"] == FILIAL_NOVA),
            selecionadas=len(selecionadas),
        )
        if args.limite is not None and len(selecionadas) > args.limite:
            logger.info(f"--limite {args.limite}: enviando {args.limite} de {len(selecionadas):,}")
            selecionadas = selecionadas[: args.limite]
            contagens["limite"] = args.limite
        csv_lista = escrever_csv(selecionadas, Path("saida") / f"prospeccao_{mes_base}.csv")
        contagens["csv_lista"] = str(csv_lista)
        logger.info(f"Exportação: {len(linhas):,} estabelecimentos, {novas:,} novos, {len(selecionadas):,} a enviar")

        # 4. Envia
        execucao_id = estado.iniciar_execucao(mes_base, "tudo" if args.tudo else "novos")
        if args.sem_envio:
            logger.info("--sem-envio: nada foi enviado ao GestaoRevenda")
            contagens["envio"] = None
        elif not selecionadas:
            logger.info("Nada novo para enviar")
            contagens["envio"] = {"enviadas": 0}
        else:
            from enviar_gestaorevenda import ConfigEnvio, enviar

            resultado = enviar(selecionadas, mes_base, estado, execucao_id, ConfigEnvio.from_env())
            contagens["envio"] = resultado.como_dict()
        status = "OK" if not args.sem_envio else "SEM_ENVIO"
        return 0
    except Exception as e:
        logger.exception(f"Execução mensal falhou: {e}")
        contagens["erro"] = str(e)
        return 1
    finally:
        # 5. Limpa temporários (ZIPs/CSVs) e registra
        contagens["temporarios_apagados"] = limpar_temporarios(config.temp_dir)
        contagens["duracao_min"] = round((time.monotonic() - inicio) / 60, 1)
        if execucao_id is None:
            execucao_id = estado.iniciar_execucao(contagens.get("mes_base"), contagens["modo"])
        estado.finalizar_execucao(execucao_id, status, contagens)
        estado.close()
        logger.info(f"Resumo ({status}): {json.dumps(contagens, ensure_ascii=False)}")
        logger.info(f"Log: {caminho_log}")


if __name__ == "__main__":
    sys.exit(executar())
