"""Configuração da prospecção, editada no GestaoRevenda (Configurações → Prospecção Receita).

O serviço e o rodar_mensal buscam em GET {GR_API_URL}/prospeccao-receita/servico/config com o API
Client do .env (escopo "prospeccao-config", só leitura):

- resposta boa → usada e gravada em estado/config_remota.json (a "última configuração boa");
- GestaoRevenda fora do ar, credencial recusada, resposta inválida → usa esse arquivo;
- nunca conseguiu buscar → valores padrão abaixo, com o envio DESLIGADO.

No .env ficam só GR_API_URL, GR_CLIENT_ID e GR_CLIENT_SECRET. O que não está na tela tem valor fixo
aqui (Parquet, filtro de prospecção ligado, tentativas, retenção de logs).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from datetime import time as hora_do_dia
from pathlib import Path
from typing import Any, cast

from filtro import (
    CNAES_PADRAO,
    MESES_PADRAO,
    SITUACOES_PADRAO,
    UF_BASE_PADRAO,
    FiltroProspeccao,
    normalizar_cnae,
    normalizar_situacao,
)

logger = logging.getLogger(__name__)

ORIGEM_REMOTA = "remota"
ORIGEM_CACHE = "cache"
ORIGEM_PADRAO = "padrao"
ESCOPO_CONFIG = "prospeccao-config"
CAMINHO_API = "/prospeccao-receita/servico/config"

# Primeira execução com envio: todas as ativas da região, independente da abertura mínima da tela.
PRIMEIRA_EXECUCAO_MESES = 0


class ConfigInvalidaError(ValueError):
    """A resposta do GestaoRevenda (ou o arquivo guardado) não é uma configuração válida."""


def caminho_cache() -> Path:
    return Path(os.getenv("PROSPECCAO_ESTADO", "estado/prospeccao.sqlite")).parent / "config_remota.json"


def _hora(texto: str) -> hora_do_dia:
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(texto).strip())
    if not m:
        raise ConfigInvalidaError(f"horário inválido: {texto!r} (esperado HH:MM)")
    return hora_do_dia(int(m[1]), int(m[2]))


@dataclass(frozen=True)
class ConfigProspeccao:
    envio_ativo: bool = False
    cnaes: tuple[str, ...] = CNAES_PADRAO
    uf: str | None = None
    municipios: tuple[str, ...] = ()
    situacoes: tuple[str, ...] = SITUACOES_PADRAO
    abertura_meses: int = MESES_PADRAO
    horario: hora_do_dia = field(default=hora_do_dia(3, 0))
    disco_min_gb: float = 25.0
    versao: int | None = None
    filtro_versao: int | None = None
    origem: str = ORIGEM_PADRAO
    obtida_em: str | None = None
    # Por que a busca mais recente falhou (None = buscou agora com sucesso)
    erro_busca: str | None = None

    @classmethod
    def from_api(cls, dados: Any, origem: str, obtida_em: str | None) -> ConfigProspeccao:
        """Valida a resposta de GET /prospeccao-receita/servico/config."""
        if not isinstance(dados, dict):
            raise ConfigInvalidaError("resposta não é um objeto JSON")
        dados = cast(dict[str, Any], dados)
        try:
            if not isinstance(dados["filtro"], dict):
                raise ConfigInvalidaError("'filtro' não é um objeto")
            filtro = cast(dict[str, Any], dados["filtro"])
            municipios_api = cast(list[Any], filtro.get("municipios") or [])
            cnaes = tuple(str(c) for c in filtro["cnaes"])
            situacoes = tuple(str(s) for s in filtro["situacoes"])
            if not cnaes or not situacoes:
                raise ConfigInvalidaError("CNAEs e situações não podem ficar vazios")
            for c in cnaes:
                normalizar_cnae(c)
            for sit in situacoes:
                normalizar_situacao(sit)
            uf = (filtro.get("uf") or "").strip().upper() or None
            if uf is not None and not re.fullmatch(r"[A-Z]{2}", uf):
                raise ConfigInvalidaError(f"UF inválida: {uf!r}")
            abertura = int(filtro["aberturaMeses"])
            disco = float(dados["discoMinGb"])
            if abertura < 0 or disco <= 0:
                raise ConfigInvalidaError("aberturaMeses/discoMinGb fora da faixa")
            return cls(
                envio_ativo=dados["envioAtivo"] is True,
                cnaes=cnaes,
                uf=uf,
                municipios=tuple(str(m).strip() for m in municipios_api if str(m).strip()),
                situacoes=situacoes,
                abertura_meses=abertura,
                horario=_hora(dados["horario"]),
                disco_min_gb=disco,
                versao=int(dados["versao"]),
                filtro_versao=int(dados["filtroVersao"]),
                origem=origem,
                obtida_em=obtida_em,
            )
        except ConfigInvalidaError:
            raise
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigInvalidaError(f"configuração inválida: {e!r}") from e

    def filtro(self, meses: int | None = None) -> FiltroProspeccao:
        """Filtro da carga/exportação. `meses` = janela de início de atividade efetiva (a 1ª
        execução com envio usa 0); padrão = a abertura mínima da tela."""
        return FiltroProspeccao(
            situacoes={normalizar_situacao(s) for s in self.situacoes},
            cnaes={normalizar_cnae(c) for c in self.cnaes},
            uf=self.uf,
            municipios_config=list(self.municipios),
            meses_inicio_atividade=self.abertura_meses if meses is None else meses,
            tolerar_municipios=True,
        )

    @property
    def uf_base(self) -> str:
        """UF da base rápida (o que a carga mensal guarda): a UF da tela, ou SC."""
        return self.uf or UF_BASE_PADRAO

    def filtro_base(self) -> FiltroProspeccao:
        """Filtro da CARGA: a UF inteira. Só mudar a UF exige baixar o mês de novo."""
        return FiltroProspeccao.base(self.uf_base)

    def chave_filtro(self) -> str:
        """Identifica o filtro do envio automático (CNAE/UF/município/situação/abertura). Mudou →
        o serviço refaz a seleção do mês na próxima verificação programada (sem baixar de novo,
        salvo se a UF mudou)."""
        return self.filtro().assinatura()

    def descrever_origem(self) -> str:
        versao = f"v{self.versao}" if self.versao is not None else "padrão"
        if self.origem == ORIGEM_REMOTA:
            return f"configuração {versao} do GestaoRevenda"
        if self.origem == ORIGEM_CACHE:
            return (
                f"configuração {versao} guardada em {self.obtida_em} (GestaoRevenda não respondeu: {self.erro_busca})"
            )
        return f"valores padrão, envio desligado (nunca obtida do GestaoRevenda: {self.erro_busca})"


def _ler_cache(caminho: Path) -> ConfigProspeccao | None:
    if not caminho.exists():
        return None
    try:
        guardado = json.loads(caminho.read_text(encoding="utf-8"))
        return ConfigProspeccao.from_api(guardado["config"], ORIGEM_CACHE, guardado.get("obtida_em"))
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.warning(f"Configuração guardada em {caminho} ilegível ({e}) — ignorada")
        return None


def _gravar_cache(caminho: Path, dados: dict[str, Any], obtida_em: str) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(".json.partial")
    temporario.write_text(
        json.dumps({"obtida_em": obtida_em, "config": dados}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporario.replace(caminho)  # atômico: nunca deixa um arquivo pela metade


def obter(caminho: Path | None = None, cliente: Any = None) -> ConfigProspeccao:
    """Busca no GestaoRevenda; senão a última boa guardada; senão os padrões (envio desligado).
    Nunca levanta exceção: a origem e o erro ficam em `origem` / `erro_busca`."""
    from enviar_gestaorevenda import ClienteGestaoRevenda, ConfigEnvio, credenciais_faltando

    caminho = caminho or caminho_cache()
    faltando = credenciais_faltando()
    if faltando and cliente is None:
        erro = f"{', '.join(faltando)} vazio no .env"
    else:
        try:
            cliente = cliente or ClienteGestaoRevenda(ConfigEnvio.from_env())
            dados = cliente.requisitar(
                "GET", CAMINHO_API, ESCOPO_CONFIG, tentativas=2, timeout=30, rotulo="Configuração"
            )
            agora = datetime.now().isoformat(timespec="seconds")
            cfg = ConfigProspeccao.from_api(dados, ORIGEM_REMOTA, agora)
            _gravar_cache(caminho, dados, agora)
            return cfg
        except Exception as e:  # rede, credencial, escopo, backend antigo, resposta inválida
            erro = str(e)[:300] or e.__class__.__name__
    guardada = _ler_cache(caminho)
    if guardada is not None:
        logger.warning(f"GestaoRevenda não respondeu a configuração ({erro}) — usando a de {guardada.obtida_em}")
        return replace(guardada, erro_busca=erro)
    logger.warning(f"Configuração nunca obtida do GestaoRevenda ({erro}) — valores padrão, envio desligado")
    return ConfigProspeccao(erro_busca=erro)
