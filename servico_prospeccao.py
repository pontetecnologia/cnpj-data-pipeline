#!/usr/bin/env python3
"""Serviço Windows "Ponte - Prospecção Receita" (WinSW roda este script; ver scripts/instalar_servico.ps1).

Uma vez por dia, a partir de SERVICO_HORARIO (padrão 03:00), verifica se a Receita publicou mês
novo. Se o mês mais recente ainda não foi processado, confere o disco e roda rodar_mensal.py
(subprocesso) para ele; se já foi, não faz nada. Ao terminar, manda um e-mail com as contagens ou
com o erro.

- Computador desligado às 03:00: a verificação do dia roda SERVICO_ATRASO_INICIAL_MIN depois de ligar.
- "Mês processado" = há execução OK para ele (ou SEM_ENVIO, enquanto o envio estiver desligado).
  Um mês carregado mas não enviado, ou interrompido no meio, continua pendente e é retomado.
- Receita fora do ar ou outra execução em andamento: nova tentativa a cada
  SERVICO_INTERVALO_TENTATIVA_MIN, até SERVICO_TENTATIVAS_DIA vezes no dia.
- Disco: exige SERVICO_DISCO_MIN_GB livres (descontado o que já está em TEMP_DIR, que é retomado).
- O .env é relido a cada ciclo: mudar SERVICO_ENVIO_ATIVO ou o horário não exige reiniciar o serviço.

Uso:
    python servico_prospeccao.py                  # laço do serviço (o WinSW chama assim)
    python servico_prospeccao.py --agora          # verifica e roda agora, em primeiro plano
        [--sem-envio] [--limite N] [--forcar] [--recarregar]
    python servico_prospeccao.py --status         # última verificação, execuções, próxima verificação
    python servico_prospeccao.py --testar-email   # manda um e-mail de teste
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

from estado_prospeccao import EstadoProspeccao
from rodar_mensal import CODIGO_INTERROMPIDA, CODIGO_OCUPADO

RAIZ = Path(__file__).resolve().parent
NOME_SERVICO = "Ponte - Prospecção Receita"
GB = 1024**3

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
    horario: hora_do_dia = hora_do_dia(3, 0)
    atraso_inicial: timedelta = timedelta(minutes=10)
    disco_min_gb: float = 25.0
    tentativas_dia: int = 3
    intervalo_tentativa: timedelta = timedelta(minutes=60)
    logs_manter_dias: int = 365

    @classmethod
    def from_env(cls) -> ConfigServico:
        texto = os.getenv("SERVICO_HORARIO", "03:00").strip()
        try:
            h, m = (int(p) for p in texto.split(":"))
            horario = hora_do_dia(h, m)
        except ValueError as e:
            raise ValueError(f"SERVICO_HORARIO deve ser HH:MM (veio {texto!r})") from e
        return cls(
            horario=horario,
            atraso_inicial=timedelta(minutes=float(os.getenv("SERVICO_ATRASO_INICIAL_MIN", "10"))),
            disco_min_gb=float(os.getenv("SERVICO_DISCO_MIN_GB", "25")),
            tentativas_dia=max(1, int(os.getenv("SERVICO_TENTATIVAS_DIA", "3"))),
            intervalo_tentativa=timedelta(minutes=float(os.getenv("SERVICO_INTERVALO_TENTATIVA_MIN", "60"))),
            logs_manter_dias=int(os.getenv("SERVICO_LOGS_MANTER_DIAS", "365")),
        )


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
    """Quando a próxima verificação deve acontecer (para o --status e o log)."""
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
                f"mínimo {min_gb:g} GB (SERVICO_DISCO_MIN_GB)"
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


def _ultimas_linhas(caminho: str | None, n: int = 30) -> str:
    if not caminho or not Path(caminho).exists():
        return ""
    with open(caminho, encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-n:])


def montar_aviso(mes: str, execucao: dict[str, Any] | None, codigo: int) -> tuple[str, str]:
    """Assunto e corpo do e-mail de fim de execução."""
    execucao = execucao or {}
    c: dict[str, Any] = execucao.get("contagens") or {}
    status: str = execucao.get("status") or "FALHOU"
    envio: dict[str, Any] = c.get("envio") or {}
    lidas_tabela: dict[str, int] = c.get("linhas_lidas") or {}
    filtradas_tabela: dict[str, int] = c.get("linhas_filtradas") or {}
    lidas = sum(lidas_tabela.values())
    filtradas = sum(filtradas_tabela.values())
    desligado: str | None = c.get("envio_desligado")

    if status in ("OK", "PARCIAL", "SEM_ENVIO"):
        rotulo = {"OK": "OK", "PARCIAL": "OK PARCIAL", "SEM_ENVIO": "OK (envio desligado)"}[status]
        assunto = (
            f"[Prospecção Receita] {rotulo} {mes} — {c.get('novas', 0):,} novas, {envio.get('enviadas', 0):,} enviadas"
        )
    else:
        erro = c.get("erro") or f"rodar_mensal saiu com código {codigo} sem registrar a execução"
        assunto = f"[Prospecção Receita] FALHOU {mes} — {erro[:120]}"

    linhas = [f"Mês da Receita: {mes}", f"Status: {status}"]
    if desligado:
        linhas.append(f"ENVIO DESLIGADO: {desligado}. Nada foi enviado ao GestaoRevenda.")
    if c.get("limite"):
        linhas.append(f"Envio limitado a {c['limite']} (--limite): o restante segue como novo para a próxima.")
    linhas += [
        "",
        "Contagens",
        f"  Linhas lidas da Receita:   {lidas:,}" + ("   (mês já carregado antes)" if not lidas else ""),
        f"  Linhas após o filtro:      {filtradas:,}",
        f"  Estabelecimentos na lista: {c.get('exportadas', 0):,}",
        f"  Novos:                     {c.get('novas', 0):,}"
        f"  (CNPJ inédito {c.get('novas_cnpj_inedito', 0):,}, filial nova {c.get('novas_filial', 0):,})",
        f"  Enviados:                  {envio.get('enviadas', 0):,}",
        f"    importados:              {envio.get('importados', 0):,}  (bloqueados {envio.get('bloqueados', 0):,})",
        f"    duplicados:              {envio.get('duplicados', 0):,}",
        f"    recusados (erro):        {envio.get('erros', 0):,}",
        "",
        f"Janela de início de atividade: {c.get('janela_inicio_atividade_meses', '?')} meses",
        f"Duração: {c.get('duracao_min', '?')} min",
    ]
    if c.get("csv_lista"):
        linhas.append(f"Lista: {RAIZ / c['csv_lista']}")
    if envio.get("backup"):
        linhas.append(f"Backup do envio: {RAIZ / envio['backup']}")
    if c.get("log"):
        linhas.append(f"Log: {RAIZ / c['log']}")
    if status not in ("OK", "PARCIAL", "SEM_ENVIO"):
        linhas += ["", f"Erro: {c.get('erro', '(sem detalhe)')}", "", "Últimas linhas do log:"]
        linhas.append(_ultimas_linhas(c.get("log")) or "(sem log — ver logs/rodar_mensal_saida.txt)")
        linhas += ["", "A próxima verificação diária tenta de novo, retomando de onde parou."]
    linhas += ["", f"Computador: {os.getenv('COMPUTERNAME', '?')}  ·  {NOME_SERVICO}"]
    return assunto, "\n".join(linhas)


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
            estado.registrar_verificacao(origem, FALHOU, detalhe=str(e)[:500])
        from notificacao import enviar_email

        enviar_email(f"[Prospecção Receita] FALHOU — {str(e)[:120]}", f"A verificação falhou:\n\n{e}")
        return FALHOU


def _verificar(
    origem: str, parar: Event, sem_envio: bool, limite: int | None, forcar: bool, recarregar: bool, console: bool
) -> str:
    from config import Config
    from downloader import Downloader
    from enviar_gestaorevenda import motivo_envio_desligado
    from notificacao import enviar_email

    recarregar_env()
    cfg = ConfigServico.from_env()
    config = Config.from_env()
    if config.output_format != "parquet":
        logger.warning("OUTPUT_FORMAT não é parquet: a carga depende do Postgres (Docker) estar no ar")

    try:
        mes = Downloader(config).get_latest_directory()
    except Exception as e:
        logger.warning(f"Não consegui listar os meses da Receita: {e}")
        with EstadoProspeccao() as estado:
            estado.registrar_verificacao(origem, FALHA_VERIFICACAO, detalhe=str(e)[:500])
        return FALHA_VERIFICACAO

    desligado = sem_envio or motivo_envio_desligado() is not None
    with EstadoProspeccao() as estado:
        concluido = estado.mes_concluido(mes, aceitar_sem_envio=desligado)
        if concluido and not (forcar or recarregar):
            logger.info(f"Mês mais recente da Receita: {mes} — já processado, nada a fazer")
            estado.registrar_verificacao(origem, SEM_NOVIDADE, mes, "já processado")
            return SEM_NOVIDADE
        antes = estado.ultimo_id_execucao()
    logger.info(f"Mês mais recente da Receita: {mes} — " + ("reprocessando (--forcar)" if concluido else "pendente"))

    motivo = checar_disco(
        cfg.disco_min_gb,
        Path(config.temp_dir),
        [Path(tempfile.gettempdir()), Path(config.parquet_output_dir)],
    )
    if motivo:
        logger.error(f"Execução pulada: {motivo}")
        with EstadoProspeccao() as estado:
            estado.registrar_verificacao(origem, DISCO_INSUFICIENTE, mes, motivo)
        enviar_email(
            f"[Prospecção Receita] PULADA {mes} — espaço em disco insuficiente",
            f"A execução do mês {mes} não foi feita: {motivo}.\n\n"
            "Libere espaço; a próxima verificação diária tenta de novo.",
        )
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
        estado.registrar_verificacao(origem, resultado, mes, detalhe)
    logger.info(f"Execução do mês {mes} terminou: {status or 'sem registro'} (código {codigo})")
    assunto, corpo = montar_aviso(mes, execucao, codigo)
    enviar_email(assunto, corpo)
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


def laco(parar: Event, agora: type[datetime] = datetime) -> None:
    inicio = agora.now()
    cfg = ConfigServico.from_env()
    logger.info(
        f"{NOME_SERVICO} iniciado (verificação diária às {cfg.horario:%H:%M}; "
        f"primeira checagem em {cfg.atraso_inicial.total_seconds() / 60:g} min)"
    )
    while not parar.is_set():
        espera = 60.0
        try:
            recarregar_env()
            cfg = ConfigServico.from_env()
            instante = agora.now()
            if instante - inicio >= cfg.atraso_inicial:
                with EstadoProspeccao() as estado:
                    registros = estado.verificacoes_do_dia(instante.date())
                if verificacao_devida(instante, cfg, registros):
                    verificar("servico", parar)
                    if not parar.is_set():
                        with EstadoProspeccao() as estado:
                            registros = estado.verificacoes_do_dia(agora.now().date())
                        proxima = proxima_verificacao(agora.now(), cfg, registros)
                        logger.info(f"Próxima verificação: {proxima:%Y-%m-%d %H:%M}")
                    limpar_logs_antigos(pasta_logs(), cfg.logs_manter_dias)
        except Exception as e:  # configuração inválida no .env etc.: registra e espera mais
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

    cfg = ConfigServico.from_env()
    agora = datetime.now()
    with EstadoProspeccao() as estado:
        registros = estado.verificacoes_do_dia(agora.date())
        verificacoes = estado.ultimas_verificacoes(5)
        execucoes = estado.conn.execute(
            "SELECT id, iniciada_em, mes_base, status, contagens FROM execucoes ORDER BY id DESC LIMIT 5"
        ).fetchall()
    print(f"{NOME_SERVICO}")
    print(f"  Envio ao GestaoRevenda: {motivo_envio_desligado() or 'LIGADO'}")
    proxima = proxima_verificacao(agora, cfg, registros)
    print(f"  Verificação diária às {cfg.horario:%H:%M}; próxima: {proxima:%Y-%m-%d %H:%M}")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Serviço {NOME_SERVICO}")
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--agora", action="store_true", help="Verifica e roda agora, em primeiro plano")
    modo.add_argument("--status", action="store_true", help="Mostra verificações e execuções recentes")
    modo.add_argument("--testar-email", action="store_true", help="Manda um e-mail de teste")
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

    configurar_log(manual=args.agora or args.testar_email)
    if args.testar_email:
        from notificacao import enviar_email

        ok = enviar_email(
            "[Prospecção Receita] e-mail de teste",
            f"Teste do aviso do serviço {NOME_SERVICO} em {os.getenv('COMPUTERNAME', '?')}.",
        )
        return 0 if ok else 1

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
