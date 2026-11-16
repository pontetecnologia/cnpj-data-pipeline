#!/usr/bin/env python3
"""Serviço Windows "Ponte - Prospecção Receita" (WinSW roda este script; ver scripts/instalar_servico.ps1).

A configuração vem do GestaoRevenda (Configurações → Prospecção Receita; ver config_prospeccao.py),
buscada a cada 15 min. Uma vez por dia, a partir do horário da tela (padrão 03:00), verifica se a
Receita publicou mês novo. Se o mês mais recente ainda não foi processado, confere o disco e roda
rodar_mensal.py (subprocesso) para ele; se já foi, não faz nada. O resultado de cada execução e um
sinal de vida a cada verificação vão para o GestaoRevenda (aviso_gestaorevenda.py), que guarda o
histórico e manda o e-mail.

- Computador desligado no horário: a verificação do dia roda 10 min depois de ligar.
- "Mês processado" = há execução OK para ele (ou SEM_ENVIO, enquanto o envio estiver desligado),
  feita com o filtro atual da tela. Filtro alterado → o mês é baixado e recarregado na próxima
  verificação programada (nunca na hora de salvar a tela).
- Receita fora do ar ou outra execução em andamento: nova tentativa a cada 60 min, até 3 no dia.
- Disco: exige o mínimo da tela (padrão 25 GB), descontado o que já está em TEMP_DIR.
- No .env ficam só GR_API_URL, GR_CLIENT_ID e GR_CLIENT_SECRET (relidos a cada ciclo).

Uso:
    python servico_prospeccao.py                    # laço do serviço (o WinSW chama assim)
    python servico_prospeccao.py --agora            # verifica e roda agora, em primeiro plano
        [--sem-envio] [--limite N] [--forcar] [--recarregar]
    python servico_prospeccao.py --status           # configuração, verificações, execuções, fila
    python servico_prospeccao.py --testar-conexao   # busca a configuração e manda um sinal
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as hora_do_dia
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event
from types import FrameType
from typing import Any

from dotenv import load_dotenv

import aviso_gestaorevenda as aviso
import config_prospeccao
from config_prospeccao import ConfigProspeccao
from estado_prospeccao import EstadoProspeccao
from rodar_mensal import CODIGO_INTERROMPIDA, CODIGO_OCUPADO, config_pipeline

RAIZ = Path(__file__).resolve().parent
NOME_SERVICO = "Ponte - Prospecção Receita"
GB = 1024**3
INTERVALO_CONFIG = timedelta(minutes=15)

logger = logging.getLogger("servico")

# Resultados de uma verificação (tabela servico_verificacoes)
SEM_NOVIDADE = "SEM_NOVIDADE"
EXECUTADO = "EXECUTADO"
FALHOU = "FALHOU"
DISCO_INSUFICIENTE = "DISCO_INSUFICIENTE"
OCUPADO = "OCUPADO"
FALHA_VERIFICACAO = "FALHA_VERIFICACAO"
INTERROMPIDO = "INTERROMPIDO"  # serviço parado no meio: não é registrado, a verificação volta a valer
RETENTAVEIS = (OCUPADO, FALHA_VERIFICACAO)


@dataclass
class ConfigServico:
    """Horário e disco vêm da tela; o resto é fixo."""

    horario: hora_do_dia = hora_do_dia(3, 0)
    atraso_inicial: timedelta = timedelta(minutes=10)
    disco_min_gb: float = 25.0
    tentativas_dia: int = 3
    intervalo_tentativa: timedelta = timedelta(minutes=60)
    logs_manter_dias: int = 365

    @classmethod
    def de(cls, cfg: ConfigProspeccao) -> ConfigServico:
        return cls(horario=cfg.horario, disco_min_gb=cfg.disco_min_gb)


def recarregar_env() -> None:
    """Relê o .env sobrescrevendo o ambiente (o subprocesso herda os valores atuais)."""
    load_dotenv(RAIZ / ".env", override=True)


def pasta_logs() -> Path:
    return Path(os.getenv("PROSPECCAO_LOGS", "logs"))


# ── Agenda ───────────────────────────────────────────────────────────────────


def verificacao_devida(agora: datetime, cfg: ConfigServico, registros_hoje: list[tuple[str, datetime]]) -> bool:
    """A verificação do dia deve rodar agora? `registros_hoje` = (resultado, quando) do serviço hoje."""
    if agora.time() < cfg.horario:
        return False
    if any(resultado not in RETENTAVEIS for resultado, _ in registros_hoje):
        return False  # o dia já teve uma verificação definitiva
    if len(registros_hoje) >= cfg.tentativas_dia:
        return False
    if registros_hoje and agora - registros_hoje[-1][1] < cfg.intervalo_tentativa:
        return False
    return True


def proxima_verificacao(agora: datetime, cfg: ConfigServico, registros_hoje: list[tuple[str, datetime]]) -> datetime:
    """Quando a próxima verificação deve acontecer (para o --status, o log e o sinal)."""
    hoje = datetime.combine(agora.date(), cfg.horario)
    amanha = hoje + timedelta(days=1)
    if agora < hoje:
        return hoje
    if any(r not in RETENTAVEIS for r, _ in registros_hoje) or len(registros_hoje) >= cfg.tentativas_dia:
        return amanha
    if registros_hoje:
        return max(agora, registros_hoje[-1][1] + cfg.intervalo_tentativa)
    return agora


# ── Disco ────────────────────────────────────────────────────────────────────


def tamanho_pasta(pasta: Path) -> int:
    total = 0
    for arquivo in pasta.rglob("*"):
        try:
            if arquivo.is_file():
                total += arquivo.stat().st_size
        except OSError:
            pass
    return total


def _existente(p: Path) -> Path:
    p = p.resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _unidade(p: Path) -> str:
    return (os.path.splitdrive(str(p))[0] or p.anchor or "/").upper()


def checar_disco(min_gb: float, temp_dir: Path, outras: list[Path]) -> str | None:
    """None se há espaço; senão o motivo. O que já está em TEMP_DIR conta como disponível
    (são downloads que a execução retoma ou apaga) para uma sobra de falha não travar o serviço."""
    minimo = min_gb * GB
    reaproveitavel = tamanho_pasta(temp_dir) if temp_dir.exists() else 0
    unidade_temp = _unidade(_existente(temp_dir))
    vistas: dict[str, Path] = {}
    for pasta in [temp_dir, *outras]:
        existente = _existente(pasta)
        vistas.setdefault(_unidade(existente), existente)
    for unidade, caminho in vistas.items():
        livre = shutil.disk_usage(caminho).free
        credito = reaproveitavel if unidade == unidade_temp else 0
        if livre + credito < minimo:
            extra = f" + {credito / GB:.1f} GB reaproveitáveis em {temp_dir}" if credito else ""
            return (
                f"espaço insuficiente em {unidade}: {livre / GB:.1f} GB livres{extra}; "
                f"mínimo {min_gb:g} GB (Configurações → Prospecção Receita)"
            )
    return None


# ── Execução ─────────────────────────────────────────────────────────────────


def _interromper(proc: subprocess.Popen[bytes]) -> None:
    """Pede a parada (Ctrl+Break → INTERROMPIDA no estado); se não sair em 30 s, mata a árvore."""
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=30)
        return
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        proc.kill()
    proc.wait()


def rodar_fluxo(
    mes: str,
    parar: Event,
    *,
    sem_envio: bool = False,
    limite: int | None = None,
    recarregar: bool = False,
    console: bool = False,
) -> int:
    """Roda rodar_mensal.py --mes <mes> num subprocesso e devolve o código de saída."""
    cmd = [sys.executable, str(RAIZ / "rodar_mensal.py"), "--mes", mes]
    if sem_envio:
        cmd.append("--sem-envio")
    if limite is not None:
        cmd += ["--limite", str(limite)]
    if recarregar:
        cmd.append("--force")
    logger.info(f"Iniciando: {' '.join(cmd[1:])}")
    if console:
        proc = subprocess.Popen(cmd, cwd=RAIZ)
        try:
            return proc.wait()
        except KeyboardInterrupt:  # Ctrl+C chega aos dois processos; espera o filho fechar o estado
            return proc.wait()
    pasta = pasta_logs()
    pasta.mkdir(parents=True, exist_ok=True)
    # Saída bruta do subprocesso (inclui erros anteriores ao log dele); sobrescrita a cada execução
    with open(pasta / "rodar_mensal_saida.txt", "wb") as saida:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            cmd, cwd=RAIZ, stdin=subprocess.DEVNULL, stdout=saida, stderr=subprocess.STDOUT, creationflags=flags
        )
        while proc.poll() is None:
            if parar.wait(5):
                logger.warning("Serviço parando — interrompendo a execução (será retomada na próxima)")
                _interromper(proc)
                break
        return proc.wait()


def filtro_mudou(execucao: dict[str, Any] | None, cfg: ConfigProspeccao) -> bool:
    """A execução que concluiu o mês foi feita com outro filtro da tela? (Execuções de antes da
    configuração pela tela não têm a chave: não forçam recarga.)"""
    contagens: dict[str, Any] = (execucao or {}).get("contagens") or {}
    chave = contagens.get("filtro_chave")
    return bool(chave) and chave != cfg.chave_filtro()


def _avisar(
    estado: EstadoProspeccao,
    cfg: ConfigProspeccao,
    envio_desligado: str | None,
    mes: str | None,
    execucao: dict[str, Any] | None = None,
) -> None:
    """Põe na fila a execução (se houver) e o sinal, e tenta mandar tudo ao GestaoRevenda."""
    if execucao is not None:
        aviso.enfileirar_execucao(estado, execucao)
    registros = estado.verificacoes_do_dia(datetime.now().date())
    proxima = proxima_verificacao(datetime.now(), ConfigServico.de(cfg), registros)
    aviso.enfileirar_sinal(
        estado, aviso.payload_sinal(estado, cfg, envio_desligado=envio_desligado, proxima=proxima, mes_receita=mes)
    )
    enviados, ficaram = aviso.enviar_fila(estado)
    if ficaram:
        logger.warning(f"{ficaram} aviso(s) para o GestaoRevenda ficaram na fila (vão no próximo ciclo)")
    elif enviados:
        logger.info(f"Resultado enviado ao GestaoRevenda ({enviados} item(ns))")


def verificar(
    origem: str,
    parar: Event,
    *,
    sem_envio: bool = False,
    limite: int | None = None,
    forcar: bool = False,
    recarregar: bool = False,
    console: bool = False,
) -> str:
    """Uma verificação completa. Devolve o resultado (e o registra, salvo INTERROMPIDO)."""
    try:
        return _verificar(origem, parar, sem_envio, limite, forcar, recarregar, console)
    except Exception as e:  # nunca deixa o laço do serviço repetir a cada minuto por um erro inesperado
        logger.exception(f"Verificação falhou: {e}")
        with EstadoProspeccao() as estado:
            vid = estado.registrar_verificacao(origem, FALHOU, detalhe=str(e)[:500])
            cfg = config_prospeccao.obter()
            payload = aviso.payload_sem_execucao(
                f"erro:{estado.instancia()}:{vid}", origem, None, "FALHOU", f"A verificação falhou: {e}", cfg
            )
            _avisar(estado, cfg, None, None, payload)
        return FALHOU


def _verificar(
    origem: str, parar: Event, sem_envio: bool, limite: int | None, forcar: bool, recarregar: bool, console: bool
) -> str:
    from downloader import Downloader
    from enviar_gestaorevenda import motivo_envio_desligado

    recarregar_env()
    cfg = config_prospeccao.obter()
    logger.info(f"Configuração: {cfg.descrever_origem()}")
    servico = ConfigServico.de(cfg)
    config = config_pipeline()
    envio_desligado = "--sem-envio" if sem_envio else motivo_envio_desligado(cfg)

    try:
        mes = Downloader(config).get_latest_directory()
    except Exception as e:
        logger.warning(f"Não consegui listar os meses da Receita: {e}")
        with EstadoProspeccao() as estado:
            estado.registrar_verificacao(origem, FALHA_VERIFICACAO, detalhe=str(e)[:500])
            _avisar(estado, cfg, envio_desligado, None)
        return FALHA_VERIFICACAO

    with EstadoProspeccao() as estado:
        concluida = estado.ultima_concluida(mes, aceitar_sem_envio=envio_desligado is not None)
        mudou = filtro_mudou(concluida, cfg)
        if concluida and not (mudou or forcar or recarregar):
            logger.info(f"Mês mais recente da Receita: {mes} — já processado, nada a fazer")
            estado.registrar_verificacao(origem, SEM_NOVIDADE, mes, "já processado")
            _avisar(estado, cfg, envio_desligado, mes)
            return SEM_NOVIDADE
        antes = estado.ultimo_id_execucao()
    if mudou and not recarregar:
        logger.info(f"Filtro alterado na tela (configuração v{cfg.versao}) — o mês {mes} será baixado e recarregado")
        recarregar = True
    situacao = "reprocessando (--forcar)" if concluida and not mudou else "pendente"
    logger.info(f"Mês mais recente da Receita: {mes} — {situacao}")

    motivo = checar_disco(
        servico.disco_min_gb,
        Path(config.temp_dir),
        [Path(tempfile.gettempdir()), Path(config.parquet_output_dir)],
    )
    if motivo:
        logger.error(f"Execução pulada: {motivo}")
        with EstadoProspeccao() as estado:
            vid = estado.registrar_verificacao(origem, DISCO_INSUFICIENTE, mes, motivo)
            payload = aviso.payload_sem_execucao(
                f"pulada:{estado.instancia()}:{vid}", origem, mes, DISCO_INSUFICIENTE, motivo, cfg
            )
            _avisar(estado, cfg, envio_desligado, mes, payload)
        return DISCO_INSUFICIENTE

    codigo = rodar_fluxo(mes, parar, sem_envio=sem_envio, limite=limite, recarregar=recarregar, console=console)
    if parar.is_set() or codigo == CODIGO_INTERROMPIDA:
        logger.warning("Execução interrompida — a próxima verificação retoma de onde parou")
        return INTERROMPIDO  # não registra nem avisa: ao voltar, a verificação do dia continua devida
    if codigo == CODIGO_OCUPADO:
        logger.warning("Outra execução da prospecção está em andamento — nova tentativa mais tarde")
        with EstadoProspeccao() as estado:
            estado.registrar_verificacao(origem, OCUPADO, mes)
        return OCUPADO

    with EstadoProspeccao() as estado:
        execucao = estado.ultima_execucao()
        if execucao is not None and execucao["id"] <= antes:
            execucao = None  # o subprocesso morreu antes de registrar qualquer coisa
        status = execucao["status"] if execucao else None
        resultado = EXECUTADO if status in ("OK", "PARCIAL", "SEM_ENVIO") else FALHOU
        detalhe = json.dumps({"status": status, "codigo": codigo}, ensure_ascii=False)
        vid = estado.registrar_verificacao(origem, resultado, mes, detalhe)
        logger.info(f"Execução do mês {mes} terminou: {status or 'sem registro'} (código {codigo})")
        if execucao is not None:
            payload = aviso.payload_execucao(execucao, origem, estado.instancia(), cfg)
            contagens_execucao: dict[str, Any] = execucao.get("contagens") or {}
            envio_desligado = contagens_execucao.get("envio_desligado", envio_desligado)
        else:
            payload = aviso.payload_sem_execucao(
                f"erro:{estado.instancia()}:{vid}",
                origem,
                mes,
                "FALHOU",
                f"rodar_mensal saiu com código {codigo} sem registrar a execução (ver logs/rodar_mensal_saida.txt)",
                cfg,
            )
        _avisar(estado, cfg, envio_desligado, mes, payload)
    return resultado


# ── Laço do serviço ──────────────────────────────────────────────────────────


def limpar_logs_antigos(pasta: Path, dias: int) -> int:
    limite = time.time() - dias * 86400
    apagados = 0
    for arquivo in pasta.glob("rodar_mensal_*.log"):
        try:
            if arquivo.stat().st_mtime < limite:
                arquivo.unlink()
                apagados += 1
        except OSError:
            pass
    return apagados


def atualizar_config(atual: ConfigProspeccao | None) -> ConfigProspeccao:
    """Busca a configuração e manda o que estiver na fila. Loga quando a versão muda."""
    recarregar_env()
    cfg = config_prospeccao.obter()
    if atual is None or (cfg.versao, cfg.origem) != (atual.versao, atual.origem):
        logger.info(f"Configuração: {cfg.descrever_origem()}")
    with EstadoProspeccao() as estado:
        aviso.enviar_fila(estado)
    return cfg


def laco(parar: Event, agora: type[datetime] = datetime) -> None:
    inicio = agora.now()
    cfg = atualizar_config(None)
    servico = ConfigServico.de(cfg)
    ultima_busca = agora.now()
    logger.info(
        f"{NOME_SERVICO} iniciado (verificação diária às {servico.horario:%H:%M}; "
        f"primeira checagem em {servico.atraso_inicial.total_seconds() / 60:g} min)"
    )
    try:
        from enviar_gestaorevenda import motivo_envio_desligado

        with EstadoProspeccao() as estado:
            _avisar(estado, cfg, motivo_envio_desligado(cfg), None)
    except Exception as e:
        logger.warning(f"Sinal inicial não enviado: {e}")
    while not parar.is_set():
        espera = 60.0
        try:
            instante = agora.now()
            if instante - ultima_busca >= INTERVALO_CONFIG:
                cfg = atualizar_config(cfg)
                ultima_busca = instante
            servico = ConfigServico.de(cfg)
            if instante - inicio >= servico.atraso_inicial:
                with EstadoProspeccao() as estado:
                    registros = estado.verificacoes_do_dia(instante.date())
                if verificacao_devida(instante, servico, registros):
                    verificar("servico", parar)
                    cfg = atualizar_config(cfg)
                    ultima_busca = agora.now()
                    if not parar.is_set():
                        with EstadoProspeccao() as estado:
                            registros = estado.verificacoes_do_dia(agora.now().date())
                        proxima = proxima_verificacao(agora.now(), ConfigServico.de(cfg), registros)
                        logger.info(f"Próxima verificação: {proxima:%Y-%m-%d %H:%M}")
                    limpar_logs_antigos(pasta_logs(), servico.logs_manter_dias)
        except Exception as e:
            logger.exception(f"Erro no ciclo do serviço: {e} — nova tentativa em 15 min")
            espera = 900.0
        parar.wait(espera)
    logger.info(f"{NOME_SERVICO} parado")


# ── Linha de comando ─────────────────────────────────────────────────────────


def configurar_log(manual: bool) -> None:
    pasta = pasta_logs()
    pasta.mkdir(parents=True, exist_ok=True)
    nome = "servico_manual.log" if manual else "servico.log"
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s", "%Y-%m-%d %H:%M:%S")
    arquivo = RotatingFileHandler(pasta / nome, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8")
    handlers: list[logging.Handler] = [arquivo]
    if manual:
        handlers.append(logging.StreamHandler())
    raiz = logging.getLogger()
    raiz.setLevel(logging.INFO)
    for h in handlers:
        h.setFormatter(fmt)
        raiz.addHandler(h)


def mostrar_status() -> int:
    from enviar_gestaorevenda import motivo_envio_desligado

    cfg = config_prospeccao.obter()
    servico = ConfigServico.de(cfg)
    agora = datetime.now()
    with EstadoProspeccao() as estado:
        registros = estado.verificacoes_do_dia(agora.date())
        verificacoes = estado.ultimas_verificacoes(5)
        execucoes = estado.conn.execute(
            "SELECT id, iniciada_em, mes_base, status, contagens FROM execucoes ORDER BY id DESC LIMIT 5"
        ).fetchall()
        fila = estado.tamanho_fila()
    print(f"{NOME_SERVICO}")
    print(f"  Configuração: {cfg.descrever_origem()}")
    print(f"  Envio ao GestaoRevenda: {motivo_envio_desligado(cfg) or 'LIGADO'}")
    uf = cfg.uf or "todas"
    municipios = ", ".join(cfg.municipios) or "todos"
    print(f"  Filtro: CNAEs {', '.join(cfg.cnaes)}; UF {uf}; municípios {municipios}")
    abertura = f"últimos {cfg.abertura_meses} meses" if cfg.abertura_meses else "sem limite"
    print(f"          situação {', '.join(cfg.situacoes)}; abertura: {abertura}")
    proxima = proxima_verificacao(agora, servico, registros)
    print(f"  Verificação diária às {servico.horario:%H:%M}; próxima: {proxima:%Y-%m-%d %H:%M}")
    print(f"  Disco mínimo: {servico.disco_min_gb:g} GB · avisos na fila para o GestaoRevenda: {fila}")
    print("\nÚltimas verificações:")
    for v in verificacoes or [{"em": "-", "origem": "", "mes_receita": "", "resultado": "(nenhuma)", "detalhe": ""}]:
        print(f"  {v['em']}  {v['origem']:<8} {v['mes_receita'] or '':<8} {v['resultado']:<19} {v['detalhe'] or ''}")
    print("\nÚltimas execuções:")
    for id_, iniciada, mes, status, contagens in execucoes:
        c: dict[str, Any] = json.loads(contagens) if contagens else {}
        envio: dict[str, Any] = c.get("envio") or {}
        print(
            f"  #{id_:<4} {iniciada}  {mes or '':<8} {status:<12} "
            f"novas={c.get('novas', '-')} enviadas={envio.get('enviadas', 0)} recusadas={envio.get('erros', 0)}"
        )
    return 0


def testar_conexao() -> int:
    """Busca a configuração e manda um sinal agora. 0 = as duas coisas funcionaram."""
    from enviar_gestaorevenda import motivo_envio_desligado

    cfg = config_prospeccao.obter()
    print(f"Configuração: {cfg.descrever_origem()}")
    with EstadoProspeccao() as estado:
        _avisar(estado, cfg, motivo_envio_desligado(cfg), None)
        fila = estado.tamanho_fila()
    ok = cfg.origem == config_prospeccao.ORIGEM_REMOTA and fila == 0
    print("Sinal enviado ao GestaoRevenda." if fila == 0 else f"Sinal NÃO enviado: {fila} item(ns) na fila (ver log).")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Serviço {NOME_SERVICO}")
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--agora", action="store_true", help="Verifica e roda agora, em primeiro plano")
    modo.add_argument("--status", action="store_true", help="Mostra configuração, verificações e execuções")
    modo.add_argument("--testar-conexao", action="store_true", help="Busca a configuração e manda um sinal")
    parser.add_argument("--sem-envio", action="store_true", help="(--agora) não envia ao GestaoRevenda")
    parser.add_argument("--limite", type=int, help="(--agora) envia só as N primeiras")
    parser.add_argument("--forcar", action="store_true", help="(--agora) roda mesmo se o mês já foi processado")
    parser.add_argument("--recarregar", action="store_true", help="(--agora) baixa e carrega o mês do zero")
    args = parser.parse_args(argv)
    if not args.agora and (args.sem_envio or args.limite is not None or args.forcar or args.recarregar):
        parser.error("--sem-envio, --limite, --forcar e --recarregar só valem com --agora")

    os.chdir(RAIZ)
    recarregar_env()
    if args.status:
        return mostrar_status()

    configurar_log(manual=args.agora or args.testar_conexao)
    if args.testar_conexao:
        return testar_conexao()

    # Com TMP/TEMP apontando para temp\sys (XML do WinSW), garante que a pasta exista
    Path(tempfile.gettempdir()).mkdir(parents=True, exist_ok=True)

    parar = Event()

    def ao_parar(signum: int, _frame: FrameType | None) -> None:
        logger.info(f"Sinal {signum} recebido — parando")
        parar.set()

    if args.agora:
        resultado = verificar(
            "manual",
            parar,
            sem_envio=args.sem_envio,
            limite=args.limite,
            forcar=args.forcar,
            recarregar=args.recarregar,
            console=True,
        )
        print(f"\nResultado: {resultado}")
        return 0 if resultado in (SEM_NOVIDADE, EXECUTADO) else 1

    for nome in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, nome):
            signal.signal(getattr(signal, nome), ao_parar)
    laco(parar)
    return 0


if __name__ == "__main__":
    sys.exit(main())
