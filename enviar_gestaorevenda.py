#!/usr/bin/env python3
"""Envia a lista de prospecção ao GestaoRevenda (POST /api/leads/importar com origem RECEITA_FEDERAL).

Autenticação OAuth2 client-credentials do módulo api-clients do GestaoRevenda (POST /api/auth/token,
token de 1h, renovado antes de vencer). Client ID/Secret gerados em Configurações > API Externa,
com os escopos "leads" (envio), "prospeccao-config" (ler a configuração) e "prospeccao-execucoes"
(gravar execução e sinal). Credenciais SÓ no .env:

    GR_API_URL=https://<backend>/api      # base da API, com o /api
    GR_CLIENT_ID=...
    GR_CLIENT_SECRET=...

Ajustes opcionais (fora do .env.example; os padrões servem): GR_LOTE=200 (linhas por requisição,
máx. 1000), GR_TENTATIVAS=4 (rede, 5xx, 429), GR_TIMEOUT=300 (segundos por requisição),
GR_ENFILEIRAR_CNPJA=false (true = o backend também consulta a cnpja.com, lento).

Se envia ou não (e o filtro, o horário...) vem da tela do GestaoRevenda: ver config_prospeccao.py.

Reenviar um lote que o servidor já processou (timeout depois do processamento) não duplica nada:
o GestaoRevenda deduplica por CNPJ e devolve DUPLICADO.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

from estado_prospeccao import EstadoProspeccao, ItemResultado
from exportar_prospeccao import COLUNAS_CSV, escrever_csv

if TYPE_CHECKING:
    from config_prospeccao import ConfigProspeccao

logger = logging.getLogger(__name__)

ORIGEM = "RECEITA_FEDERAL"
MARGEM_TOKEN_S = 120
VARIAVEIS_GR = ("GR_API_URL", "GR_CLIENT_ID", "GR_CLIENT_SECRET")
ESCOPO_LEADS = "leads"


class EnvioError(RuntimeError):
    """Falha que não adianta repetir (credencial, payload recusado, backend sem suporte)."""


def credenciais_faltando() -> list[str]:
    return [n for n in VARIAVEIS_GR if not os.getenv(n)]


def motivo_envio_desligado(cfg: ConfigProspeccao) -> str | None:
    """None se o envio ao GestaoRevenda está liberado; senão o motivo (vai para o log e o aviso).

    Envia só com GR_* preenchidos no .env E com o envio ligado na tela do GestaoRevenda. Se o
    serviço nunca conseguiu buscar a configuração, não envia. Vale para o serviço, o rodar_agora
    e o rodar_mensal chamado direto.
    """
    from config_prospeccao import ORIGEM_PADRAO

    faltando = credenciais_faltando()
    if faltando:
        return f"envio desligado ({', '.join(faltando)} vazio no .env)"
    if cfg.origem == ORIGEM_PADRAO:
        detalhe = f": {cfg.erro_busca}" if cfg.erro_busca else ""
        return f"envio desligado (configuração nunca obtida do GestaoRevenda{detalhe})"
    if not cfg.envio_ativo:
        return "envio desligado na tela do GestaoRevenda (Configurações → Prospecção Receita)"
    return None


@dataclass
class ConfigEnvio:
    api_url: str
    client_id: str
    client_secret: str
    lote: int = 200
    tentativas: int = 4
    timeout: int = 300
    enfileirar_cnpja: bool = False
    espera_base: float = 5.0

    @classmethod
    def from_env(cls) -> ConfigEnvio:
        faltando = credenciais_faltando()
        if faltando:
            raise EnvioError(f"Defina no .env: {', '.join(faltando)}")
        cfg = cls(
            api_url=os.environ["GR_API_URL"].rstrip("/"),
            client_id=os.environ["GR_CLIENT_ID"],
            client_secret=os.environ["GR_CLIENT_SECRET"],
            lote=int(os.getenv("GR_LOTE", "200")),
            tentativas=int(os.getenv("GR_TENTATIVAS", "4")),
            timeout=int(os.getenv("GR_TIMEOUT", "300")),
            enfileirar_cnpja=os.getenv("GR_ENFILEIRAR_CNPJA", "false").lower() == "true",
        )
        if not 1 <= cfg.lote <= 1000:
            raise EnvioError("GR_LOTE deve estar entre 1 e 1000")
        return cfg


@dataclass
class ResultadoEnvio:
    enviadas: int = 0
    importados: int = 0
    duplicados: int = 0
    bloqueados: int = 0
    erros: int = 0
    lotes: int = 0
    backup: Path | None = None
    detalhes_erro: list[str] = field(default_factory=list)

    def como_dict(self) -> dict[str, Any]:
        return {
            "enviadas": self.enviadas,
            "importados": self.importados,
            "duplicados": self.duplicados,
            "bloqueados": self.bloqueados,
            "erros": self.erros,
            "lotes": self.lotes,
            "backup": str(self.backup) if self.backup else None,
        }


class ClienteGestaoRevenda:
    def __init__(self, cfg: ConfigEnvio, session: requests.Session | None = None, sleep: Callable = time.sleep):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.sleep = sleep
        self._token: str | None = None
        self._escopos: list[str] = []
        self._expira_em = 0.0

    def _obter_token(self, escopo: str = ESCOPO_LEADS) -> str:
        if not (self._token and time.monotonic() < self._expira_em - MARGEM_TOKEN_S):
            self._renovar_token()
        if escopo not in self._escopos:
            raise EnvioError(f"O Client ID não tem o escopo '{escopo}' (Configurações > API Externa)")
        assert self._token is not None
        return self._token

    def _renovar_token(self) -> None:
        resp = self.session.post(
            f"{self.cfg.api_url}/auth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
            },
            timeout=60,
        )
        if resp.status_code in (400, 401, 403):
            raise EnvioError(f"Credenciais recusadas pelo GestaoRevenda ({resp.status_code}): {resp.text[:200]}")
        resp.raise_for_status()
        corpo = resp.json()
        self._token = corpo["access_token"]
        self._escopos = list(corpo.get("escopos") or [])
        self._expira_em = time.monotonic() + int(corpo.get("expires_in", 3600))

    def requisitar(
        self,
        metodo: str,
        caminho: str,
        escopo: str,
        corpo: dict[str, Any] | None = None,
        *,
        tentativas: int | None = None,
        timeout: float | None = None,
        rotulo: str = "Requisição",
    ) -> dict[str, Any]:
        """Chamada autenticada com novas tentativas para rede/5xx/429 e renovação de token em 401.
        4xx (fora 401/429) é EnvioError: não adianta repetir."""
        tentativas = tentativas or self.cfg.tentativas
        ultimo_erro: Exception | None = None
        for tentativa in range(1, tentativas + 1):
            try:
                resp = self.session.request(
                    metodo,
                    f"{self.cfg.api_url}{caminho}",
                    json=corpo,
                    headers={"Authorization": f"Bearer {self._obter_token(escopo)}"},
                    timeout=timeout or self.cfg.timeout,
                )
                if resp.status_code == 401:
                    self._token = None  # token revogado/expirado antes da hora: renova e repete
                    raise requests.HTTPError("401", response=resp)
                if resp.status_code in (429,) or resp.status_code >= 500:
                    raise requests.HTTPError(f"{resp.status_code}", response=resp)
                if resp.status_code == 404:
                    raise EnvioError(
                        f"{rotulo}: {caminho} não existe no GestaoRevenda (404) — o backend está desatualizado?"
                    )
                if resp.status_code >= 400:
                    raise EnvioError(f"{rotulo} recusado ({resp.status_code}): {resp.text[:500]}")
                return resp.json()
            except EnvioError:
                raise
            except (requests.RequestException, ValueError) as e:
                ultimo_erro = e
                if tentativa == tentativas:
                    break
                espera = self.cfg.espera_base * 2 ** (tentativa - 1)
                logger.warning(f"{rotulo} falhou (tentativa {tentativa}/{tentativas}): {e} — nova em {espera:.0f}s")
                self.sleep(espera)
        raise RuntimeError(f"{rotulo} falhou após {tentativas} tentativas: {ultimo_erro}")

    def importar(self, rows: list[dict], data_base: str) -> dict:
        """Um lote de leads (escopo "leads")."""
        payload = {
            "origem": ORIGEM,
            "dataBase": data_base,
            "enfileirarConsultaCnpj": self.cfg.enfileirar_cnpja,
            "rows": rows,
        }
        corpo = self.requisitar("POST", "/leads/importar", ESCOPO_LEADS, payload, rotulo="Lote")
        if "resultados" not in corpo:
            raise EnvioError(
                "Resposta sem 'resultados' — o backend do GestaoRevenda não tem a importação com origem "
                "(LeadsService.importarReceita). Atualize o backend antes de enviar."
            )
        return corpo


def linha_para_payload(linha: dict) -> dict:
    """Linha de exportar_prospeccao → CsvLeadRowDto do GestaoRevenda (sem os campos vazios)."""
    tels = linha.get("telefones") or []
    row = {
        "razao_social": linha["razao_social"],
        "nome_fantasia": linha.get("nome_fantasia"),
        "cnpj": linha["cnpj"],
        "uf": linha.get("uf"),
        "municipio_nome": linha.get("municipio_nome"),
        "situacao_cadastral": linha.get("situacao"),
        "cnae_codigo": linha.get("cnae_codigo"),
        "cnae_descricao": linha.get("cnae_descricao"),
        "cnaes_secundarios": linha.get("cnaes_secundarios") or None,
        "data_inicio_atividade": linha.get("data_inicio_atividade"),
        "data_situacao": linha.get("data_situacao"),
        "matriz_filial": linha.get("matriz_filial"),
        "porte": linha.get("porte"),
        "natureza_juridica": linha.get("natureza_juridica"),
        "capital_social": linha.get("capital_social"),
        "simples_nacional": linha.get("simples_nacional"),
        "mei": linha.get("mei"),
        "logradouro": linha.get("logradouro"),
        "numero": linha.get("numero"),
        "complemento": linha.get("complemento"),
        "bairro": linha.get("bairro"),
        "cep": linha.get("cep"),
        "endereco_completo": linha.get("endereco_completo"),
        "telefones": tels or None,
        "telefone": tels[0]["numero"] if tels else None,
        "email": linha.get("email"),
    }
    return {k: v for k, v in row.items() if v is not None}


def data_da_base(mes_base: str | None) -> str:
    """'2026-10' → '2026-10-01' (a Receita publica por mês; o dia exato não vem na listagem)."""
    if not mes_base or len(mes_base) != 7:
        raise EnvioError(f"Mês da base inválido: {mes_base!r}")
    return f"{mes_base}-01"


def _lotes(itens: list[dict], tamanho: int) -> Iterable[list[dict]]:
    for i in range(0, len(itens), tamanho):
        yield itens[i : i + tamanho]


def enviar(
    linhas: list[dict],
    mes_base: str | None,
    estado: EstadoProspeccao,
    execucao_id: int,
    cfg: ConfigEnvio,
    pasta_backup: Path = Path("saida"),
    cliente: ClienteGestaoRevenda | None = None,
    ao_lote: Callable[[ResultadoEnvio], None] | None = None,
) -> ResultadoEnvio:
    """Envia em lotes, registra cada lote no estado local e grava o CSV de backup do que foi enviado.
    `ao_lote` é chamado depois de cada lote (andamento na tela local)."""
    cliente = cliente or ClienteGestaoRevenda(cfg)
    data_base = data_da_base(mes_base)
    res = ResultadoEnvio()
    enviadas: list[dict] = []
    try:
        for lote in _lotes(linhas, cfg.lote):
            corpo = cliente.importar([linha_para_payload(linha) for linha in lote], data_base)
            por_cnpj: dict[str, list[str]] = {}
            for r in corpo["resultados"]:
                por_cnpj.setdefault(r.get("cnpj") or "", []).append(r["resultado"])
            itens = []
            for linha in lote:
                # Primeiro resultado daquele CNPJ no lote (o backend devolve na mesma ordem)
                resultados = por_cnpj.get(linha["cnpj"]) or ["ERRO"]
                resultado = resultados.pop(0)
                itens.append(ItemResultado(linha["cnpj"], linha["cnpj_basico"], linha["novidade"], resultado))
                enviadas.append({**linha, "resultado": resultado})
            estado.registrar_resultados(execucao_id, itens)
            res.lotes += 1
            res.enviadas += len(lote)
            res.importados += int(corpo.get("importados", 0))
            res.duplicados += int(corpo.get("duplicados", 0))
            res.bloqueados += int(corpo.get("bloqueados", 0))
            res.erros += int(corpo.get("erros", 0))
            res.detalhes_erro += [d for d in corpo.get("detalhes", []) if d.startswith("ERRO")]
            logger.info(
                f"Lote {res.lotes}: {len(lote)} enviadas — {corpo.get('importados', 0)} importadas "
                f"({corpo.get('bloqueados', 0)} bloqueadas), {corpo.get('duplicados', 0)} duplicadas, "
                f"{corpo.get('erros', 0)} erros"
            )
            if ao_lote is not None:
                ao_lote(res)
    finally:
        # Backup mesmo se um lote falhar no meio: mostra exatamente o que chegou ao GestaoRevenda
        if enviadas:
            nome = f"enviados_{mes_base}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            res.backup = escrever_csv(enviadas, pasta_backup / nome, colunas=[*COLUNAS_CSV, "resultado"])
            logger.info(f"Backup do envio: {res.backup}")
    for d in res.detalhes_erro[:20]:
        logger.warning(f"  {d}")
    return res
