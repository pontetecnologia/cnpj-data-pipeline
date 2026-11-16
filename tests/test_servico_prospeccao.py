"""Serviço de prospecção: agenda diária, disco, decisão de rodar, aviso ao GestaoRevenda.
Sem rede e sem subprocesso."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from datetime import time as hora_do_dia
from pathlib import Path
from threading import Event
from typing import Any

import pytest

import aviso_gestaorevenda as aviso
import servico_prospeccao as sp
from config_prospeccao import ORIGEM_REMOTA, ConfigProspeccao
from estado_prospeccao import EstadoProspeccao

CFG = sp.ConfigServico()  # 03:00, 3 tentativas, 60 min entre elas
REMOTA = ConfigProspeccao(
    envio_ativo=False, uf="SC", municipios=("JOINVILLE",), versao=3, filtro_versao=2, origem=ORIGEM_REMOTA
)


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


def test_config_servico_vem_da_tela() -> None:
    servico = sp.ConfigServico.de(replace(REMOTA, horario=hora_do_dia(2, 30), disco_min_gb=40))
    assert servico.horario.strftime("%H:%M") == "02:30" and servico.disco_min_gb == 40
    assert servico.tentativas_dia == 3 and servico.atraso_inicial == timedelta(minutes=10)  # fixos


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
    """Estado em tmp, Receita, subprocesso e GestaoRevenda falsos (o que iria para ele é capturado)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sp, "recarregar_env", lambda: None)
    monkeypatch.setattr("config.load_dotenv", _sem_dotenv)
    monkeypatch.setenv("PROSPECCAO_ESTADO", str(tmp_path / "estado.sqlite"))
    monkeypatch.setenv("PROSPECCAO_LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "temp"))
    for nome, valor in (("GR_API_URL", "http://gr/api"), ("GR_CLIENT_ID", "id"), ("GR_CLIENT_SECRET", "segredo")):
        monkeypatch.setenv(nome, valor)
    ctx: dict[str, Any] = {
        "mes": "2026-11",
        "cfg": REMOTA,
        "execucoes": [],
        "status_filho": "SEM_ENVIO",
        "codigo": 0,
        "gr": [],  # (tipo, payload) entregues ao GestaoRevenda
    }

    def mes_mais_recente(_self: object) -> str:
        return ctx["mes"]

    def checar_disco(*_a: object) -> str | None:
        return ctx.get("disco")

    def obter() -> ConfigProspeccao:
        return ctx["cfg"]

    def enviar_fila(estado: EstadoProspeccao, cliente: Any = None) -> tuple[int, int]:
        itens = estado.pendentes_fila()
        for item_id, tipo, _chave, payload in itens:
            ctx["gr"].append((tipo, payload))
            estado.remover_da_fila(item_id)
        return len(itens), 0

    monkeypatch.setattr("downloader.Downloader.get_latest_directory", mes_mais_recente)
    monkeypatch.setattr(sp, "checar_disco", checar_disco)
    monkeypatch.setattr(sp.config_prospeccao, "obter", obter)
    monkeypatch.setattr(sp.aviso, "enviar_fila", enviar_fila)

    def rodar_fluxo(mes: str, _parar: Event, **kwargs: Any) -> int:
        ctx["execucoes"].append((mes, kwargs))
        if ctx["status_filho"] is not None:
            cfg: ConfigProspeccao = ctx["cfg"]
            with EstadoProspeccao() as estado:
                execucao_id = estado.iniciar_execucao(mes, "novos")
                contagens: dict[str, Any] = {
                    "mes_base": mes,
                    "linhas_lidas": {"estabelecimentos": 100},
                    "linhas_filtradas": {"estabelecimentos": 10},
                    "exportadas": 9,
                    "novas": 7,
                    "selecionadas": 7,
                    "a_enviar": 7,
                    "envio": {"enviadas": 7, "importados": 5, "duplicados": 1, "bloqueados": 0, "erros": 1},
                    "config_versao": cfg.versao,
                    "filtro_versao": cfg.filtro_versao,
                    "origem_config": cfg.origem,
                    "filtro_chave": cfg.chave_filtro(),
                    "municipios_nao_reconhecidos": ["CIDADE X"],
                }
                if ctx["status_filho"] == "SEM_ENVIO":
                    contagens["envio"] = None
                    contagens["envio_desligado"] = "envio desligado na tela do GestaoRevenda"
                if ctx["status_filho"] == "FALHOU":
                    contagens["erro"] = "Lote recusado (400)"
                estado.finalizar_execucao(execucao_id, ctx["status_filho"], contagens)
        return ctx["codigo"]

    monkeypatch.setattr(sp, "rodar_fluxo", rodar_fluxo)
    return ctx


def _entregues(ctx: dict[str, Any], tipo: str) -> list[dict[str, Any]]:
    return [p for t, p in ctx["gr"] if t == tipo]


def test_verificar_envio_desligado_roda_uma_vez_por_mes(ambiente: dict[str, Any]) -> None:
    assert sp.verificar("servico", Event()) == sp.EXECUTADO
    assert len(ambiente["execucoes"]) == 1
    [execucao] = _entregues(ambiente, "execucao")
    assert execucao["status"] == "SEM_ENVIO" and execucao["mesBase"] == "2026-11"
    assert "na tela" in execucao["envioDesligado"]
    assert (execucao["lidas"], execucao["filtradas"], execucao["novas"], execucao["enviadas"]) == (100, 10, 7, 0)
    assert execucao["configVersao"] == 3 and execucao["municipiosNaoReconhecidos"] == ["CIDADE X"]
    assert execucao["chave"].startswith("exec:")
    assert execucao["terminadaEm"][19:] != ""  # com fuso: o GestaoRevenda não desloca 3 horas
    [sinal] = _entregues(ambiente, "sinal")
    # A próxima execução com envio mandaria os 7 que a execução sem envio selecionou
    assert sinal["pendentesEnvio"] == 7 and sinal["pendentesMes"] == "2026-11" and sinal["pendentesFiltroVersao"] == 2
    assert sinal["configVersaoAplicada"] == 3 and sinal["ultimaVerificacao"]["resultado"] == sp.EXECUTADO

    # Dia seguinte, mesmo mês, envio ainda desligado: nada a fazer; só o sinal vai
    ambiente["gr"].clear()
    assert sp.verificar("servico", Event()) == sp.SEM_NOVIDADE
    assert len(ambiente["execucoes"]) == 1
    assert _entregues(ambiente, "execucao") == []
    assert _entregues(ambiente, "sinal")[0]["ultimaVerificacao"]["resultado"] == sp.SEM_NOVIDADE


def test_verificar_ligar_envio_reabre_o_mes(ambiente: dict[str, Any]) -> None:
    assert sp.verificar("servico", Event()) == sp.EXECUTADO  # SEM_ENVIO
    ambiente["cfg"] = replace(REMOTA, envio_ativo=True, versao=4)
    ambiente["status_filho"] = "OK"
    assert sp.verificar("servico", Event()) == sp.EXECUTADO
    assert len(ambiente["execucoes"]) == 2
    execucao = _entregues(ambiente, "execucao")[-1]
    assert execucao["status"] == "OK" and execucao["envioDesligado"] is None
    assert (execucao["enviadas"], execucao["importadas"], execucao["duplicadas"], execucao["recusadas"]) == (7, 5, 1, 1)
    assert _entregues(ambiente, "sinal")[-1]["pendentesEnvio"] == 0
    # Agora o mês está concluído mesmo com envio ligado
    assert sp.verificar("servico", Event()) == sp.SEM_NOVIDADE


def test_verificar_filtro_alterado_recarrega_na_proxima_verificacao(ambiente: dict[str, Any]) -> None:
    assert sp.verificar("servico", Event()) == sp.EXECUTADO
    assert ambiente["execucoes"][-1][1]["recarregar"] is False
    # Mudou o município na tela: o mês já processado volta a rodar, baixando e carregando de novo
    ambiente["cfg"] = replace(REMOTA, municipios=("JOINVILLE", "BLUMENAU"), versao=4, filtro_versao=3)
    assert sp.verificar("servico", Event()) == sp.EXECUTADO
    assert ambiente["execucoes"][-1][1]["recarregar"] is True
    # Já recarregado com o filtro novo: nada a fazer
    assert sp.verificar("servico", Event()) == sp.SEM_NOVIDADE
    # Mudar só o horário ou o disco não muda o filtro
    ambiente["cfg"] = replace(ambiente["cfg"], horario=hora_do_dia(4, 0), disco_min_gb=30, versao=5)
    assert sp.verificar("servico", Event()) == sp.SEM_NOVIDADE


def test_verificar_falha_avisa_e_conta_como_verificacao_do_dia(ambiente: dict[str, Any]) -> None:
    ambiente["status_filho"] = "FALHOU"
    ambiente["codigo"] = 1
    assert sp.verificar("servico", Event()) == sp.FALHOU
    [execucao] = _entregues(ambiente, "execucao")
    assert execucao["status"] == "FALHOU" and execucao["erro"] == "Lote recusado (400)"
    with EstadoProspeccao() as estado:
        regs = estado.verificacoes_do_dia(datetime.now().date())
    assert [r for r, _ in regs] == [sp.FALHOU]
    assert not sp.verificacao_devida(datetime.combine(datetime.now().date(), CFG.horario), CFG, regs)


def test_verificar_subprocesso_sem_registro_avisa_falha(ambiente: dict[str, Any]) -> None:
    ambiente["status_filho"] = None
    ambiente["codigo"] = 1
    assert sp.verificar("servico", Event()) == sp.FALHOU
    [execucao] = _entregues(ambiente, "execucao")
    assert execucao["status"] == "FALHOU" and "sem registrar" in execucao["erro"]
    assert execucao["chave"].startswith("erro:")


def test_verificar_disco_insuficiente_pula_e_avisa(ambiente: dict[str, Any]) -> None:
    ambiente["disco"] = "espaço insuficiente em C:: 3.0 GB livres; mínimo 25 GB"
    assert sp.verificar("servico", Event()) == sp.DISCO_INSUFICIENTE
    assert ambiente["execucoes"] == []
    [execucao] = _entregues(ambiente, "execucao")
    assert execucao["status"] == "DISCO_INSUFICIENTE" and "3.0 GB livres" in execucao["erro"]
    assert execucao["chave"].startswith("pulada:")


def test_verificar_ocupado_e_interrompido_nao_avisam(ambiente: dict[str, Any]) -> None:
    ambiente["status_filho"] = None
    ambiente["codigo"] = sp.CODIGO_OCUPADO
    assert sp.verificar("servico", Event()) == sp.OCUPADO

    ambiente["codigo"] = sp.CODIGO_INTERROMPIDA
    assert sp.verificar("servico", Event()) == sp.INTERROMPIDO
    assert _entregues(ambiente, "execucao") == []
    with EstadoProspeccao() as estado:
        regs = estado.verificacoes_do_dia(datetime.now().date())
    assert [r for r, _ in regs] == [sp.OCUPADO]  # interrompido não registra: continua devida


def test_verificar_receita_fora_do_ar(ambiente: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def falha(_self: object) -> str:
        raise ConnectionError("Receita fora do ar")

    monkeypatch.setattr("downloader.Downloader.get_latest_directory", falha)
    assert sp.verificar("servico", Event()) == sp.FALHA_VERIFICACAO
    assert ambiente["execucoes"] == [] and _entregues(ambiente, "execucao") == []
    assert _entregues(ambiente, "sinal")[0]["ultimaVerificacao"]["resultado"] == sp.FALHA_VERIFICACAO


def test_verificar_erro_inesperado_vira_falha_avisada(
    ambiente: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_a: object, **_k: object) -> str:
        raise RuntimeError("disco sumiu")

    monkeypatch.setattr(sp, "checar_disco", explode)
    assert sp.verificar("servico", Event()) == sp.FALHOU
    [execucao] = _entregues(ambiente, "execucao")
    assert execucao["status"] == "FALHOU" and "disco sumiu" in execucao["erro"]


def test_verificar_forcar_roda_mes_ja_processado(ambiente: dict[str, Any]) -> None:
    assert sp.verificar("manual", Event()) == sp.EXECUTADO
    assert sp.verificar("manual", Event(), forcar=True, limite=20) == sp.EXECUTADO
    assert ambiente["execucoes"][-1] == (
        "2026-11",
        {"sem_envio": False, "limite": 20, "recarregar": False, "console": False},
    )
    assert _entregues(ambiente, "execucao")[-1]["origem"] == "manual"


def test_pendentes_de_envio_por_status() -> None:
    def execucao(status: str, **c: Any) -> dict[str, Any]:
        return {"status": status, "contagens": c}

    assert aviso.a_enviar_pendentes(execucao("SEM_ENVIO", a_enviar=7174)) == 7174
    assert aviso.a_enviar_pendentes(execucao("PARCIAL", a_enviar=7174, envio={"enviadas": 20})) == 7154
    assert aviso.a_enviar_pendentes(execucao("OK", a_enviar=7154, envio={"enviadas": 7154})) == 0
    assert aviso.a_enviar_pendentes(execucao("SEM_ENVIO")) is None  # execução antiga, sem a contagem


def test_estado_marca_execucoes_orfas(tmp_path: Path) -> None:
    with EstadoProspeccao(tmp_path / "e.sqlite") as estado:
        orfa = estado.iniciar_execucao("2026-10", "novos")  # processo morreu sem finalizar
        assert estado.marcar_interrompidas() == 1
        execucao = estado.execucao(orfa)
        assert execucao is not None and execucao["status"] == "INTERROMPIDA"
        assert not estado.mes_concluido("2026-10", aceitar_sem_envio=True)


def test_estado_fila_e_instancia(tmp_path: Path) -> None:
    with EstadoProspeccao(tmp_path / "e.sqlite") as estado:
        instancia = estado.instancia()
        assert instancia == estado.instancia() and len(instancia) == 12
        estado.enfileirar("sinal", "sinal", {"n": 1})
        estado.enfileirar("execucao", "exec:a:1", {"x": 1})
        estado.enfileirar("sinal", "sinal", {"n": 2})  # o sinal guarda só o mais recente
        itens = estado.pendentes_fila()
        assert [(t, p) for _, t, _, p in itens] == [("execucao", {"x": 1}), ("sinal", {"n": 2})]
        estado.remover_da_fila(itens[0][0])
        assert estado.tamanho_fila() == 1


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
