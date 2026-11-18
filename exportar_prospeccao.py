#!/usr/bin/env python3
"""Exporta a lista de prospecção: uma linha por estabelecimento, pronta para o GestaoRevenda.

Lê do PostgreSQL ou do Parquet (via DuckDB) conforme OUTPUT_FORMAT, aplicando o mesmo filtro de
prospecção da carga (o da tela do GestaoRevenda; ver config_prospeccao.py), e marca cada linha como
CNPJ_INEDITO, FILIAL_NOVA ou JA_ENVIADO comparando com o estado local (estado_prospeccao.py).

Uso:
    python exportar_prospeccao.py                       # só as novas → saida/prospeccao_<mes>.csv
    python exportar_prospeccao.py --tudo                # todas, inclusive as já enviadas
    python exportar_prospeccao.py --saida lista.csv

Este script NÃO registra nada como enviado — isso só acontece no envio (enviar_gestaorevenda.py).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from config import Config
from estado_prospeccao import JA_ENVIADO, EstadoProspeccao
from filtro import FiltroProspeccao

logger = logging.getLogger(__name__)

# Mesmos textos que o GestaoRevenda já usa (LeadsService.parseSituacao / mapCnpjaResponse)
SITUACOES = {"01": "NULA", "02": "ATIVA", "03": "SUSPENSA", "04": "INAPTA", "08": "BAIXADA"}
# 05 = "Demais" na Receita; o GestaoRevenda grava "Grande" para DEMAIS (mapCnpjaResponse.porteMap)
PORTES = {"01": "ME", "03": "EPP", "05": "Grande"}

COLUNAS_CSV = [
    "novidade",
    "cnpj",
    "razao_social",
    "nome_fantasia",
    "matriz_filial",
    "cnae_codigo",
    "cnae_descricao",
    "cnaes_secundarios",
    "data_inicio_atividade",
    "situacao",
    "data_situacao",
    "logradouro",
    "numero",
    "complemento",
    "bairro",
    "cep",
    "municipio_nome",
    "municipio_codigo",
    "uf",
    "endereco_completo",
    "telefone_1",
    "telefone_2",
    "email",
    "porte",
    "natureza_juridica",
    "capital_social",
    "simples_nacional",
    "mei",
    "mes_base",
]

# Uma linha por estabelecimento. Datas chegam como DATE nos dois backends (no DuckDB as views
# convertem quando o Parquet não é tipado — ver _views_duckdb).
SQL_BASE = """
SELECT
    e.cnpj_basico,
    e.cnpj_ordem,
    e.cnpj_dv,
    emp.razao_social,
    e.nome_fantasia,
    e.identificador_matriz_filial,
    e.cnae_fiscal_principal,
    c.descricao AS cnae_descricao,
    e.cnae_fiscal_secundaria,
    e.data_inicio_atividade,
    e.situacao_cadastral,
    e.data_situacao_cadastral,
    e.tipo_logradouro,
    e.logradouro,
    e.numero,
    e.complemento,
    e.bairro,
    e.cep,
    m.descricao AS municipio_nome,
    e.municipio,
    e.uf,
    e.ddd_1,
    e.telefone_1,
    e.ddd_2,
    e.telefone_2,
    e.correio_eletronico,
    emp.porte,
    n.descricao AS natureza_juridica,
    emp.capital_social,
    s.opcao_pelo_simples,
    s.opcao_pelo_mei
FROM estabelecimentos e
LEFT JOIN empresas emp ON emp.cnpj_basico = e.cnpj_basico
LEFT JOIN dados_simples s ON s.cnpj_basico = e.cnpj_basico
LEFT JOIN cnaes c ON c.codigo = e.cnae_fiscal_principal
LEFT JOIN municipios m ON m.codigo = e.municipio
LEFT JOIN naturezas_juridicas n ON n.codigo = emp.natureza_juridica
"""


# ── Filtro em SQL ─────────────────────────────────────────────────────────────


def where_filtro(filtro: FiltroProspeccao | None, ph: str) -> tuple[str, list[Any]]:
    """WHERE equivalente a FiltroProspeccao.expressao_estabelecimentos. `ph` = placeholder (%s ou ?)."""
    if filtro is None:
        return "", []
    partes: list[str] = []
    params: list[Any] = []

    def em(coluna: str, valores: set[str]) -> None:
        if not valores:  # vazio = todos (base rápida)
            return
        partes.append(f"{coluna} IN ({', '.join([ph] * len(valores))})")
        params.extend(sorted(valores))

    em("e.situacao_cadastral", filtro.situacoes)
    em("e.cnae_fiscal_principal", filtro.cnaes)
    if filtro.uf:
        partes.append(f"e.uf = {ph}")
        params.append(filtro.uf)
    if filtro.municipios_config:
        assert filtro.municipios is not None, "resolver_municipios() antes de montar o WHERE"
        em("e.municipio", filtro.municipios)
    if (data_min := filtro.data_inicio_minima) is not None:
        partes.append(f"e.data_inicio_atividade >= {ph}")
        params.append(data_min)
    return ("WHERE " + " AND ".join(partes)) if partes else "", params


# ── Leitura ───────────────────────────────────────────────────────────────────


def _ler_postgres(config: Config, filtro: FiltroProspeccao | None) -> tuple[list[dict], dict[str, str], str | None]:
    from database import Database

    db = Database(config.database_url, retry_attempts=config.retry_attempts)
    try:
        if filtro is not None and filtro.municipios_config:
            filtro.resolver_municipios(
                dict(db.fetch_rows("SELECT codigo, descricao FROM municipios"))
                if filtro.precisa_tabela_municipios
                else None
            )
        where, params = where_filtro(filtro, "%s")
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"{SQL_BASE} {where} ORDER BY e.uf, e.municipio, e.cnpj_basico, e.cnpj_ordem", params)
                nomes = [d[0] for d in cur.description]
                linhas = [dict(zip(nomes, row, strict=True)) for row in cur.fetchall()]
        finally:
            conn.rollback()
        cnaes = dict(db.fetch_rows("SELECT codigo, descricao FROM cnaes"))
        meses = db.fetch_rows("SELECT MAX(directory) FROM processed_files")
        return linhas, cnaes, meses[0][0] if meses else None
    finally:
        db.disconnect()


_COLUNAS_VAZIAS = {
    "empresas": "cnpj_basico VARCHAR, razao_social VARCHAR, natureza_juridica VARCHAR, porte VARCHAR, "
    "capital_social DOUBLE",
    "dados_simples": "cnpj_basico VARCHAR, opcao_pelo_simples VARCHAR, opcao_pelo_mei VARCHAR",
    "cnaes": "codigo VARCHAR, descricao VARCHAR",
    "municipios": "codigo VARCHAR, descricao VARCHAR",
    "naturezas_juridicas": "codigo VARCHAR, descricao VARCHAR",
}


def _views_duckdb(con: Any, pasta: Path) -> bool:
    """Cria views com os nomes das tabelas do Postgres. Retorna False se não há estabelecimentos.

    Tabela que o filtro deixou vazia não gera arquivo Parquet — vira uma tabela vazia aqui.
    Parquet não tipado (PARQUET_TYPED_OUTPUT=false) guarda datas/números como texto: convertidos
    na view para a consulta ser a mesma do Postgres.
    """
    caminho_estab = pasta / "estabelecimentos.parquet"
    if not caminho_estab.exists():
        return False
    for tabela, colunas in _COLUNAS_VAZIAS.items():
        caminho = pasta / f"{tabela}.parquet"
        if caminho.exists():
            con.execute(f"CREATE VIEW {tabela}_raw AS SELECT * FROM read_parquet('{caminho.as_posix()}')")
        else:
            con.execute(f"CREATE TABLE {tabela}_raw ({colunas})")
    descricao = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{caminho_estab.as_posix()}')").fetchall()
    tipos = {linha[0]: linha[1] for linha in descricao}  # (column_name, column_type, null, key, default, extra)

    trocas = [
        f"TRY_STRPTIME({col}, '%Y%m%d')::DATE AS {col}"
        for col in ("data_inicio_atividade", "data_situacao_cadastral", "data_situacao_especial")
        if tipos.get(col) == "VARCHAR"
    ]
    if tipos.get("identificador_matriz_filial") == "VARCHAR":
        trocas.append("TRY_CAST(identificador_matriz_filial AS INTEGER) AS identificador_matriz_filial")
    replace = f" REPLACE ({', '.join(trocas)})" if trocas else ""
    con.execute(f"CREATE VIEW estabelecimentos AS SELECT *{replace} FROM read_parquet('{caminho_estab.as_posix()}')")
    con.execute(
        "CREATE VIEW empresas AS SELECT * REPLACE (TRY_CAST(capital_social AS DOUBLE) AS capital_social) "
        "FROM empresas_raw"
    )
    for tabela in ("dados_simples", "cnaes", "municipios", "naturezas_juridicas"):
        con.execute(f"CREATE VIEW {tabela} AS SELECT * FROM {tabela}_raw")
    return True


def pasta_base_mais_recente(raiz: Path) -> Path | None:
    """Pasta do mês mais recente com carga COMPLETA (o manifest.json só é gravado no fim)."""
    meses = sorted(p for p in raiz.glob("????-??") if p.is_dir() and (p / "manifest.json").exists())
    return meses[-1] if meses else None


def _ler_parquet(config: Config, filtro: FiltroProspeccao | None) -> tuple[list[dict], dict[str, str], str | None]:
    import duckdb

    pasta = Path(config.parquet_output_dir)
    mes_base = None
    if (manifest := pasta / "manifest.json").exists():
        mes_base = json.loads(manifest.read_text()).get("sourceMonth")
    con = duckdb.connect()
    try:
        if not _views_duckdb(con, pasta):
            logger.warning(f"Nenhum estabelecimentos.parquet em {pasta} — nada a exportar")
            return [], {}, mes_base
        if filtro is not None and filtro.municipios_config:
            filtro.resolver_municipios(
                dict(con.execute("SELECT codigo, descricao FROM municipios").fetchall())
                if filtro.precisa_tabela_municipios
                else None
            )
        where, params = where_filtro(filtro, "?")
        cur = con.execute(f"{SQL_BASE} {where} ORDER BY e.uf, e.municipio, e.cnpj_basico, e.cnpj_ordem", params)
        nomes = [d[0] for d in cur.description]
        linhas = [dict(zip(nomes, row, strict=True)) for row in cur.fetchall()]
        cnaes = dict(con.execute("SELECT codigo, descricao FROM cnaes").fetchall())
        return linhas, cnaes, mes_base
    finally:
        con.close()


# ── Montagem da linha ─────────────────────────────────────────────────────────


def _limpo(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = re.sub(r"\s+", " ", str(valor)).strip()
    return texto or None


def formatar_telefone(ddd: Any, numero: Any) -> dict | None:
    """{'tipo': 'Fixo'|'Celular', 'numero': '(47) 32281850'} — mesmo formato do enriquecimento cnpja.

    A Receita guarda até 8 dígitos no telefone; o tipo é inferido pelo 1º dígito (6-9 = celular).
    """
    ddd_d = re.sub(r"\D", "", str(ddd or "")).lstrip("0")
    num_d = re.sub(r"\D", "", str(numero or ""))
    if not num_d or len(num_d) < 7 or set(num_d) == {"0"}:
        return None
    tipo = "Celular" if num_d[0] in "6789" else "Fixo"
    return {"tipo": tipo, "numero": f"({ddd_d}) {num_d}" if ddd_d else num_d}


def formatar_cep(cep: Any) -> str | None:
    digitos = re.sub(r"\D", "", str(cep or ""))
    if len(digitos) != 8 or digitos == "00000000":
        return None
    return f"{digitos[:5]}-{digitos[5:]}"


def _sim_nao(valor: Any) -> bool | None:
    return {"S": True, "N": False}.get((valor or "").strip().upper()) if valor is not None else None


def _data_iso(valor: Any) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, date):
        return valor.isoformat()
    texto = str(valor)
    return f"{texto[:4]}-{texto[4:6]}-{texto[6:8]}" if re.fullmatch(r"\d{8}", texto) else texto


def montar_linha(bruta: dict[str, Any], cnaes: dict[str, str], mes_base: str | None) -> dict[str, Any]:
    cnpj = f"{bruta['cnpj_basico']}{bruta['cnpj_ordem']}{bruta['cnpj_dv']}"
    logradouro = " ".join(p for p in (_limpo(bruta["tipo_logradouro"]), _limpo(bruta["logradouro"])) if p) or None
    cep = formatar_cep(bruta["cep"])
    municipio_nome = _limpo(bruta["municipio_nome"])
    numero = _limpo(bruta["numero"])
    complemento = _limpo(bruta["complemento"])
    bairro = _limpo(bruta["bairro"])
    # Mesma ordem do GestaoRevenda (mapCnpjaResponse): rua, número, complemento, bairro, cidade, UF, CEP
    endereco = ", ".join(p for p in (logradouro, numero, complemento, bairro, municipio_nome, bruta["uf"], cep) if p)
    telefones = [
        t
        for t in (
            formatar_telefone(bruta["ddd_1"], bruta["telefone_1"]),
            formatar_telefone(bruta["ddd_2"], bruta["telefone_2"]),
        )
        if t
    ]
    secundarios = [c for c in re.split(r"\D+", bruta["cnae_fiscal_secundaria"] or "") if len(c) == 7]
    matriz = {1: "Matriz", 2: "Filial"}.get(int(bruta["identificador_matriz_filial"] or 0))
    email = _limpo(bruta["correio_eletronico"])
    capital = bruta["capital_social"]
    return {
        "cnpj": cnpj,
        "cnpj_basico": bruta["cnpj_basico"],
        "razao_social": _limpo(bruta["razao_social"]) or _limpo(bruta["nome_fantasia"]) or cnpj,
        "nome_fantasia": _limpo(bruta["nome_fantasia"]),
        "matriz_filial": matriz,
        "cnae_codigo": bruta["cnae_fiscal_principal"],
        "cnae_descricao": _limpo(bruta["cnae_descricao"]),
        "cnaes_secundarios": [{"id": c, "text": cnaes.get(c, "")} for c in secundarios],
        "data_inicio_atividade": _data_iso(bruta["data_inicio_atividade"]),
        "situacao": SITUACOES.get(bruta["situacao_cadastral"] or "", bruta["situacao_cadastral"]),
        "data_situacao": _data_iso(bruta["data_situacao_cadastral"]),
        "logradouro": logradouro,
        "numero": numero,
        "complemento": complemento,
        "bairro": bairro,
        "cep": cep,
        "municipio_nome": municipio_nome,
        "municipio_codigo": bruta["municipio"],
        "uf": bruta["uf"],
        "endereco_completo": endereco or None,
        "telefones": telefones,
        "email": email.lower() if email else None,
        "porte": PORTES.get(bruta["porte"] or ""),
        "natureza_juridica": _limpo(bruta["natureza_juridica"]),
        "capital_social": float(capital) if capital is not None else None,
        "simples_nacional": _sim_nao(bruta["opcao_pelo_simples"]),
        "mei": _sim_nao(bruta["opcao_pelo_mei"]),
        "mes_base": mes_base,
    }


# ── API ───────────────────────────────────────────────────────────────────────


def exportar(
    config: Config,
    filtro: FiltroProspeccao | None,
    estado: EstadoProspeccao,
    tudo: bool = False,
) -> tuple[list[dict], list[dict], str | None]:
    """Retorna (todas_as_linhas_classificadas, linhas_para_enviar, mes_base)."""
    ler = _ler_parquet if config.output_format == "parquet" else _ler_postgres
    brutas, cnaes, mes_base = ler(config, filtro)
    linhas = estado.classificar(montar_linha(b, cnaes, mes_base) for b in brutas)
    selecionadas = linhas if tudo else [linha for linha in linhas if linha["novidade"] != JA_ENVIADO]
    return linhas, selecionadas, mes_base


def _valor_csv(valor: Any) -> Any:
    if isinstance(valor, bool):
        return "S" if valor else "N"
    return "" if valor is None else valor


def escrever_csv(linhas: list[dict], caminho: Path, colunas: list[str] | None = None) -> Path:
    """CSV ; com BOM (abre direto no Excel em pt-BR)."""
    colunas = colunas or COLUNAS_CSV
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with open(caminho, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=colunas, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        for linha in linhas:
            plana = dict(linha)
            tels = linha.get("telefones") or []
            plana["telefone_1"] = tels[0]["numero"] if len(tels) > 0 else None
            plana["telefone_2"] = tels[1]["numero"] if len(tels) > 1 else None
            plana["cnaes_secundarios"] = ",".join(c["id"] for c in linha.get("cnaes_secundarios") or [])
            writer.writerow({k: _valor_csv(plana.get(k)) for k in colunas})
    return caminho


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    parser = argparse.ArgumentParser(description="Exporta a lista de prospecção (uma linha por estabelecimento)")
    parser.add_argument("--tudo", action="store_true", help="Inclui os CNPJs já enviados em execuções anteriores")
    parser.add_argument("--saida", type=Path, help="Arquivo CSV de saída (padrão: saida/prospeccao_<mes>.csv)")
    args = parser.parse_args(argv)

    import config_prospeccao
    from rodar_mensal import config_pipeline

    config = config_pipeline()
    cfg = config_prospeccao.obter()
    logger.info(f"Filtro: {cfg.descrever_origem()}")
    filtro = cfg.filtro()
    if config.output_format == "parquet":
        pasta = pasta_base_mais_recente(Path(config.parquet_output_dir))
        if pasta is not None:  # rodar_mensal grava uma pasta por mês: exporta a mais recente
            config = replace(config, parquet_output_dir=str(pasta))
    with EstadoProspeccao() as estado:
        linhas, selecionadas, mes_base = exportar(config, filtro, estado, tudo=args.tudo)
    saida = args.saida or Path("saida") / f"prospeccao_{mes_base or 'sem-mes'}.csv"
    escrever_csv(selecionadas, saida)
    contagem: dict[str, int] = {}
    for linha in linhas:
        contagem[linha["novidade"]] = contagem.get(linha["novidade"], 0) + 1
    resumo = ", ".join(f"{k}={v:,}" for k, v in sorted(contagem.items())) or "nenhum"
    logger.info(f"{len(linhas):,} estabelecimentos ({resumo}); {len(selecionadas):,} gravados em {saida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
