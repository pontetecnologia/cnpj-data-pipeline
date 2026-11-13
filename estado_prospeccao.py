"""Estado local da prospecção: o que já foi enviado ao GestaoRevenda e em qual execução.

SQLite (arquivo único, padrão estado/prospeccao.sqlite) para funcionar igual com OUTPUT_FORMAT
postgres ou parquet e sobreviver a um reset do banco do pipeline (`just reset` não apaga isto).

Novidade de cada estabelecimento, em relação ao que já foi enviado em execuções anteriores:
- CNPJ_INEDITO: nenhum estabelecimento dessa empresa (cnpj_basico) foi enviado antes;
- FILIAL_NOVA:  a empresa já é conhecida, mas este estabelecimento (CNPJ completo) não;
- JA_ENVIADO:   este CNPJ completo já foi enviado.

Só entram em `cnpjs_enviados` os resultados definitivos (IMPORTADO, DUPLICADO, BLOQUEADO).
ERRO não entra, então o CNPJ volta a ser tentado na próxima execução.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

CNPJ_INEDITO = "CNPJ_INEDITO"
FILIAL_NOVA = "FILIAL_NOVA"
JA_ENVIADO = "JA_ENVIADO"

RESULTADOS_DEFINITIVOS = ("IMPORTADO", "DUPLICADO", "BLOQUEADO")
RESULTADOS = (*RESULTADOS_DEFINITIVOS, "ERRO")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS execucoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iniciada_em TEXT NOT NULL,
    finalizada_em TEXT,
    mes_base TEXT,
    modo TEXT NOT NULL,              -- 'novos' | 'tudo'
    status TEXT NOT NULL,            -- 'EM_ANDAMENTO' | 'OK' | 'FALHOU'
    contagens TEXT                   -- JSON com as contagens do log
);
CREATE TABLE IF NOT EXISTS cnpjs_enviados (
    cnpj TEXT PRIMARY KEY,
    cnpj_basico TEXT NOT NULL,
    primeira_execucao INTEGER NOT NULL REFERENCES execucoes(id),
    ultima_execucao INTEGER NOT NULL REFERENCES execucoes(id),
    primeiro_envio_em TEXT NOT NULL,
    ultimo_resultado TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cnpjs_enviados_basico ON cnpjs_enviados(cnpj_basico);
CREATE TABLE IF NOT EXISTS execucao_itens (
    execucao_id INTEGER NOT NULL REFERENCES execucoes(id),
    cnpj TEXT NOT NULL,
    novidade TEXT NOT NULL,
    resultado TEXT NOT NULL,
    PRIMARY KEY (execucao_id, cnpj)
);
"""


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class ItemResultado:
    cnpj: str
    cnpj_basico: str
    novidade: str
    resultado: str  # IMPORTADO | DUPLICADO | BLOQUEADO | ERRO


class EstadoProspeccao:
    def __init__(self, caminho: str | Path | None = None):
        caminho = caminho or os.getenv("PROSPECCAO_ESTADO", "estado/prospeccao.sqlite")
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.caminho)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> EstadoProspeccao:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── Classificação ─────────────────────────────────────────────────────────

    def classificar(self, linhas: Iterable[dict]) -> list[dict]:
        """Devolve as linhas com a coluna `novidade` preenchida (não grava nada)."""
        linhas = list(linhas)
        cnpjs = {row[0] for row in self.conn.execute("SELECT cnpj FROM cnpjs_enviados")}
        basicos = {row[0] for row in self.conn.execute("SELECT DISTINCT cnpj_basico FROM cnpjs_enviados")}
        for linha in linhas:
            if linha["cnpj"] in cnpjs:
                linha["novidade"] = JA_ENVIADO
            elif linha["cnpj_basico"] in basicos:
                linha["novidade"] = FILIAL_NOVA
            else:
                linha["novidade"] = CNPJ_INEDITO
        return linhas

    # ── Execuções ─────────────────────────────────────────────────────────────

    def iniciar_execucao(self, mes_base: str | None, modo: str) -> int:
        if modo not in ("novos", "tudo"):
            raise ValueError("modo deve ser 'novos' ou 'tudo'")
        cur = self.conn.execute(
            "INSERT INTO execucoes (iniciada_em, mes_base, modo, status) VALUES (?, ?, ?, 'EM_ANDAMENTO')",
            (_agora(), mes_base, modo),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def registrar_resultados(self, execucao_id: int, itens: Iterable[ItemResultado]) -> None:
        """Grava o resultado de um lote. Chamado a cada lote enviado: se o processo cair no meio,
        o que já foi aceito pelo GestaoRevenda não é reenviado como novo."""
        agora = _agora()
        with self.conn:
            for item in itens:
                if item.resultado not in RESULTADOS:
                    raise ValueError(f"resultado inválido: {item.resultado!r}")
                self.conn.execute(
                    "INSERT OR REPLACE INTO execucao_itens (execucao_id, cnpj, novidade, resultado) "
                    "VALUES (?, ?, ?, ?)",
                    (execucao_id, item.cnpj, item.novidade, item.resultado),
                )
                if item.resultado in RESULTADOS_DEFINITIVOS:
                    self.conn.execute(
                        """INSERT INTO cnpjs_enviados (cnpj, cnpj_basico, primeira_execucao,
                               ultima_execucao, primeiro_envio_em, ultimo_resultado)
                           VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT (cnpj) DO UPDATE SET
                               ultima_execucao = excluded.ultima_execucao,
                               ultimo_resultado = excluded.ultimo_resultado""",
                        (item.cnpj, item.cnpj_basico, execucao_id, execucao_id, agora, item.resultado),
                    )

    def finalizar_execucao(self, execucao_id: int, status: str, contagens: dict) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE execucoes SET finalizada_em = ?, status = ?, contagens = ? WHERE id = ?",
                (_agora(), status, json.dumps(contagens, ensure_ascii=False), execucao_id),
            )

    def ultima_execucao(self) -> dict | None:
        row = self.conn.execute(
            "SELECT id, iniciada_em, finalizada_em, mes_base, modo, status, contagens FROM execucoes "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        chaves = ("id", "iniciada_em", "finalizada_em", "mes_base", "modo", "status", "contagens")
        resultado = dict(zip(chaves, row, strict=True))
        resultado["contagens"] = json.loads(resultado["contagens"]) if resultado["contagens"] else None
        return resultado
