"""Resultado das execuções e sinal de vida do serviço → GestaoRevenda.

O GestaoRevenda guarda o histórico (Configurações → Prospecção Receita → Execuções), manda o aviso
por e-mail pela conta escolhida na tela e cria uma Demanda Interna se o serviço ficar mais de 8
dias sem dar sinal. Nenhuma senha de SMTP fica no pipeline.

API Client do .env, escopo "prospeccao-execucoes" (só gravar execução e sinal):
    POST /prospeccao-receita/servico/execucoes   ao terminar cada execução (ou ao pulá-la)
    POST /prospeccao-receita/servico/sinal       a cada verificação diária e ao iniciar o serviço

Tudo passa pela fila `fila_gestaorevenda` do estado local: se o GestaoRevenda não responder, o item
fica lá e vai no próximo ciclo. A `chave` de cada execução torna o reenvio idempotente (o
GestaoRevenda não duplica a execução nem repete o e-mail).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any, cast

from config_prospeccao import ConfigProspeccao
from estado_prospeccao import EstadoProspeccao

logger = logging.getLogger(__name__)

ESCOPO_EXECUCOES = "prospeccao-execucoes"
CAMINHO_EXECUCOES = "/prospeccao-receita/servico/execucoes"
CAMINHO_SINAL = "/prospeccao-receita/servico/sinal"
STATUS_REPORTADOS = ("OK", "PARCIAL", "SEM_ENVIO", "FALHOU")
LIMITE_LISTA = 200


def computador() -> str:
    return os.getenv("COMPUTERNAME") or os.getenv("HOSTNAME") or "?"


def versao_pipeline() -> str:
    try:
        return metadata.version("cnpj-data-pipeline")
    except metadata.PackageNotFoundError:
        return "?"


def _com_fuso(texto: str | None) -> str | None:
    """Datas do estado local são hora local sem fuso; o GestaoRevenda precisa do fuso para não
    deslocar 3 horas."""
    if not texto:
        return None
    try:
        return datetime.fromisoformat(texto).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return None


def _agora() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _fim_do_log(caminho: str | None, n: int = 30) -> str | None:
    if not caminho or not Path(caminho).exists():
        return None
    with open(caminho, encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-n:])[-6000:]


def _soma(valor: Any) -> int:
    if isinstance(valor, dict):
        return sum(int(v or 0) for v in cast(dict[str, Any], valor).values())
    return int(valor or 0)


def a_enviar_pendentes(execucao: dict[str, Any]) -> int | None:
    """Quantas empresas uma execução com envio mandaria, segundo esta execução: tudo o que estava
    selecionado (SEM_ENVIO), o que o --limite deixou de fora (PARCIAL) ou nada (OK)."""
    c: dict[str, Any] = execucao.get("contagens") or {}
    a_enviar = c.get("a_enviar", c.get("selecionadas"))
    if a_enviar is None:
        return None
    if execucao["status"] == "OK":
        return 0
    if execucao["status"] == "PARCIAL":
        envio: dict[str, Any] = c.get("envio") or {}
        return max(int(a_enviar) - int(envio.get("enviadas", 0)), 0)
    return int(a_enviar)


def payload_execucao(
    execucao: dict[str, Any], origem: str, instancia: str, cfg: ConfigProspeccao | None = None
) -> dict[str, Any]:
    """POST /execucoes a partir de uma execução do estado local (ver rodar_mensal)."""
    c: dict[str, Any] = execucao.get("contagens") or {}
    envio: dict[str, Any] = c.get("envio") or {}
    status = execucao["status"] if execucao["status"] in STATUS_REPORTADOS else "FALHOU"
    detalhes: dict[str, Any] = {
        k: c[k]
        for k in ("limite", "janela_inicio_atividade_meses", "csv_lista", "log", "a_enviar", "modo")
        if c.get(k) is not None
    }
    if envio.get("backup"):
        detalhes["backup"] = envio["backup"]
    if status == "FALHOU":
        fim = _fim_do_log(c.get("log"))
        if fim:
            detalhes["fimDoLog"] = fim
    return {
        "chave": f"exec:{instancia}:{execucao['id']}",
        "origem": origem,
        "computador": computador(),
        "iniciadaEm": _com_fuso(execucao.get("iniciada_em")),
        "terminadaEm": _com_fuso(execucao.get("finalizada_em")),
        "mesBase": execucao.get("mes_base") or c.get("mes_base"),
        "status": status,
        "envioDesligado": c.get("envio_desligado"),
        "lidas": _soma(c.get("linhas_lidas")),
        "filtradas": _soma(c.get("linhas_filtradas")),
        "exportadas": int(c.get("exportadas") or 0),
        "novas": int(c.get("novas") or 0),
        "enviadas": int(envio.get("enviadas") or 0),
        "importadas": int(envio.get("importados") or 0),
        "duplicadas": int(envio.get("duplicados") or 0),
        "bloqueadas": int(envio.get("bloqueados") or 0),
        "recusadas": int(envio.get("erros") or 0),
        "duracaoMin": c.get("duracao_min"),
        "erro": (str(c["erro"])[:4000] if c.get("erro") else None),
        "configVersao": c.get("config_versao", cfg.versao if cfg else None),
        "filtroVersao": c.get("filtro_versao", cfg.filtro_versao if cfg else None),
        "origemConfig": c.get("origem_config", cfg.origem if cfg else None),
        "municipiosNaoReconhecidos": list(c.get("municipios_nao_reconhecidos") or [])[:LIMITE_LISTA],
        "detalhes": detalhes,
    }


def payload_sem_execucao(
    chave: str, origem: str, mes: str | None, status: str, erro: str, cfg: ConfigProspeccao
) -> dict[str, Any]:
    """Execução que nem começou: disco insuficiente, ou rodar_mensal morreu sem registrar nada."""
    agora = _agora()
    return {
        "chave": chave,
        "origem": origem,
        "computador": computador(),
        "iniciadaEm": agora,
        "terminadaEm": agora,
        "mesBase": mes,
        "status": status,
        "erro": erro[:4000],
        "configVersao": cfg.versao,
        "filtroVersao": cfg.filtro_versao,
        "origemConfig": cfg.origem,
        "detalhes": {},
    }


def payload_sinal(
    estado: EstadoProspeccao,
    cfg: ConfigProspeccao,
    *,
    envio_desligado: str | None,
    proxima: datetime | None,
    mes_receita: str | None = None,
) -> dict[str, Any]:
    ultima_verificacao = (estado.ultimas_verificacoes(1) or [None])[0]
    concluida = estado.ultima_com_status("OK", "PARCIAL", "SEM_ENVIO")
    pendentes = a_enviar_pendentes(concluida) if concluida else None
    c: dict[str, Any] = (concluida or {}).get("contagens") or {}
    payload: dict[str, Any] = {
        "computador": computador(),
        "versaoPipeline": versao_pipeline(),
        "configVersaoAplicada": cfg.versao,
        "origemConfig": cfg.origem,
        "envioDesligadoMotivo": envio_desligado,
        "proximaVerificacao": proxima.astimezone().isoformat(timespec="seconds") if proxima else None,
        "mesReceita": mes_receita or (ultima_verificacao or {}).get("mes_receita"),
        "pendentesEnvio": pendentes,
        "pendentesMes": (concluida or {}).get("mes_base") if pendentes is not None else None,
        "pendentesFiltroVersao": c.get("filtro_versao") if pendentes is not None else None,
        "pendentesContadoEm": _com_fuso((concluida or {}).get("finalizada_em")) if pendentes is not None else None,
        "municipiosNaoReconhecidos": list(c.get("municipios_nao_reconhecidos") or [])[:LIMITE_LISTA],
    }
    if ultima_verificacao:
        payload["ultimaVerificacao"] = {
            "em": _com_fuso(ultima_verificacao["em"]) or ultima_verificacao["em"],
            "resultado": ultima_verificacao["resultado"],
            "mes": ultima_verificacao["mes_receita"],
            "detalhe": (ultima_verificacao["detalhe"] or "")[:1000] or None,
        }
    return payload


def enfileirar_execucao(estado: EstadoProspeccao, payload: dict[str, Any]) -> None:
    estado.enfileirar("execucao", payload["chave"], payload)


def enfileirar_sinal(estado: EstadoProspeccao, payload: dict[str, Any]) -> None:
    estado.enfileirar("sinal", "sinal", payload)


def enviar_fila(estado: EstadoProspeccao, cliente: Any = None) -> tuple[int, int]:
    """Manda o que estiver na fila. Devolve (enviados, que ficaram). Nunca levanta exceção."""
    from enviar_gestaorevenda import ClienteGestaoRevenda, ConfigEnvio, EnvioError, credenciais_faltando

    itens = estado.pendentes_fila()
    if not itens:
        return 0, 0
    if cliente is None:
        if credenciais_faltando():
            return 0, len(itens)
        cliente = ClienteGestaoRevenda(ConfigEnvio.from_env())
    enviados = 0
    for item_id, tipo, chave, payload in itens:
        caminho = CAMINHO_SINAL if tipo == "sinal" else CAMINHO_EXECUCOES
        try:
            cliente.requisitar("POST", caminho, ESCOPO_EXECUCOES, payload, tentativas=2, timeout=60, rotulo=tipo)
        except EnvioError as e:
            # Credencial, escopo ou backend sem a rota (404): resolve-se do lado do GestaoRevenda,
            # então tudo fica na fila até lá. Outro 4xx (payload recusado) não muda repetindo: sai
            # da fila e fica no log.
            msg = str(e)
            logger.warning(f"GestaoRevenda recusou {tipo} {chave}: {msg}")
            estado.falha_na_fila(item_id, msg)
            if any(t in msg for t in ("escopo", "(404)", "Credenciais")):
                break
            estado.remover_da_fila(item_id)
            continue
        except Exception as e:  # rede/5xx: para aqui e tenta tudo de novo no próximo ciclo
            logger.warning(f"GestaoRevenda não respondeu ({tipo}): {e} — fica na fila")
            estado.falha_na_fila(item_id, str(e))
            break
        estado.remover_da_fila(item_id)
        enviados += 1
    return enviados, estado.tamanho_fila()
