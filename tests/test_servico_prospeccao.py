"""Serviço de prospecção: agenda diária, disco, decisão de rodar, e-mail. Sem rede e sem subprocesso."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import pytest

import servico_prospeccao as sp
from estado_prospeccao import EstadoProspeccao
from notificacao import ConfigEmail, enviar_email

CFG = sp.ConfigServico()  # 03:00, 3 tentativas, 60 min entre elas


def _dt(texto: str) -> datetime:
    return datetime.fromisoformat(f"2026-11-15T{texto}")


# ── Agenda ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("agora", "registros", "devida"),
    [
        ("02:59", [], False),  # antes do horário
        ("03:00", [], True),
        ("14:00", [], True),  # computador ligado depois das 03:00: roda ao ligar
        ("03:30", [(sp.SEM_NOVIDADE, "03:00")], False),  # já verificado hoje
        ("23:00", [(sp.EXECUTADO, "03:00")], False),
        ("23:00", [(sp.DISCO_INSUFICIENTE, "03:00")], False),  # disco: só amanhã
        ("03:30", [(sp.FALHA_VERIFICACAO, "03:00")], False),  # ainda no intervalo
        ("04:00", [(sp.FALHA_VERIFICACAO, "03:00")], True),
        ("04:00", [(sp.OCUPADO, "03:00")], True),
        ("09:00", [(sp.FALHA_VERIFICACAO, t) for t in ("03:00", "04:00", "05:00")], False),  # 3 tentativas
    ],
)
def test_verificacao_devida(agora: str, registros: list[tuple[str, str]], devida: bool) -> None:
    regs = [(r, _dt(t)) for r, t in registros]
    assert sp.verificacao_devida(_dt(agora), CFG, regs) is devida


def test_proxima_verificacao() -> None:
    assert sp.proxima_verificacao(_dt("01:00"), CFG, []) == _dt("03:00")
    assert sp.proxima_verificacao(_dt("05:00"), CFG, [(sp.SEM_NOVIDADE, _dt("03:00"))]) == _dt("03:00") + timedelta(
        days=1
    )
    assert sp.proxima_verificacao(_dt("03:10"), CFG, [(sp.OCUPADO, _dt("03:00"))]) == _dt("04:00")


def test_config_servico_horario_invalido(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICO_HORARIO", "3h")
    with pytest.raises(ValueError, match="HH:MM"):
        sp.ConfigServico.from_env()
    monkeypatch.setenv("SERVICO_HORARIO", "02:30")
    assert sp.ConfigServico.from_env().horario.strftime("%H:%M") == "02:30"


# ── Disco ────────────────────────────────────────────────────────────────────


class _Uso:
    def __init__(self, free: int) -> None:
        self.free = free


def test_checar_disco(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    temp.mkdir()
    livre = {"valor": 30 * sp.GB}

    def uso(_p: object) -> _Uso:
        return _Uso(livre["valor"])

    monkeypatch.setattr(sp.shutil, "disk_usage", uso)
    assert sp.checar_disco(25, temp, [tmp_path / "parquet"]) is None

    livre["valor"] = 10 * sp.GB
    motivo = sp.checar_disco(25, temp, [])
    assert motivo is not None and "10.0 GB livres" in motivo and "25 GB" in motivo

    # O que já está em temp/ (downloads a retomar) conta como disponível
    def dezesseis_gb(_p: object) -> int:
        return 16 * sp.GB

    monkeypatch.setattr(sp, "tamanho_pasta", dezesseis_gb)
    assert sp.checar_disco(25, temp, []) is None


# ── Verificação ──────────────────────────────────────────────────────────────


def _sem_dotenv(*_a: object, **_k: object) -> bool:
    return False  # não ler o .env real do projeto


@pytest.fixture
def ambiente(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Estado em tmp, Receita e subprocesso falsos, e-mails capturados."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sp, "recarregar_env", lambda: None)
    monkeypatch.setattr("config.load_dotenv", _sem_dotenv)
    monkeypatch.setenv("PROSPECCAO_ESTADO", str(tmp_path / "estado.sqlite"))
    monkeypatch.setenv("PROSPECCAO_LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("OUTPUT_FORMAT", "parquet")
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "temp"))
    monkeypatch.setenv("SERVICO_ENVIO_ATIVO", "false")
    ctx: dict[str, Any] = {"mes": "2026-11", "emails": [], "execucoes": [], "status_filho": "SEM_ENVIO", "codigo": 0}

    def mes_mais_recente(_self: object) -> str:
        return ctx["mes"]

    def checar_disco(*_a: object) -> str | None:
        return ctx.get("disco")

    def enviar_email(assunto: str, corpo: str) -> bool:
        ctx["emails"].append((assunto, corpo))
        return True

    monkeypatch.setattr("downloader.Downloader.get_latest_directory", mes_mais_recente)
    monkeypatch.setattr(sp, "checar_disco", checar_disco)
    monkeypatch.setattr("notificacao.enviar_email", enviar_email)

    def rodar_fluxo(mes: str, _parar: Event, **kwargs: Any) -> int:
        ctx["execucoes"].append((mes, kwargs))
        if ctx["status_filho"] is not None:
            with EstadoProspeccao() as estado:
                execucao_id = estado.iniciar_execucao(mes, "novos")
                contagens: dict[str, Any] = {
                    "mes_base": mes,
                    "linhas_lidas": {"estabelecimentos": 100},
                    "linhas_filtradas": {"estabelecimentos": 10},
                    "novas": 7,
                    "envio": {"enviadas": 7, "importados": 5, "duplicados": 1, "bloqueados": 0, "erros": 1},
                }
                if ctx["status_filho"] == "SEM_ENVIO":
                    contagens["envio"] = None
                    contagens["envio_desligado"] = "envio desligado (SERVICO_ENVIO_ATIVO=false no .env)"
                if ctx["status_filho"] == "FALHOU":
                    contagens["erro"] = "Lote recusado (400)"
                estado.finalizar_execucao(execucao_id, ctx["status_filho"], contagens)
        return ctx["codigo"]

    monkeypatch.setattr(sp, "rodar_fluxo", rodar_fluxo)
    return ctx


def test_verificar_envio_desligado_roda_uma_vez_por_mes(ambiente: dict[str, Any]) -> None:
    assert sp.verificar("servico", Event()) == sp.EXECUTADO
    assert len(ambiente["execucoes"]) == 1
    assunto, corpo = ambiente["emails"][0]
    assert "envio desligado" in assunto and "2026-11" in assunto
    assert "ENVIO DESLIGADO" in corpo and "Linhas lidas da Receita:   100" in corpo

    # Dia seguinte, mesmo mês, envio ainda desligado: nada a fazer, sem e-mail
    assert sp.verificar("servico", Event()) == sp.SEM_NOVIDADE
    assert len(ambiente["execucoes"]) == 1 and len(ambiente["emails"]) == 1


def test_verificar_ligar_envio_reabre_o_mes(ambiente: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    assert sp.verificar("servico", Event()) == sp.EXECUTADO  # SEM_ENVIO
    monkeypatch.setenv("SERVICO_ENVIO_ATIVO", "true")
    monkeypatch.setenv("GR_API_URL", "http://gr/api")
    monkeypatch.setenv("GR_CLIENT_ID", "id")
    monkeypatch.setenv("GR_CLIENT_SECRET", "segredo")
    ambiente["status_filho"] = "OK"
    assert sp.verificar("servico", Event()) == sp.EXECUTADO
    assert len(ambiente["execucoes"]) == 2
    assunto, corpo = ambiente["emails"][-1]
    assert assunto.startswith("[Prospecção Receita] OK 2026-11 — 7 novas, 7 enviadas")
    assert "recusados (erro):        1" in corpo and "duplicados:              1" in corpo
    # Agora o mês está concluído mesmo com envio ligado
    assert sp.verificar("servico", Event()) == sp.SEM_NOVIDADE


def test_verificar_falha_avisa_e_conta_como_verificacao_do_dia(ambiente: dict[str, Any]) -> None:
    ambiente["status_filho"] = "FALHOU"
    ambiente["codigo"] = 1
    assert sp.verificar("servico", Event()) == sp.FALHOU
    assunto, corpo = ambiente["emails"][0]
    assert "FALHOU 2026-11 — Lote recusado (400)" in assunto and "Erro: Lote recusado (400)" in corpo
    with EstadoProspeccao() as estado:
        regs = estado.verificacoes_do_dia(datetime.now().date())
    assert [r for r, _ in regs] == [sp.FALHOU]
    assert not sp.verificacao_devida(datetime.combine(datetime.now().date(), CFG.horario), CFG, regs)


def test_verificar_disco_insuficiente_pula_e_avisa(ambiente: dict[str, Any]) -> None:
    ambiente["disco"] = "espaço insuficiente em C:: 3.0 GB livres; mínimo 25 GB (SERVICO_DISCO_MIN_GB)"
    assert sp.verificar("servico", Event()) == sp.DISCO_INSUFICIENTE
    assert ambiente["execucoes"] == []
    assert "PULADA 2026-11" in ambiente["emails"][0][0] and "3.0 GB livres" in ambiente["emails"][0][1]


def test_verificar_ocupado_e_interrompido_nao_avisam(ambiente: dict[str, Any]) -> None:
    ambiente["status_filho"] = None
    ambiente["codigo"] = sp.CODIGO_OCUPADO
    assert sp.verificar("servico", Event()) == sp.OCUPADO

    ambiente["codigo"] = sp.CODIGO_INTERROMPIDA
    assert sp.verificar("servico", Event()) == sp.INTERROMPIDO
    assert ambiente["emails"] == []
    with EstadoProspeccao() as estado:
        regs = estado.verificacoes_do_dia(datetime.now().date())
    assert [r for r, _ in regs] == [sp.OCUPADO]  # interrompido não registra: continua devida


def test_verificar_receita_fora_do_ar(ambiente: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def falha(_self: object) -> str:
        raise ConnectionError("Receita fora do ar")

    monkeypatch.setattr("downloader.Downloader.get_latest_directory", falha)
    assert sp.verificar("servico", Event()) == sp.FALHA_VERIFICACAO
    assert ambiente["execucoes"] == [] and ambiente["emails"] == []


def test_verificar_forcar_roda_mes_ja_processado(ambiente: dict[str, Any]) -> None:
    assert sp.verificar("manual", Event()) == sp.EXECUTADO
    assert sp.verificar("manual", Event(), forcar=True, limite=20) == sp.EXECUTADO
    assert ambiente["execucoes"][-1] == (
        "2026-11",
        {"sem_envio": False, "limite": 20, "recarregar": False, "console": False},
    )


def test_estado_marca_execucoes_orfas(tmp_path: Path) -> None:
    with EstadoProspeccao(tmp_path / "e.sqlite") as estado:
        orfa = estado.iniciar_execucao("2026-10", "novos")  # processo morreu sem finalizar
        assert estado.marcar_interrompidas() == 1
        execucao = estado.execucao(orfa)
        assert execucao is not None and execucao["status"] == "INTERROMPIDA"
        assert not estado.mes_concluido("2026-10", aceitar_sem_envio=True)


def test_limpar_logs_antigos(tmp_path: Path) -> None:
    import os

    velho = tmp_path / "rodar_mensal_2025-01-01_030000.log"
    novo = tmp_path / "rodar_mensal_2026-11-01_030000.log"
    outro = tmp_path / "servico.log"
    for f in (velho, novo, outro):
        f.write_text("x")
    antigo = (datetime.now() - timedelta(days=400)).timestamp()
    os.utime(velho, (antigo, antigo))
    os.utime(outro, (antigo, antigo))
    assert sp.limpar_logs_antigos(tmp_path, 365) == 1
    assert not velho.exists() and novo.exists() and outro.exists()


# ── E-mail ───────────────────────────────────────────────────────────────────


class FakeSMTP:
    instancias: list[FakeSMTP] = []

    def __init__(self, host: str, porta: int, **kwargs: Any) -> None:
        self.host, self.porta, self.kwargs = host, porta, kwargs
        self.chamadas: list[str] = []
        self.mensagens: list[Any] = []
        FakeSMTP.instancias.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.chamadas.append("quit")

    def starttls(self, **kwargs: Any) -> None:
        self.chamadas.append("starttls")

    def login(self, usuario: str, senha: str) -> None:
        if senha != "certa":
            raise OSError("535 autenticação recusada")
        self.chamadas.append(f"login:{usuario}")

    def send_message(self, msg: Any) -> None:
        self.mensagens.append(msg)


def _cfg_email(senha: str = "certa") -> ConfigEmail:
    return ConfigEmail("smtp.exemplo", 587, "starttls", "eu@exemplo.com", senha, "eu@exemplo.com", ["a@x", "b@x"])


def test_enviar_email_starttls() -> None:
    FakeSMTP.instancias.clear()
    assert enviar_email("Assunto ç", "Corpo ã", _cfg_email(), smtp_factory=FakeSMTP)
    smtp = FakeSMTP.instancias[0]
    assert (smtp.host, smtp.porta) == ("smtp.exemplo", 587)
    assert smtp.chamadas == ["starttls", "login:eu@exemplo.com", "quit"]
    msg = smtp.mensagens[0]
    assert msg["To"] == "a@x, b@x" and msg["Subject"] == "Assunto ç" and "Corpo ã" in msg.get_content()


def test_enviar_email_falha_nao_levanta() -> None:
    assert enviar_email("x", "y", _cfg_email(senha="errada"), smtp_factory=FakeSMTP) is False


def test_config_email_do_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for nome in ("SMTP_HOST", "EMAIL_PARA", "EMAIL_DE", "SMTP_PORTA", "SMTP_SEGURANCA"):
        monkeypatch.delenv(nome, raising=False)
    assert ConfigEmail.from_env() is None  # não configurado: só log
    assert enviar_email("x", "y") is False
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USUARIO", "eu@gmail.com")
    monkeypatch.setenv("EMAIL_PARA", "eu@gmail.com, outro@x")
    cfg = ConfigEmail.from_env()
    assert cfg is not None
    assert (cfg.porta, cfg.seguranca, cfg.remetente) == (587, "starttls", "eu@gmail.com")
    assert cfg.destinatarios == ["eu@gmail.com", "outro@x"]
    monkeypatch.setenv("SMTP_SEGURANCA", "tls")
    with pytest.raises(ValueError, match="SMTP_SEGURANCA"):
        ConfigEmail.from_env()
