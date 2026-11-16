"""Estado local da prospecção: o que já foi enviado ao GestaoRevenda e em qual execução.

SQLite (arquivo único, padrão estado/prospeccao.sqlite) para funcionar igual com OUTPUT_FORMAT
postgres ou parquet e sobreviver a um reset do banco do pipeline (`just reset` não apaga isto).

Novidade de cada estabelecimento, em relação ao que já foi enviado em execuções anteriores:
- CNPJ_INEDITO: nenhum estabelecimento dessa empresa (cnpj_basico) foi enviado antes;
- FILIAL_NOVA:  a empresa já é conhecida, mas este estabelecimento (CNPJ completo) não;
- JA_ENVIADO:   este CNPJ completo já foi enviado.

Só entram em `cnpjs_enviados` os resultados definitivos (IMPORTADO, DUPLICADO, BLOQUEADO).
ERRO não entra, então o CNPJ volta a ser tentado na próxima execução.

Status de uma execução: EM_ANDAMENTO (ou interrompida sem chegar ao fim, até a próxima execução
marcá-la como INTERROMPIDA), OK, PARCIAL (--limite cortou a lista), SEM_ENVIO, FALHOU, INTERROMPIDA.

`servico_verificacoes` guarda as verificações diárias do servico_prospeccao.py.
`fila_gestaorevenda` guarda as execuções e o sinal que ainda não chegaram ao GestaoRevenda.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

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
    status TEXT NOT NULL,            -- EM_ANDAMENTO | OK | PARCIAL | SEM_ENVIO | FALHOU | INTERROMPIDA
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
CREATE TABLE IF NOT EXISTS servico_verificacoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    em TEXT NOT NULL,
    data TEXT NOT NULL,              -- AAAA-MM-DD (dia da verificação)
    origem TEXT NOT NULL,            -- 'servico' | 'manual'
    mes_receita TEXT,
    resultado TEXT NOT NULL,
    detalhe TEXT
);
CREATE INDEX IF NOT EXISTS idx_servico_verificacoes_data ON servico_verificacoes(data, origem);
-- O que ainda não chegou ao GestaoRevenda (execuções e o último sinal). Reenviado no próximo
-- ciclo; a chave torna o reenvio idempotente lá.
CREATE TABLE IF NOT EXISTS fila_gestaorevenda (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo TEXT NOT NULL,              -- 'execucao' | 'sinal'
    chave TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    criado_em TEXT NOT NULL,
    tentativas INTEGER NOT NULL DEFAULT 0,
    ultimo_erro TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
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
        # timeout: o serviço e a execução (subprocesso) usam o mesmo arquivo
        self.conn = sqlite3.connect(self.caminho, timeout=30)
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

    def marcar_interrompidas(self) -> int:
        """Execuções que ficaram EM_ANDAMENTO (computador desligado, processo morto) viram INTERROMPIDA.
        Só chame com a trava da prospecção na mão: aí nenhuma outra está de fato em andamento."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE execucoes SET status = 'INTERROMPIDA', finalizada_em = COALESCE(finalizada_em, ?) "
                "WHERE status = 'EM_ANDAMENTO'",
                (_agora(),),
            )
        return cur.rowcount

    def mes_concluido(self, mes_base: str, aceitar_sem_envio: bool) -> bool:
        """O mês já teve execução completa? OK sempre conta; SEM_ENVIO só quando o envio está desligado
        (ao ligar o envio, um mês que só rodou sem envio volta a ficar pendente)."""
        return self.ultima_concluida(mes_base, aceitar_sem_envio) is not None

    def ultima_concluida(self, mes_base: str, aceitar_sem_envio: bool) -> dict[str, Any] | None:
        """A execução mais recente que conclui o mês (ver mes_concluido), ou None."""
        status = ("OK", "SEM_ENVIO") if aceitar_sem_envio else ("OK",)
        marcadores = ",".join("?" * len(status))
        return self._execucao_onde(f"mes_base = ? AND status IN ({marcadores})", (mes_base, *status))

    def ultima_com_status(self, *status: str) -> dict[str, Any] | None:
        marcadores = ",".join("?" * len(status))
        return self._execucao_onde(f"status IN ({marcadores})", status)

    def ultimo_id_execucao(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM execucoes").fetchone()
        return int(row[0])

    def execucao(self, execucao_id: int) -> dict[str, Any] | None:
        return self._execucao_onde("id = ?", (execucao_id,))

    def finalizar_execucao(self, execucao_id: int, status: str, contagens: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE execucoes SET finalizada_em = ?, status = ?, contagens = ? WHERE id = ?",
                (_agora(), status, json.dumps(contagens, ensure_ascii=False), execucao_id),
            )

    def ultima_execucao(self) -> dict[str, Any] | None:
        return self._execucao_onde("1 = 1", ())

    def _execucao_onde(self, where: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT id, iniciada_em, finalizada_em, mes_base, modo, status, contagens FROM execucoes "
            f"WHERE {where} ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return None
        chaves = ("id", "iniciada_em", "finalizada_em", "mes_base", "modo", "status", "contagens")
        resultado: dict[str, Any] = dict(zip(chaves, row, strict=True))
        resultado["contagens"] = json.loads(resultado["contagens"]) if resultado["contagens"] else None
        return resultado

    # ── Verificações do serviço ───────────────────────────────────────────────

    def registrar_verificacao(
        self, origem: str, resultado: str, mes_receita: str | None = None, detalhe: str | None = None
    ) -> int:
        agora = datetime.now()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO servico_verificacoes (em, data, origem, mes_receita, resultado, detalhe) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    agora.isoformat(timespec="seconds"),
                    agora.date().isoformat(),
                    origem,
                    mes_receita,
                    resultado,
                    detalhe,
                ),
            )
        return int(cur.lastrowid or 0)

    def verificacoes_do_dia(self, dia: date, origem: str = "servico") -> list[tuple[str, datetime]]:
        """(resultado, quando) das verificações daquele dia, mais antigas primeiro."""
        rows = self.conn.execute(
            "SELECT resultado, em FROM servico_verificacoes WHERE data = ? AND origem = ? ORDER BY id",
            (dia.isoformat(), origem),
        ).fetchall()
        return [(r[0], datetime.fromisoformat(r[1])) for r in rows]

    def ultimas_verificacoes(self, n: int = 5) -> list[dict[str, Any]]:
        chaves = ("em", "origem", "mes_receita", "resultado", "detalhe")
        rows = self.conn.execute(
            "SELECT em, origem, mes_receita, resultado, detalhe FROM servico_verificacoes ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [dict(zip(chaves, r, strict=True)) for r in rows]

    # ── Instalação e fila para o GestaoRevenda ────────────────────────────────

    def instancia(self) -> str:
        """Identificador desta instalação (gerado uma vez): entra na chave das execuções enviadas
        ao GestaoRevenda, para dois computadores (ou um estado recriado) nunca colidirem."""
        row = self.conn.execute("SELECT valor FROM meta WHERE chave = 'instancia'").fetchone()
        if row:
            return str(row[0])
        valor = uuid.uuid4().hex[:12]
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO meta (chave, valor) VALUES ('instancia', ?)", (valor,))
        row = self.conn.execute("SELECT valor FROM meta WHERE chave = 'instancia'").fetchone()
        return str(row[0])

    def enfileirar(self, tipo: str, chave: str, payload: dict[str, Any]) -> None:
        """Mesma chave substitui o conteúdo (o sinal guarda só o mais recente)."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO fila_gestaorevenda (tipo, chave, payload, criado_em) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chave) DO UPDATE SET payload = excluded.payload",
                (tipo, chave, json.dumps(payload, ensure_ascii=False), _agora()),
            )

    def pendentes_fila(self) -> list[tuple[int, str, str, dict[str, Any]]]:
        """(id, tipo, chave, payload) — execuções antes do sinal, mais antigas primeiro."""
        rows = self.conn.execute(
            "SELECT id, tipo, chave, payload FROM fila_gestaorevenda ORDER BY tipo = 'sinal', id"
        ).fetchall()
        return [(int(r[0]), str(r[1]), str(r[2]), json.loads(r[3])) for r in rows]

    def remover_da_fila(self, item_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM fila_gestaorevenda WHERE id = ?", (item_id,))

    def falha_na_fila(self, item_id: int, erro: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE fila_gestaorevenda SET tentativas = tentativas + 1, ultimo_erro = ? WHERE id = ?",
                (erro[:500], item_id),
            )

    def tamanho_fila(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM fila_gestaorevenda").fetchone()[0])
