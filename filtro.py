"""Filtro de prospecção aplicado durante a carga.

O serviço e o rodar_mensal carregam a "base rápida" com FiltroProspeccao.base(uf): todos os
estabelecimentos da UF, de qualquer situação, CNAE e data. A tela local e o envio automático
filtram essa base depois, por consulta (DuckDB), sem baixar de novo. Rodando o pipeline original
(main.py) direto, o filtro liga com PROSPECCAO_FILTRO=true e lê FILTRO_* (from_env). Com ele ligado:

- estabelecimentos são filtrados por situação cadastral, CNAE principal, UF, município e data
  mínima de início de atividade (situações ou CNAEs vazios = todos);
- empresas e dados_simples são carregados só para os CNPJs básicos que sobraram em
  estabelecimentos (por isso estabelecimentos passa a ser processado ANTES deles);
- sócios não são baixados nem carregados.

No main.py sem PROSPECCAO_FILTRO, nada neste módulo é usado e o pipeline se comporta exatamente
como o upstream.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date

import polars as pl

logger = logging.getLogger(__name__)

# Varejo alimentar, materiais de construção, autopeças (motos) e pet — ver prompt-pipeline-receita.md
CNAES_PADRAO = ("4711-3/02", "4712-1/00", "4711-3/01", "4744-0/99", "4744-0/01", "4530-7/03", "4789-0/04")
SITUACOES_PADRAO = ("02",)  # 02 = ativa
MESES_PADRAO = 12
UF_BASE_PADRAO = "SC"  # UF da base rápida quando a tela do GestaoRevenda não define uma

# Tipos de arquivo afetados pelo filtro
TIPO_ESTABELECIMENTOS = "ESTABELE"
TIPOS_POR_CNPJ_BASICO = ("EMPRECSV", "SIMPLESCSV")
TIPOS_IGNORADOS = ("SOCIOCSV",)


class FiltroInvalidoError(ValueError):
    """Configuração do filtro inválida (CNAE malformado, município desconhecido ou ambíguo...)."""


def _lista_env(nome: str, padrao: Iterable[str] = ()) -> list[str]:
    valor = os.getenv(nome)
    if valor is None:
        return list(padrao)
    # Vírgula ou ponto e vírgula; ponto e vírgula permite nomes de município com vírgula (raro)
    return [p.strip() for p in re.split(r"[;,]", valor) if p.strip()]


def normalizar_cnae(valor: str) -> str:
    """'4711-3/02' / '4711302' / '4711.3-02' → '4711302'."""
    digitos = re.sub(r"\D", "", valor)
    if len(digitos) != 7:
        raise FiltroInvalidoError(f"CNAE inválido: {valor!r} (esperado 7 dígitos, ex.: 4711-3/02)")
    return digitos


def normalizar_situacao(valor: str) -> str:
    digitos = re.sub(r"\D", "", valor)
    if not digitos or len(digitos) > 2:
        raise FiltroInvalidoError(f"Situação cadastral inválida: {valor!r} (ex.: 02)")
    return digitos.zfill(2)


def normalizar_nome(nome: str) -> str:
    """Comparação de nomes de município: sem acento, maiúsculas, espaços simples, sem apóstrofo/hífen."""
    sem_acento = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode("ascii")
    sem_pontuacao = re.sub(r"['`´\-]", " ", sem_acento.upper())
    return re.sub(r"\s+", " ", sem_pontuacao).strip()


def subtrair_meses(base: date, meses: int) -> date:
    ano, mes = divmod(base.year * 12 + (base.month - 1) - meses, 12)
    mes += 1
    # Dia 31 em mês curto → último dia do mês
    for dia in (base.day, 30, 29, 28):
        try:
            return date(ano, mes, dia)
        except ValueError:
            continue
    raise AssertionError("inalcançável")


@dataclass
class FiltroProspeccao:
    situacoes: set[str] = field(default_factory=lambda: set(SITUACOES_PADRAO))
    cnaes: set[str] = field(default_factory=lambda: {normalizar_cnae(c) for c in CNAES_PADRAO})
    uf: str | None = None
    # Como veio da configuração (códigos e/ou nomes). Resolvido para `municipios` em resolver_municipios.
    municipios_config: list[str] = field(default_factory=list)
    municipios: set[str] | None = None
    meses_inicio_atividade: int = MESES_PADRAO
    hoje: date = field(default_factory=date.today)
    # CNPJs básicos que passaram no filtro de estabelecimentos (preenchido após carregar estabelecimentos)
    cnpjs_basicos: set[str] | None = None
    # Configuração vinda da tela do GestaoRevenda: município que não está na tabela da Receita (ou
    # é ambíguo) fica de fora e é listado aqui, em vez de derrubar a execução. Se NENHUM for
    # reconhecido, ainda é erro (senão o filtro viraria "a UF inteira").
    tolerar_municipios: bool = False
    municipios_nao_reconhecidos: list[str] = field(default_factory=list)
    _basicos: pl.DataFrame | None = field(default=None, repr=False, compare=False)

    # ── Configuração ──────────────────────────────────────────────────────────

    @classmethod
    def base(cls, uf: str) -> FiltroProspeccao:
        """Base rápida: a UF inteira, todas as situações, CNAEs e datas."""
        return cls(situacoes=set(), cnaes=set(), uf=uf.strip().upper(), meses_inicio_atividade=0)

    @classmethod
    def from_env(cls) -> FiltroProspeccao | None:
        """None quando PROSPECCAO_FILTRO não está ligado (comportamento original do pipeline)."""
        if os.getenv("PROSPECCAO_FILTRO", "false").strip().lower() not in ("1", "true", "sim", "yes"):
            return None

        meses_txt = os.getenv("FILTRO_INICIO_ATIVIDADE_MESES", str(MESES_PADRAO)).strip()
        try:
            meses = int(meses_txt)
        except ValueError as e:
            raise FiltroInvalidoError(f"FILTRO_INICIO_ATIVIDADE_MESES inválido: {meses_txt!r}") from e
        if meses < 0:
            raise FiltroInvalidoError("FILTRO_INICIO_ATIVIDADE_MESES não pode ser negativo (0 desliga)")

        uf = os.getenv("FILTRO_UF", "").strip().upper() or None
        if uf is not None and not re.fullmatch(r"[A-Z]{2}", uf):
            raise FiltroInvalidoError(f"FILTRO_UF inválida: {uf!r} (ex.: SC)")

        situacoes = {normalizar_situacao(s) for s in _lista_env("FILTRO_SITUACAO", SITUACOES_PADRAO)}
        cnaes = {normalizar_cnae(c) for c in _lista_env("FILTRO_CNAES", CNAES_PADRAO)}
        return cls(
            situacoes=situacoes,
            cnaes=cnaes,
            uf=uf,
            municipios_config=_lista_env("FILTRO_MUNICIPIOS"),
            meses_inicio_atividade=meses,
        )

    @property
    def data_inicio_minima(self) -> date | None:
        if self.meses_inicio_atividade == 0:
            return None
        return subtrair_meses(self.hoje, self.meses_inicio_atividade)

    @property
    def precisa_tabela_municipios(self) -> bool:
        return any(not m.isdigit() for m in self.municipios_config)

    def resolver_municipios(self, tabela: Mapping[str, str] | None = None) -> None:
        """Converte FILTRO_MUNICIPIOS (código da Receita ou nome) em códigos.

        `tabela` é {codigo: descricao} da tabela de referência `municipios` da Receita (o código da
        Receita NÃO é o do IBGE). A tabela da Receita não tem UF, então um nome que existe em mais
        de um estado (ex.: "BOM JESUS") só é aceito com FILTRO_UF definido — o filtro de UF é que
        desambigua; sem UF, é erro.
        """
        if not self.municipios_config:
            self.municipios = None
            return

        codigos: set[str] = set()
        self.municipios_nao_reconhecidos = []
        por_nome: dict[str, list[str]] = {}
        if self.precisa_tabela_municipios:
            if not tabela:
                raise FiltroInvalidoError("Tabela de municípios vazia — não dá para converter nome em código")
            for codigo, descricao in tabela.items():
                por_nome.setdefault(normalizar_nome(descricao or ""), []).append(str(codigo))

        for item in self.municipios_config:
            if item.isdigit():
                codigos.add(item.zfill(4))
                continue
            encontrados = por_nome.get(normalizar_nome(item), [])
            erro = None
            if not encontrados:
                erro = (
                    f"Município não encontrado na tabela da Receita: {item!r}. "
                    "Use o nome como a Receita grafa (ex.: JOINVILLE) ou o código da Receita."
                )
            elif len(encontrados) > 1 and self.uf is None:
                erro = (
                    f"Município {item!r} existe em mais de um estado (códigos {', '.join(sorted(encontrados))}). "
                    "Defina a UF ou use o código da Receita."
                )
            if erro:
                if not self.tolerar_municipios:
                    raise FiltroInvalidoError(erro)
                logger.warning(f"{erro} — fica fora do filtro")
                self.municipios_nao_reconhecidos.append(item)
                continue
            codigos.update(encontrados)

        if not codigos:
            raise FiltroInvalidoError(
                f"Nenhum dos municípios do filtro foi reconhecido ({', '.join(self.municipios_config)}): "
                "corrija os nomes em Configurações → Prospecção Receita"
            )
        self.municipios = codigos
        logger.info(f"Filtro: municípios {', '.join(self.municipios_config)} → códigos {', '.join(sorted(codigos))}")

    def assinatura(self) -> str:
        """Identifica a configuração do filtro (sem a data de hoje, para retomar no dia seguinte)."""
        if not (self.situacoes or self.cnaes or self.municipios_config or self.meses_inicio_atividade):
            return f"base;uf={self.uf or ''}"
        return (
            f"situacao={','.join(sorted(self.situacoes))};cnaes={','.join(sorted(self.cnaes))};"
            f"uf={self.uf or ''};municipios={','.join(sorted(normalizar_nome(m) for m in self.municipios_config))};"
            f"meses={self.meses_inicio_atividade}"
        )

    def descrever(self) -> str:
        data_min = self.data_inicio_minima
        return (
            f"situação={','.join(sorted(self.situacoes)) or 'todas'}; "
            f"CNAEs={','.join(sorted(self.cnaes)) or 'todos'}; "
            f"UF={self.uf or 'todas'}; "
            f"municípios={','.join(self.municipios_config) or 'todos'}; "
            f"início de atividade >= {data_min.isoformat() if data_min else 'sem limite'}"
        )

    # ── Aplicação ─────────────────────────────────────────────────────────────

    def ignora_tipo(self, file_type: str | None) -> bool:
        return file_type in TIPOS_IGNORADOS

    def expressao_estabelecimentos(self, dtype_data: pl.DataType | None = None) -> pl.Expr:
        expr = pl.lit(True)
        if self.situacoes:
            expr = expr & pl.col("situacao_cadastral").is_in(sorted(self.situacoes))
        if self.cnaes:
            expr = expr & pl.col("cnae_fiscal_principal").is_in(sorted(self.cnaes))
        if self.uf:
            expr = expr & (pl.col("uf") == self.uf)
        if self.municipios_config:
            if self.municipios is None:
                raise RuntimeError("resolver_municipios() precisa rodar antes de filtrar estabelecimentos")
            expr = expr & pl.col("municipio").is_in(sorted(self.municipios))
        data_min = self.data_inicio_minima
        if data_min is not None:
            if dtype_data == pl.Date:
                expr = expr & (pl.col("data_inicio_atividade") >= data_min)
            else:
                # Ainda string YYYYMMDD (carga PostgreSQL / Parquet não tipado): comparação lexicográfica vale
                expr = expr & (pl.col("data_inicio_atividade") >= data_min.strftime("%Y%m%d"))
            expr = expr & pl.col("data_inicio_atividade").is_not_null()
        return expr

    def aplicar(self, df: pl.DataFrame, file_type: str | None) -> pl.DataFrame:
        """Filtra um lote já transformado. Tipos não afetados (tabelas de referência) passam inteiros."""
        if file_type == TIPO_ESTABELECIMENTOS:
            return df.filter(self.expressao_estabelecimentos(df.schema.get("data_inicio_atividade")))
        if file_type in TIPOS_POR_CNPJ_BASICO:
            if self.cnpjs_basicos is None:
                raise RuntimeError(
                    f"{file_type}: CNPJs básicos filtrados ainda não carregados — estabelecimentos tem que vir antes"
                )
            # Base rápida: milhões de CNPJs básicos; a tabela é montada uma vez, não a cada lote
            if self._basicos is None or self._basicos.height != len(self.cnpjs_basicos):
                self._basicos = pl.DataFrame(
                    {"cnpj_basico": sorted(self.cnpjs_basicos)}, schema={"cnpj_basico": pl.String}
                )
            return df.join(self._basicos, on="cnpj_basico", how="semi")
        return df


@dataclass
class ContadoresFiltro:
    """Linhas lidas/mantidas por tabela, para o log do rodar_mensal."""

    lidas: dict[str, int] = field(default_factory=dict)
    mantidas: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def registrar(self, tabela: str, lidas: int, mantidas: int) -> None:
        # PROCESS_WORKERS > 1 chama isso de várias threads
        with self._lock:
            self.lidas[tabela] = self.lidas.get(tabela, 0) + lidas
            self.mantidas[tabela] = self.mantidas.get(tabela, 0) + mantidas
