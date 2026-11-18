"""Consultas da tela local sobre a base rápida (parquet/<mês>/), com DuckDB.

A carga mensal (rodar_mensal.carregar_base) guarda todos os estabelecimentos da UF, com empresas e
Simples. Aqui os filtros da tela viram SQL sobre esses arquivos: respostas em segundos, sem baixar
nada. "Já enviada" = CNPJ completo em cnpjs_enviados do estado local (o mesmo registro do envio
automático, então nenhum dos dois reenvia o que o outro mandou).
"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from exportar_prospeccao import (
    SQL_BASE,
    _views_duckdb,  # pyright: ignore[reportPrivateUsage]
    montar_linha,
)
from filtro import normalizar_cnae, normalizar_situacao

LIMITE_PREVIA = 100
LIMITE_EXCEL = 1_048_575  # linhas do Excel menos o cabeçalho
SITUACOES_TELA = {"02": "Ativa", "03": "Suspensa", "04": "Inapta", "08": "Baixada", "01": "Nula"}
MATRIZ_FILIAL = ("todas", "matriz", "filial")
MESES = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)  # fmt: skip

# Telefone com pelo menos 7 dígitos que não sejam só zeros (o mesmo critério de formatar_telefone)
_MACROS = """
CREATE OR REPLACE TEMP MACRO so_digitos(t) AS regexp_replace(coalesce(CAST(t AS VARCHAR), ''), '[^0-9]', '', 'g');
CREATE OR REPLACE TEMP MACRO tel_ok(t) AS length(so_digitos(t)) >= 7 AND NOT regexp_full_match(so_digitos(t), '0+');
"""
_CNPJ = "(e.cnpj_basico || e.cnpj_ordem || e.cnpj_dv)"
_SEM_CONTATO = (
    "(NOT tel_ok(e.telefone_1) AND NOT tel_ok(e.telefone_2) AND nullif(trim(e.correio_eletronico), '') IS NULL)"
)
_MEI = "(coalesce(s.opcao_pelo_mei, '') = 'S')"


class FiltrosInvalidosError(ValueError):
    """Filtro da tela inválido. A mensagem vai direto para a tela, em português simples."""


class SemBaseError(RuntimeError):
    """Ainda não há base carregada."""


def mes_por_extenso(mes: str | None) -> str:
    """'2026-10' → 'outubro de 2026'."""
    if not mes or not re.fullmatch(r"\d{4}-\d{2}", mes):
        return mes or ""
    return f"{MESES[int(mes[5:]) - 1]} de {mes[:4]}"


def formatar_cnae(codigo: str) -> str:
    """'4711302' → '4711-3/02'."""
    return f"{codigo[:4]}-{codigo[4]}/{codigo[5:]}" if len(codigo) == 7 else codigo


def formatar_cnpj(cnpj: str) -> str:
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}" if len(cnpj) == 14 else cnpj


# ── Filtros ───────────────────────────────────────────────────────────────────


def _lista(dados: dict[str, Any], chave: str) -> list[Any]:
    valor = dados.get(chave)
    if valor is None:
        return []
    if not isinstance(valor, list):
        raise FiltrosInvalidosError(f"Filtro inválido: {chave}")
    return cast(list[Any], valor)


def _data(valor: Any, nome: str) -> date | None:
    if valor in (None, ""):
        return None
    try:
        return date.fromisoformat(str(valor))
    except ValueError as e:
        raise FiltrosInvalidosError(f"Data de abertura inválida ({nome}): {valor}") from e


def _valor(valor: Any, nome: str) -> float | None:
    if valor in (None, ""):
        return None
    try:
        numero = float(str(valor).replace(",", "."))
    except ValueError as e:
        raise FiltrosInvalidosError(f"Capital social {nome} inválido: {valor}") from e
    if numero < 0:
        raise FiltrosInvalidosError(f"O capital social {nome} não pode ser negativo")
    return numero


@dataclass(frozen=True)
class Filtros:
    cnaes: tuple[str, ...] = ()  # 7 dígitos; vazio = todas as atividades
    municipios: tuple[str, ...] = ()  # códigos da Receita; vazio = todas as cidades da base
    abertura_de: date | None = None
    abertura_ate: date | None = None
    situacoes: tuple[str, ...] = ("02",)  # vazio = todas
    matriz_filial: str = "todas"
    capital_ligado: bool = False
    capital_min: float | None = None
    capital_max: float | None = None
    excluir_mei: bool = False

    @classmethod
    def de_json(cls, dados: Any) -> Filtros:
        if not isinstance(dados, dict):
            raise FiltrosInvalidosError("Filtros inválidos")
        d = cast(dict[str, Any], dados)
        try:
            cnaes = tuple(sorted({normalizar_cnae(str(c)) for c in _lista(d, "cnaes")}))
            situacoes = tuple(sorted({normalizar_situacao(str(s)) for s in _lista(d, "situacoes")}))
        except ValueError as e:
            raise FiltrosInvalidosError(str(e)) from e
        municipios = tuple(sorted({str(m).strip() for m in _lista(d, "municipios") if str(m).strip()}))
        if any(not m.isdigit() for m in municipios):
            raise FiltrosInvalidosError("Cidade inválida: escolha as cidades na lista")
        matriz = str(d.get("matriz_filial") or "todas")
        if matriz not in MATRIZ_FILIAL:
            raise FiltrosInvalidosError("Escolha matriz, filial ou as duas")
        de, ate = _data(d.get("abertura_de"), "de"), _data(d.get("abertura_ate"), "até")
        if de and ate and de > ate:
            raise FiltrosInvalidosError("A data de abertura 'de' é depois da data 'até'")
        capital_ligado = d.get("capital_ligado") is True
        cmin = _valor(d.get("capital_min"), "mínimo") if capital_ligado else None
        cmax = _valor(d.get("capital_max"), "máximo") if capital_ligado else None
        if capital_ligado and cmin is None and cmax is None:
            raise FiltrosInvalidosError("Informe o capital social mínimo, o máximo ou os dois (ou desligue o filtro)")
        if cmin is not None and cmax is not None and cmin > cmax:
            raise FiltrosInvalidosError("O capital social mínimo é maior que o máximo")
        return cls(
            cnaes=cnaes,
            municipios=tuple(m.zfill(4) for m in municipios),
            abertura_de=de,
            abertura_ate=ate,
            situacoes=situacoes,
            matriz_filial=matriz,
            capital_ligado=capital_ligado,
            capital_min=cmin,
            capital_max=cmax,
            excluir_mei=d.get("excluir_mei") is True,
        )

    def como_json(self) -> dict[str, Any]:
        dados = asdict(self)
        for chave in ("cnaes", "municipios", "situacoes"):
            dados[chave] = list(dados[chave])
        for chave in ("abertura_de", "abertura_ate"):
            dados[chave] = dados[chave].isoformat() if dados[chave] else None
        return dados

    def resumo(self, nomes_cnae: dict[str, str], nomes_municipio: dict[str, str]) -> str:
        """Os filtros em português, para o histórico."""
        partes: list[str] = []
        if self.cnaes:
            atividades = [f"{formatar_cnae(c)} {nomes_cnae.get(c, '')}".strip() for c in self.cnaes]
            partes.append("Atividades: " + "; ".join(atividades))
        else:
            partes.append("Todas as atividades")
        if self.municipios:
            partes.append("Cidades: " + ", ".join(nomes_municipio.get(m, m) for m in self.municipios))
        else:
            partes.append("Todas as cidades")
        if self.abertura_de or self.abertura_ate:
            de = self.abertura_de.strftime("%d/%m/%Y") if self.abertura_de else "o início"
            ate = self.abertura_ate.strftime("%d/%m/%Y") if self.abertura_ate else "hoje"
            partes.append(f"Abertura de {de} até {ate}")
        else:
            partes.append("Qualquer data de abertura")
        if self.situacoes:
            partes.append("Situação: " + ", ".join(SITUACOES_TELA.get(s, s) for s in self.situacoes))
        else:
            partes.append("Qualquer situação")
        if self.matriz_filial != "todas":
            partes.append(f"Só {self.matriz_filial}")
        if self.capital_ligado:
            if self.capital_min is not None and self.capital_max is not None:
                partes.append(f"Capital de {_reais(self.capital_min)} a {_reais(self.capital_max)}")
            elif self.capital_min is not None:
                partes.append(f"Capital a partir de {_reais(self.capital_min)}")
            elif self.capital_max is not None:
                partes.append(f"Capital até {_reais(self.capital_max)}")
        if self.excluir_mei:
            partes.append("Sem MEI")
        return " · ".join(partes)


def _reais(valor: float) -> str:
    inteiro = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {inteiro.removesuffix(',00')}"


# ── Base ──────────────────────────────────────────────────────────────────────


@dataclass
class InfoBase:
    pasta: Path
    mes: str
    uf: str | None
    carregada_em: str | None  # ISO, hora local
    tamanho_bytes: int
    estabelecimentos: int
    meses_guardados: list[str] = field(default_factory=list[str])

    def como_json(self) -> dict[str, Any]:
        return {
            "mes": self.mes,
            "mes_texto": mes_por_extenso(self.mes),
            "uf": self.uf,
            "carregada_em": self.carregada_em,
            "tamanho_mb": round(self.tamanho_bytes / 1024**2),
            "estabelecimentos": self.estabelecimentos,
            "meses_guardados": self.meses_guardados,
        }


def _modulo(nome: str) -> Any:
    """pyarrow e xlsxwriter não têm stubs de tipo: importados como Any."""
    return importlib.import_module(nome)


def _uf_da_base(pasta: Path) -> str | None:
    """A UF gravada pela carga na assinatura do Parquet ('base;uf=SC')."""
    pq = _modulo("pyarrow.parquet")
    arquivo = pasta / "estabelecimentos.parquet"
    if not arquivo.exists():
        return None
    metadados: dict[bytes, bytes] = pq.read_metadata(arquivo).metadata or {}
    valor = metadados.get(b"cnpj.prospeccaoFiltro", b"").decode()
    m = re.search(r"(?:^|;)uf=([A-Z]{2})(?:;|$)", valor)
    return m[1] if m else None


def info_base(raiz: Path) -> InfoBase | None:
    """O mês mais recente com carga completa (manifest.json), ou None."""
    completas = sorted(p for p in raiz.glob("????-??") if p.is_dir() and (p / "manifest.json").exists())
    if not completas:
        return None
    pasta = completas[-1]
    manifest: dict[str, Any] = json.loads((pasta / "manifest.json").read_text(encoding="utf-8"))
    tabelas: dict[str, Any] = manifest.get("tables") or {}
    carregada = manifest.get("exportedAt")
    if carregada:
        try:
            carregada = datetime.fromisoformat(str(carregada).replace("Z", "+00:00")).astimezone().isoformat()
        except ValueError:
            pass
    tamanho = sum(a.stat().st_size for a in pasta.glob("*.parquet"))
    return InfoBase(
        pasta=pasta,
        mes=str(manifest.get("sourceMonth") or pasta.name),
        uf=_uf_da_base(pasta),
        carregada_em=carregada,
        tamanho_bytes=tamanho,
        estabelecimentos=int(cast(dict[str, Any], tabelas.get("estabelecimentos") or {}).get("rows") or 0),
        meses_guardados=[p.name for p in completas],
    )


class Consulta:
    """Uma conexão DuckDB sobre a pasta do mês. Use com `with`: não segura os arquivos depois
    (a carga do mês seguinte precisa apagar as pastas antigas)."""

    def __init__(self, pasta: Path, enviados: Iterable[str] = ()):
        import duckdb

        pa = _modulo("pyarrow")
        self.con = duckdb.connect()
        if not _views_duckdb(self.con, pasta):
            self.con.close()
            raise SemBaseError(f"Nenhum estabelecimento em {pasta}")
        self.con.execute(_MACROS)
        self.con.register("enviados", pa.table({"cnpj": pa.array(list(enviados), type=pa.string())}))

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Consulta:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── Listas da tela ────────────────────────────────────────────────────────

    def cnaes(self) -> list[dict[str, str]]:
        rows = self.con.execute("SELECT codigo, descricao FROM cnaes ORDER BY descricao").fetchall()
        return [{"codigo": str(c), "descricao": str(d or "")} for c, d in rows if len(str(c)) == 7]

    def municipios(self) -> list[dict[str, Any]]:
        """Só as cidades que aparecem na base (as da UF)."""
        rows = self.con.execute(
            """SELECT e.municipio, coalesce(m.descricao, e.municipio) AS nome, count(*) AS n
               FROM estabelecimentos e LEFT JOIN municipios m ON m.codigo = e.municipio
               WHERE e.municipio IS NOT NULL GROUP BY 1, 2 ORDER BY 2"""
        ).fetchall()
        return [{"codigo": str(c), "nome": str(n), "estabelecimentos": int(q)} for c, n, q in rows]

    # ── Filtro em SQL ─────────────────────────────────────────────────────────

    @staticmethod
    def where(f: Filtros) -> tuple[str, list[Any]]:
        partes: list[str] = []
        params: list[Any] = []

        def em(coluna: str, valores: tuple[str, ...]) -> None:
            if valores:
                partes.append(f"{coluna} IN ({', '.join('?' * len(valores))})")
                params.extend(valores)

        em("e.cnae_fiscal_principal", f.cnaes)
        em("e.municipio", f.municipios)
        em("e.situacao_cadastral", f.situacoes)
        if f.abertura_de:
            partes.append("e.data_inicio_atividade >= ?")
            params.append(f.abertura_de)
        if f.abertura_ate:
            partes.append("e.data_inicio_atividade <= ?")
            params.append(f.abertura_ate)
        if f.matriz_filial != "todas":
            partes.append("e.identificador_matriz_filial = ?")
            params.append(1 if f.matriz_filial == "matriz" else 2)
        if f.capital_ligado and f.capital_min is not None:
            partes.append("emp.capital_social >= ?")
            params.append(f.capital_min)
        if f.capital_ligado and f.capital_max is not None:
            partes.append("emp.capital_social <= ?")
            params.append(f.capital_max)
        if f.excluir_mei:
            partes.append(f"NOT {_MEI}")
        return ("WHERE " + " AND ".join(partes)) if partes else "", params

    # ── Resultados ────────────────────────────────────────────────────────────

    def contar(self, f: Filtros) -> dict[str, int]:
        where, params = self.where(f)
        nova = f"{_CNPJ} NOT IN (SELECT cnpj FROM enviados)"
        row = self.con.execute(
            f"""SELECT count(*),
                       count(*) FILTER (WHERE NOT ({nova})),
                       count(*) FILTER (WHERE {nova}),
                       count(*) FILTER (WHERE {nova} AND {_MEI}),
                       count(*) FILTER (WHERE {nova} AND {_SEM_CONTATO})
                FROM estabelecimentos e
                LEFT JOIN empresas emp ON emp.cnpj_basico = e.cnpj_basico
                LEFT JOIN dados_simples s ON s.cnpj_basico = e.cnpj_basico
                {where}""",
            params,
        ).fetchone()
        assert row is not None
        chaves = ("encontradas", "ja_enviadas", "novas", "novas_mei", "novas_sem_contato")
        return {k: int(v or 0) for k, v in zip(chaves, row, strict=True)}

    def linhas(
        self, f: Filtros, *, mes_base: str | None, so_novas: bool = False, limite: int | None = None
    ) -> list[dict[str, Any]]:
        """Linhas no formato do envio (montar_linha), com `ja_enviada`. Novas primeiro."""
        where, params = self.where(f)
        nova = f"{_CNPJ} NOT IN (SELECT cnpj FROM enviados)"
        if so_novas:
            where = f"{where} AND {nova}" if where else f"WHERE {nova}"
        # SQL_BASE já tem os JOINs com os aliases e/emp/s que o WHERE usa: o filtro vai dentro
        enviada = "(base.cnpj_basico || base.cnpj_ordem || base.cnpj_dv) IN (SELECT cnpj FROM enviados)"
        sql = (
            f"SELECT base.*, {enviada} AS ja_enviada FROM ({SQL_BASE} {where}) base "
            "ORDER BY ja_enviada, base.municipio_nome, base.razao_social, base.cnpj_basico, base.cnpj_ordem"
        )
        if limite is not None:
            sql += f" LIMIT {int(limite)}"
        cur = self.con.execute(sql, params)
        nomes = [d[0] for d in cur.description]
        brutas = [dict(zip(nomes, row, strict=True)) for row in cur.fetchall()]
        cnaes = dict(self.con.execute("SELECT codigo, descricao FROM cnaes").fetchall())
        resultado: list[dict[str, Any]] = []
        for bruta in brutas:
            linha = montar_linha(bruta, cnaes, mes_base)
            linha["ja_enviada"] = bool(bruta["ja_enviada"])
            resultado.append(linha)
        return resultado


def linha_previa(linha: dict[str, Any]) -> dict[str, Any]:
    """O que a prévia da tela mostra de cada empresa."""
    tels: list[dict[str, str]] = linha.get("telefones") or []
    return {
        "cnpj": formatar_cnpj(linha["cnpj"]),
        "nome": linha.get("razao_social"),
        "fantasia": linha.get("nome_fantasia"),
        "atividade": f"{formatar_cnae(linha.get('cnae_codigo') or '')} {linha.get('cnae_descricao') or ''}".strip(),
        "cidade": linha.get("municipio_nome"),
        "abertura": linha.get("data_inicio_atividade"),
        "situacao": linha.get("situacao"),
        "tipo": linha.get("matriz_filial"),
        "capital": linha.get("capital_social"),
        "mei": linha.get("mei"),
        "telefone": tels[0]["numero"] if tels else None,
        "email": linha.get("email"),
        "ja_enviada": linha.get("ja_enviada", False),
    }


# ── Excel ─────────────────────────────────────────────────────────────────────

COLUNAS_EXCEL: list[tuple[str, str, int]] = [
    ("envio", "Envio", 12),
    ("cnpj", "CNPJ", 20),
    ("razao_social", "Razão social", 40),
    ("nome_fantasia", "Nome fantasia", 28),
    ("matriz_filial", "Matriz/filial", 12),
    ("cnae", "CNAE", 11),
    ("cnae_descricao", "Atividade", 40),
    ("data_inicio_atividade", "Abertura", 12),
    ("situacao", "Situação", 11),
    ("endereco", "Endereço", 40),
    ("bairro", "Bairro", 20),
    ("municipio_nome", "Cidade", 20),
    ("uf", "UF", 5),
    ("cep", "CEP", 11),
    ("telefone_1", "Telefone 1", 16),
    ("telefone_2", "Telefone 2", 16),
    ("email", "E-mail", 32),
    ("porte", "Porte", 8),
    ("capital_social", "Capital social", 15),
    ("simples_nacional", "Simples", 9),
    ("mei", "MEI", 6),
    ("mes_base", "Base da Receita", 14),
]


def gerar_excel(linhas: Iterable[dict[str, Any]], caminho: Path) -> int:
    """Grava o .xlsx (modo de memória constante: aguenta o limite do Excel). Devolve as linhas."""
    xlsxwriter = _modulo("xlsxwriter")
    livro = xlsxwriter.Workbook(str(caminho), {"constant_memory": True, "strings_to_numbers": False})
    try:
        aba = livro.add_worksheet("Empresas")
        cabecalho = livro.add_format({"bold": True, "bg_color": "#DDE6F0", "border": 1})
        data = livro.add_format({"num_format": "dd/mm/yyyy"})
        dinheiro = livro.add_format({"num_format": "#,##0.00"})
        for col, (_chave, titulo, largura) in enumerate(COLUNAS_EXCEL):
            aba.set_column(col, col, largura)
            aba.write(0, col, titulo, cabecalho)
        aba.freeze_panes(1, 0)
        n = 0
        for n, linha in enumerate(linhas, start=1):
            tels: list[dict[str, str]] = linha.get("telefones") or []
            logradouro = ", ".join(
                p for p in (linha.get("logradouro"), linha.get("numero"), linha.get("complemento")) if p
            )
            valores: dict[str, Any] = {
                **linha,
                "envio": "Já enviada" if linha.get("ja_enviada") else "Nova",
                "cnpj": formatar_cnpj(linha["cnpj"]),
                "cnae": formatar_cnae(linha.get("cnae_codigo") or ""),
                "endereco": logradouro or None,
                "telefone_1": tels[0]["numero"] if len(tels) > 0 else None,
                "telefone_2": tels[1]["numero"] if len(tels) > 1 else None,
                "simples_nacional": {True: "Sim", False: "Não"}.get(linha.get("simples_nacional")),  # type: ignore[arg-type]
                "mei": {True: "Sim", False: "Não"}.get(linha.get("mei")),  # type: ignore[arg-type]
            }
            for col, (chave, _titulo, _largura) in enumerate(COLUNAS_EXCEL):
                valor = valores.get(chave)
                if valor is None:
                    continue
                if chave == "data_inicio_atividade":
                    try:
                        aba.write_datetime(n, col, datetime.fromisoformat(str(valor)), data)
                        continue
                    except ValueError:
                        pass
                if chave == "capital_social":
                    aba.write_number(n, col, float(valor), dinheiro)
                else:
                    aba.write_string(n, col, str(valor))
        if n:
            aba.autofilter(0, 0, n, len(COLUNAS_EXCEL) - 1)
    finally:
        livro.close()
    return n
