"""Configuração da prospecção vinda da tela do GestaoRevenda: busca, última boa guardada, padrão."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import config_prospeccao as cp
from enviar_gestaorevenda import EnvioError, motivo_envio_desligado

RESPOSTA: dict[str, Any] = {
    "versao": 7,
    "filtroVersao": 3,
    "atualizadoEm": "2026-11-16T10:00:00.000Z",
    "envioAtivo": True,
    "filtro": {
        "cnaes": ["4711-3/02", "4744001"],
        "uf": "sc",
        "municipios": ["JARAGUA DO SUL", " ", "BLUMENAU"],
        "situacoes": ["02"],
        "aberturaMeses": 6,
    },
    "horario": "02:30",
    "discoMinGb": 40,
}


class ClienteFalso:
    def __init__(self, resposta: Any = None, erro: Exception | None = None) -> None:
        self.resposta, self.erro = resposta, erro
        self.chamadas: list[tuple[str, str, str]] = []

    def requisitar(self, metodo: str, caminho: str, escopo: str, *_a: object, **_k: object) -> Any:
        self.chamadas.append((metodo, caminho, escopo))
        if self.erro:
            raise self.erro
        return self.resposta


@pytest.fixture(autouse=True)
def _credenciais(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    for nome, valor in (("GR_API_URL", "http://gr/api"), ("GR_CLIENT_ID", "id"), ("GR_CLIENT_SECRET", "segredo")):
        monkeypatch.setenv(nome, valor)


def test_busca_valida_e_guarda(tmp_path: Path) -> None:
    caminho = tmp_path / "config_remota.json"
    cliente = ClienteFalso(RESPOSTA)
    cfg = cp.obter(caminho, cliente)
    assert cliente.chamadas == [("GET", "/prospeccao-receita/servico/config", "prospeccao-config")]
    assert (cfg.origem, cfg.versao, cfg.filtro_versao, cfg.envio_ativo) == (cp.ORIGEM_REMOTA, 7, 3, True)
    assert cfg.uf == "SC" and cfg.municipios == ("JARAGUA DO SUL", "BLUMENAU")
    assert cfg.horario.strftime("%H:%M") == "02:30" and cfg.disco_min_gb == 40
    filtro = cfg.filtro()
    assert filtro.cnaes == {"4711302", "4744001"} and filtro.meses_inicio_atividade == 6
    assert filtro.tolerar_municipios and cfg.filtro(0).meses_inicio_atividade == 0
    assert json.loads(caminho.read_text(encoding="utf-8"))["config"]["versao"] == 7
    assert motivo_envio_desligado(cfg) is None


def test_gestaorevenda_fora_do_ar_usa_a_ultima_boa(tmp_path: Path) -> None:
    caminho = tmp_path / "config_remota.json"
    cp.obter(caminho, ClienteFalso(RESPOSTA))
    cfg = cp.obter(caminho, ClienteFalso(erro=RuntimeError("Configuração falhou após 2 tentativas: 503")))
    assert cfg.origem == cp.ORIGEM_CACHE and cfg.versao == 7 and cfg.envio_ativo
    assert "503" in (cfg.erro_busca or "") and "guardada" in cfg.descrever_origem()
    # Resposta inválida também não substitui a boa
    cfg = cp.obter(caminho, ClienteFalso({"versao": 8}))
    assert cfg.origem == cp.ORIGEM_CACHE and cfg.versao == 7


def test_nunca_buscou_roda_com_envio_desligado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = cp.obter(tmp_path / "config_remota.json", ClienteFalso(erro=EnvioError("Credenciais recusadas (401)")))
    assert cfg.origem == cp.ORIGEM_PADRAO and not cfg.envio_ativo and cfg.versao is None
    assert cfg.cnaes == cp.CNAES_PADRAO and cfg.situacoes == ("02",)
    motivo = motivo_envio_desligado(cfg)
    assert motivo is not None and "nunca obtida" in motivo and "401" in motivo

    # Sem GR_* no .env: nem tenta buscar
    monkeypatch.delenv("GR_CLIENT_SECRET")
    cfg = cp.obter(tmp_path / "outro.json")
    assert cfg.origem == cp.ORIGEM_PADRAO and "GR_CLIENT_SECRET" in (cfg.erro_busca or "")
    assert "GR_CLIENT_SECRET vazio" in (motivo_envio_desligado(cfg) or "")


def test_envio_desligado_na_tela(tmp_path: Path) -> None:
    cfg = cp.obter(tmp_path / "c.json", ClienteFalso({**RESPOSTA, "envioAtivo": False}))
    assert "na tela do GestaoRevenda" in (motivo_envio_desligado(cfg) or "")


@pytest.mark.parametrize(
    "mudanca",
    [
        {"horario": "3h"},
        {"discoMinGb": 0},
        {"filtro": {**RESPOSTA["filtro"], "cnaes": []}},
        {"filtro": {**RESPOSTA["filtro"], "cnaes": ["47"]}},
        {"filtro": {**RESPOSTA["filtro"], "uf": "Santa Catarina"}},
        {"filtro": {**RESPOSTA["filtro"], "situacoes": ["123"]}},
    ],
)
def test_resposta_invalida(mudanca: dict[str, Any]) -> None:
    with pytest.raises(cp.ConfigInvalidaError):
        cp.ConfigProspeccao.from_api({**RESPOSTA, **mudanca}, cp.ORIGEM_REMOTA, None)


def test_chave_do_filtro_so_muda_com_o_filtro() -> None:
    base = cp.ConfigProspeccao.from_api(RESPOSTA, cp.ORIGEM_REMOTA, None)
    outro_horario = cp.ConfigProspeccao.from_api(
        {**RESPOSTA, "horario": "05:00", "discoMinGb": 99, "envioAtivo": False, "versao": 9}, cp.ORIGEM_REMOTA, None
    )
    outro_municipio = cp.ConfigProspeccao.from_api(
        {**RESPOSTA, "filtro": {**RESPOSTA["filtro"], "municipios": ["JOINVILLE"]}}, cp.ORIGEM_REMOTA, None
    )
    assert base.chave_filtro() == outro_horario.chave_filtro()
    assert base.chave_filtro() != outro_municipio.chave_filtro()
