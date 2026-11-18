#!/usr/bin/env python3
"""Tela local "Prospecção Receita": filtros, resultado, Excel, envio ao GestaoRevenda e histórico.

O serviço Windows (servico_prospeccao.py) atende esta página em http://127.0.0.1:8765, e o ícone
"Prospecção Receita" da área de trabalho só abre o navegador nela. Sem o serviço (desenvolvimento):

    python tela_local.py              # sobe a página e abre o navegador
    python tela_local.py --nao-abrir

Consultas: consulta_base.py (DuckDB sobre a base rápida do mês). Envio: o mesmo do envio
automático (enviar_gestaorevenda.enviar: lotes, sem duplicar, bloqueados recusados), registrado
como execução com modo 'tela' e informado ao GestaoRevenda com origem "tela" (sem e-mail).

Segurança:
- só escuta em 127.0.0.1 (outro computador da rede não abre), com a porta exclusiva no Windows;
- recusa Host diferente de 127.0.0.1/localhost (outro site não usa a página por DNS rebinding);
- POST exige o cabeçalho X-Prospeccao e JSON, e recusa Origin de outro site (um site aberto no
  navegador não consegue mandar enviar leads nem atualizar a base);
- acima de 500 empresas, o envio só sai com o número digitado na tela (conferido aqui também).

Uma coisa por vez: o envio e a atualização da base usam a mesma trava do rodar_mensal; com o
serviço já trabalhando, a tela avisa e não faz nada.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event
from typing import IO, Any, cast
from urllib.parse import urlsplit

import aviso_gestaorevenda as aviso
from consulta_base import (
    LIMITE_EXCEL,
    LIMITE_PREVIA,
    SITUACOES_TELA,
    Consulta,
    Filtros,
    FiltrosInvalidosError,
    InfoBase,
    SemBaseError,
    gerar_excel,
    info_base,
    linha_previa,
    mes_por_extenso,
)
from estado_prospeccao import EstadoProspeccao

logger = logging.getLogger("tela")

RAIZ = Path(__file__).resolve().parent
PASTA_TELA = RAIZ / "tela"
PORTA = 8765
LIMITE_CONFIRMACAO = 500  # acima disso, o envio pede o número digitado
LIMITE_CORPO = 1024 * 1024
CABECALHO_POST = "X-Prospeccao"
GB = 1024**3
ARQUIVOS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/icone.ico": ("icone.ico", "image/x-icon"),
    "/favicon.ico": ("icone.ico", "image/x-icon"),
}
CABECALHOS_SEGURANCA = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}


class ErroTela(Exception):
    """Erro com mensagem para a pessoa (vai para a tela como está)."""

    def __init__(self, status: int, mensagem: str):
        super().__init__(mensagem)
        self.status = status
        self.mensagem = mensagem


def _agora() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _mes_mais_recente_receita() -> str:
    from downloader import Downloader
    from rodar_mensal import config_pipeline

    return Downloader(config_pipeline()).get_latest_directory()


def _numero(valor: Any, nome: str) -> int:
    try:
        return int(valor)
    except (TypeError, ValueError) as e:
        raise ErroTela(400, f"{nome} inválido") from e


class App:
    """O que a página faz. Sem HTTP aqui: os testes chamam os métodos direto."""

    def __init__(self, raiz_parquet: Path, temp_dir: Path, parar: Event):
        self.raiz_parquet = raiz_parquet
        self.temp_dir = temp_dir
        self.parar = parar
        self._lock = threading.Lock()
        self.atualizacao: dict[str, Any] | None = None
        self.envio: dict[str, Any] | None = None
        self._opcoes: tuple[Path, dict[str, Any]] | None = None
        # Substituíveis nos testes
        self.mes_mais_recente: Callable[[], str] = _mes_mais_recente_receita
        self.rodar_base: Callable[[str, bool], int] = self._rodar_base_subprocesso
        self.cliente_envio: Any = None
        self.disco_insuficiente: Callable[[], str | None] = self._checar_disco

    # ── Base ──────────────────────────────────────────────────────────────────

    def base(self) -> InfoBase:
        base = info_base(self.raiz_parquet)
        if base is None:
            raise ErroTela(
                409, 'Ainda não há base carregada. Clique em "Atualizar base agora" (leva cerca de 30 minutos).'
            )
        return base

    def _enviados(self) -> list[str]:
        with EstadoProspeccao() as estado:
            return estado.cnpjs_ja_enviados()

    # ── GET ───────────────────────────────────────────────────────────────────

    def estado(self) -> dict[str, Any]:
        from enviar_gestaorevenda import credenciais_faltando

        base = info_base(self.raiz_parquet)
        faltando = credenciais_faltando()
        return {
            "base": base.como_json() if base else None,
            "atualizacao": self._atualizacao_json(),
            "envio": dict(self.envio) if self.envio else None,
            "gestaorevenda": {
                "configurado": not faltando,
                "aviso": (
                    "O envio ao GestaoRevenda ainda não está configurado neste computador "
                    f"(falta preencher {', '.join(faltando)} no arquivo .env). Dá para ver o resultado e "
                    "baixar em Excel, mas não enviar."
                )
                if faltando
                else None,
            },
            "limite_confirmacao": LIMITE_CONFIRMACAO,
            "limite_excel": LIMITE_EXCEL,
        }

    def _atualizacao_json(self) -> dict[str, Any] | None:
        if self.atualizacao is None:
            return None
        dados = dict(self.atualizacao)
        if not dados.get("fim"):
            from servico_prospeccao import tamanho_pasta

            dados["decorrido_min"] = round((time.time() - dados["inicio_ts"]) / 60)
            dados["baixado_gb"] = round(tamanho_pasta(self.temp_dir) / GB, 1) if self.temp_dir.exists() else 0.0
        dados.pop("inicio_ts", None)
        return dados

    def opcoes(self) -> dict[str, Any]:
        base = self.base()
        if self._opcoes is None or self._opcoes[0] != base.pasta:
            with Consulta(base.pasta) as consulta:
                dados = {"cnaes": consulta.cnaes(), "municipios": consulta.municipios()}
            self._opcoes = (base.pasta, dados)
        return {
            "uf": base.uf,
            "mes": base.mes,
            **self._opcoes[1],
            "situacoes": [{"codigo": c, "nome": n} for c, n in SITUACOES_TELA.items()],
        }

    def historico(self) -> list[dict[str, Any]]:
        with EstadoProspeccao() as estado:
            return estado.varreduras(100)

    # ── Ver resultado ─────────────────────────────────────────────────────────

    def _resumo(self, f: Filtros) -> str:
        opcoes = self.opcoes()
        cnaes = {c["codigo"]: c["descricao"] for c in opcoes["cnaes"]}
        municipios = {m["codigo"]: m["nome"] for m in opcoes["municipios"]}
        return f.resumo(cnaes, municipios)

    def consultar(self, corpo: dict[str, Any]) -> dict[str, Any]:
        f = Filtros.de_json(corpo.get("filtros"))
        base = self.base()
        with Consulta(base.pasta, self._enviados()) as consulta:
            contagens = consulta.contar(f)
            previa = [linha_previa(linha) for linha in consulta.linhas(f, mes_base=base.mes, limite=LIMITE_PREVIA)]
        resumo = self._resumo(f)
        with EstadoProspeccao() as estado:
            varredura_id = estado.registrar_varredura(base.mes, f.como_json(), resumo, contagens)
        return {
            "varredura_id": varredura_id,
            "mes_base": base.mes,
            "mes_texto": mes_por_extenso(base.mes),
            "resumo": resumo,
            "filtros": f.como_json(),
            "contagens": contagens,
            "previa": previa,
        }

    def excel(self, corpo: dict[str, Any]) -> tuple[Path, str]:
        """Gera o .xlsx num arquivo temporário. Quem chama apaga depois de mandar."""
        f = Filtros.de_json(corpo.get("filtros"))
        base = self.base()
        with Consulta(base.pasta, self._enviados()) as consulta:
            total = consulta.contar(f)["encontradas"]
            if total == 0:
                raise ErroTela(400, "Nenhuma empresa encontrada com esses filtros.")
            if total > LIMITE_EXCEL:
                raise ErroTela(
                    400,
                    f"São {total:,} empresas, mais do que o Excel aceita ({LIMITE_EXCEL:,}). "
                    "Escolha atividades ou cidades para diminuir.".replace(",", "."),
                )
            linhas = consulta.linhas(f, mes_base=base.mes)
        descritor, nome_tmp = tempfile.mkstemp(suffix=".xlsx", prefix="prospeccao_")
        os.close(descritor)
        caminho = Path(nome_tmp)
        try:
            gerar_excel(linhas, caminho)
        except Exception:
            caminho.unlink(missing_ok=True)
            raise
        return caminho, f"prospeccao_{base.mes}_{time.strftime('%Y%m%d_%H%M')}.xlsx"

    # ── Enviar ────────────────────────────────────────────────────────────────

    def enviar(self, corpo: dict[str, Any]) -> dict[str, Any]:
        from enviar_gestaorevenda import credenciais_faltando
        from main import _try_lock  # pyright: ignore[reportPrivateUsage] - mesma trava do pipeline
        from rodar_mensal import caminho_trava

        f = Filtros.de_json(corpo.get("filtros"))
        quantidade = _numero(corpo.get("quantidade"), "Quantidade")
        varredura_id = _numero(corpo.get("varredura_id") or 0, "Varredura") or None
        if credenciais_faltando():
            raise ErroTela(400, cast(str, self.estado()["gestaorevenda"]["aviso"]))
        if quantidade <= 0:
            raise ErroTela(400, "Não há empresas novas para enviar.")
        if quantidade > LIMITE_CONFIRMACAO and str(corpo.get("confirmacao") or "").strip() != str(quantidade):
            raise ErroTela(
                400,
                f"Para enviar mais de {LIMITE_CONFIRMACAO} empresas, "
                f"digite o número exato ({quantidade}) para confirmar.",
            )
        with self._lock:
            if self.envio is not None and not self.envio.get("fim"):
                raise ErroTela(409, "Já há um envio em andamento. Espere terminar.")
            base = self.base()
            if base.mes != corpo.get("mes_base"):
                raise ErroTela(409, 'A base foi atualizada depois desta busca. Clique em "Ver resultado" de novo.')
            with Consulta(base.pasta, self._enviados()) as consulta:
                contagens = consulta.contar(f)
                linhas = consulta.linhas(f, mes_base=base.mes, so_novas=True)
            if len(linhas) != quantidade:
                raise ErroTela(
                    409,
                    f"O resultado mudou desde a busca (agora são {len(linhas)} novas). "
                    'Clique em "Ver resultado" de novo.',
                )
            caminho = caminho_trava()
            caminho.parent.mkdir(parents=True, exist_ok=True)
            trava = open(caminho, "a")  # noqa: SIM115 - fica aberta (travada) até o envio terminar
            trava.seek(0)  # o msvcrt trava a partir da posição atual: o mesmo byte que o rodar_mensal
            if not _try_lock(trava):
                trava.close()
                raise ErroTela(
                    409, "O serviço está trabalhando na base agora (atualização ou envio automático). Tente mais tarde."
                )
            resumo = self._resumo(f)
            self.envio = {
                "inicio": _agora(),
                "fim": None,
                "total": len(linhas),
                "enviadas": 0,
                "ok": None,
                "resultado": None,
                "erro": None,
                "varredura_id": varredura_id,
            }
        threading.Thread(
            target=self._enviar_em_segundo_plano,
            args=(linhas, base.mes, resumo, contagens, varredura_id, trava),
            daemon=True,
            name="tela-envio",
        ).start()
        return {"iniciado": True, "total": len(linhas)}

    def _enviar_em_segundo_plano(
        self,
        linhas: list[dict[str, Any]],
        mes: str,
        resumo: str,
        contagens_busca: dict[str, int],
        varredura_id: int | None,
        trava: IO[str],
    ) -> None:
        from enviar_gestaorevenda import ConfigEnvio, ResultadoEnvio
        from enviar_gestaorevenda import enviar as enviar_lotes  # pyright: ignore[reportUnknownVariableType]

        envio = cast(dict[str, Any], self.envio)

        def progresso(res: ResultadoEnvio) -> None:
            envio["enviadas"] = res.enviadas

        try:
            with EstadoProspeccao() as estado:
                estado.marcar_interrompidas()  # com a trava na mão, nada está de fato em andamento
                estado.classificar(linhas)  # pyright: ignore[reportUnknownMemberType] - preenche `novidade`
                execucao_id = estado.iniciar_execucao(mes, "tela")
                contagens: dict[str, Any] = {
                    "mes_base": mes,
                    "modo": "tela",
                    "filtros_tela": resumo,
                    "exportadas": contagens_busca["encontradas"],
                    "novas": len(linhas),
                    "selecionadas": len(linhas),
                    "a_enviar": len(linhas),
                }
                status = "FALHOU"
                inicio = time.monotonic()
                try:
                    res = enviar_lotes(
                        linhas, mes, estado, execucao_id, ConfigEnvio.from_env(), cliente=self.cliente_envio,
                        ao_lote=progresso,
                    )  # fmt: skip
                    contagens["envio"] = res.como_dict()
                    status = "OK"
                    envio.update(ok=True, resultado=_resultado_tela(res.como_dict()))
                except Exception as e:
                    logger.exception(f"Envio pela tela falhou: {e}")
                    contagens["erro"] = str(e)[:2000]
                    feitos = estado.resultados_da_execucao(execucao_id)
                    parcial = {
                        "enviadas": sum(feitos.values()),
                        "importados": feitos.get("IMPORTADO", 0) + feitos.get("BLOQUEADO", 0),
                        "duplicados": feitos.get("DUPLICADO", 0),
                        "bloqueados": feitos.get("BLOQUEADO", 0),
                        "erros": feitos.get("ERRO", 0),
                    }
                    contagens["envio"] = parcial
                    envio.update(
                        ok=False,
                        resultado=_resultado_tela(parcial),
                        erro=(
                            "O envio parou no meio: o GestaoRevenda não respondeu ou recusou. O que já foi "
                            'aceito ficou registrado e não será enviado de novo; clique em "Ver resultado" '
                            f"e envie o restante. Detalhe: {str(e)[:300]}"
                        ),
                    )
                finally:
                    contagens["duracao_min"] = round((time.monotonic() - inicio) / 60, 1)
                    estado.finalizar_execucao(execucao_id, status, contagens)
                    if varredura_id:
                        estado.registrar_envio_varredura(
                            varredura_id, execucao_id, {"ok": status == "OK", **(envio.get("resultado") or {})}
                        )
                    self._avisar_gestaorevenda(estado, execucao_id)
        except Exception as e:  # erro antes do envio (estado local, configuração)
            logger.exception(f"Envio pela tela não começou: {e}")
            envio.update(ok=False, erro=f"O envio não começou: {str(e)[:300]}")
        finally:
            envio["fim"] = _agora()
            _soltar(trava)

    def _avisar_gestaorevenda(self, estado: EstadoProspeccao, execucao_id: int) -> None:
        """Histórico de Execuções do GestaoRevenda (origem "tela", sem e-mail). Nunca falha o envio."""
        try:
            execucao = estado.execucao(execucao_id)
            if execucao is not None:
                aviso.enfileirar_execucao(estado, aviso.payload_execucao(execucao, "tela", estado.instancia()))
                aviso.enviar_fila(estado, self.cliente_envio)
        except Exception as e:
            logger.warning(f"Histórico do envio pela tela não foi ao GestaoRevenda agora (fica na fila): {e}")

    # ── Atualizar base ────────────────────────────────────────────────────────

    def _checar_disco(self) -> str | None:
        import config_prospeccao
        from servico_prospeccao import checar_disco

        cfg = config_prospeccao.obter()
        return checar_disco(cfg.disco_min_gb, self.temp_dir, [Path(tempfile.gettempdir()), self.raiz_parquet])

    def _rodar_base_subprocesso(self, mes: str, forcar: bool) -> int:
        from servico_prospeccao import rodar_fluxo

        return rodar_fluxo(mes, self.parar, so_base=True, recarregar=forcar)

    def atualizar_base(self, corpo: dict[str, Any]) -> dict[str, Any]:
        forcar = corpo.get("forcar") is True
        with self._lock:
            if self.atualizacao is not None and not self.atualizacao.get("fim"):
                raise ErroTela(409, "A base já está sendo atualizada.")
            try:
                mes = self.mes_mais_recente()
            except Exception as e:
                logger.warning(f"Não consegui listar os meses da Receita: {e}")
                raise ErroTela(502, "Não consegui falar com o site da Receita agora. Tente de novo mais tarde.") from e
            base = info_base(self.raiz_parquet)
            if base is not None and base.mes == mes and not forcar:
                return {"ja_atualizada": True, "mes": mes, "mes_texto": mes_por_extenso(mes)}
            motivo = self.disco_insuficiente()
            if motivo:
                raise ErroTela(400, f"Pouco espaço em disco para baixar a base: {motivo}")
            self.atualizacao = {
                "inicio": _agora(),
                "inicio_ts": time.time(),
                "mes": mes,
                "mes_texto": mes_por_extenso(mes),
                "forcar": forcar,
                "fim": None,
                "ok": None,
                "mensagem": None,
            }
        threading.Thread(target=self._atualizar_em_segundo_plano, args=(mes, forcar), daemon=True).start()
        return {"iniciado": True, "mes": mes, "mes_texto": mes_por_extenso(mes)}

    def _atualizar_em_segundo_plano(self, mes: str, forcar: bool) -> None:
        from rodar_mensal import CODIGO_INTERROMPIDA, CODIGO_OCUPADO

        atualizacao = cast(dict[str, Any], self.atualizacao)
        try:
            codigo = self.rodar_base(mes, forcar)
        except Exception as e:
            logger.exception(f"Atualização da base pela tela falhou: {e}")
            codigo = -1
        mensagens = {
            0: (True, f"Base de {mes_por_extenso(mes)} pronta."),
            CODIGO_OCUPADO: (
                False,
                "O serviço já está trabalhando na base agora (atualização ou envio automático). Tente mais tarde.",
            ),
            CODIGO_INTERROMPIDA: (
                False,
                "A atualização foi interrompida (o computador ou o serviço foi desligado). "
                "Clique de novo para continuar de onde parou.",
            ),
        }
        ok, mensagem = mensagens.get(
            codigo,
            (
                False,
                "A atualização falhou. Tente de novo; se repetir, avise o suporte "
                "(detalhes em logs\\rodar_mensal_saida.txt).",
            ),
        )
        atualizacao.update(ok=ok, mensagem=mensagem, fim=_agora())


def _soltar(trava: IO[str]) -> None:
    """Solta a trava do rodar_mensal tomada com _try_lock (mesmo processo: precisa soltar explícito)."""
    try:
        if sys.platform == "win32":
            import msvcrt

            trava.seek(0)
            msvcrt.locking(trava.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(trava, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        trava.close()


def _resultado_tela(envio: dict[str, Any]) -> dict[str, int]:
    """Importadas inclui as bloqueadas no GestaoRevenda; na tela elas aparecem separadas."""
    bloqueadas = int(envio.get("bloqueados") or 0)
    return {
        "enviadas": int(envio.get("enviadas") or 0),
        "importadas": max(int(envio.get("importados") or 0) - bloqueadas, 0),
        "ja_existiam": int(envio.get("duplicados") or 0),
        "bloqueadas": bloqueadas,
        "recusadas": int(envio.get("erros") or 0),
    }


# ── HTTP ──────────────────────────────────────────────────────────────────────


class _Servidor(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # no Windows, SO_REUSEADDR deixaria outro programa pegar a porta

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # pyright: ignore[reportAttributeAccessIssue]
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name, self.server_port = "127.0.0.1", int(self.server_address[1])


def _json_padrao(valor: Any) -> Any:
    if isinstance(valor, (date, datetime)):
        return valor.isoformat()
    raise TypeError(f"{type(valor).__name__} não é serializável")


def criar_servidor(app: App, porta: int = PORTA) -> ThreadingHTTPServer:
    servidor = _Servidor(("127.0.0.1", porta), BaseHTTPRequestHandler, bind_and_activate=False)
    try:
        servidor.server_bind()
        servidor.server_activate()
    except OSError:
        servidor.server_close()
        raise
    porta_real = servidor.server_port
    hosts = {f"127.0.0.1:{porta_real}", f"localhost:{porta_real}"}
    origens = {f"http://{h}" for h in hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "ProspeccaoReceita"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("%s - %s", self.address_string(), format % args)

        def _enviar(self, status: int, corpo: bytes, tipo: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            for nome, valor in {**CABECALHOS_SEGURANCA, **(extra or {})}.items():
                self.send_header(nome, valor)
            self.end_headers()
            self.wfile.write(corpo)

        def _json(self, status: int, dados: Any) -> None:
            corpo = json.dumps(dados, ensure_ascii=False, default=_json_padrao).encode("utf-8")
            self._enviar(status, corpo, "application/json; charset=utf-8")

        def _erro(self, status: int, mensagem: str) -> None:
            self._json(status, {"erro": mensagem})

        def _host_ok(self) -> bool:
            if (self.headers.get("Host") or "").lower() in hosts:
                return True
            self._erro(403, "Acesso recusado")
            return False

        def _executar(self, acao: Callable[[], Any]) -> None:
            try:
                self._json(200, acao())
            except ErroTela as e:
                self._erro(e.status, e.mensagem)
            except FiltrosInvalidosError as e:
                self._erro(400, str(e))
            except SemBaseError:
                self._erro(409, 'A base está vazia. Clique em "Atualizar base agora".')
            except Exception as e:
                logger.exception(f"Erro na tela ({self.path}): {e}")
                self._erro(500, "Algo deu errado. Tente de novo; se repetir, avise o suporte.")

        def do_GET(self) -> None:
            if not self._host_ok():
                return
            caminho = urlsplit(self.path).path
            if caminho in ARQUIVOS:
                nome, tipo = ARQUIVOS[caminho]
                self._enviar(200, (PASTA_TELA / nome).read_bytes(), tipo)
            elif caminho == "/api/estado":
                self._executar(app.estado)
            elif caminho == "/api/opcoes":
                self._executar(app.opcoes)
            elif caminho == "/api/historico":
                self._executar(app.historico)
            else:
                self._erro(404, "Não encontrado")

        def do_POST(self) -> None:
            if not self._host_ok():
                return
            origem = self.headers.get("Origin")
            if self.headers.get(CABECALHO_POST) != "1" or (origem and origem.lower() not in origens):
                return self._erro(403, "Acesso recusado")
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._erro(415, "Esperado JSON")
            try:
                tamanho = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._erro(400, "Pedido inválido")
            if tamanho > LIMITE_CORPO:
                return self._erro(413, "Pedido grande demais")
            try:
                corpo = json.loads(self.rfile.read(tamanho) or b"{}")
            except ValueError:
                return self._erro(400, "Pedido inválido")
            if not isinstance(corpo, dict):
                return self._erro(400, "Pedido inválido")
            dados = cast(dict[str, Any], corpo)
            caminho = urlsplit(self.path).path
            if caminho == "/api/consultar":
                self._executar(lambda: app.consultar(dados))
            elif caminho == "/api/enviar":
                self._executar(lambda: app.enviar(dados))
            elif caminho == "/api/atualizar-base":
                self._executar(lambda: app.atualizar_base(dados))
            elif caminho == "/api/excel":
                self._excel(dados)
            else:
                self._erro(404, "Não encontrado")

        def _excel(self, dados: dict[str, Any]) -> None:
            try:
                arquivo, nome = app.excel(dados)
            except ErroTela as e:
                return self._erro(e.status, e.mensagem)
            except FiltrosInvalidosError as e:
                return self._erro(400, str(e))
            except Exception as e:
                logger.exception(f"Excel falhou: {e}")
                return self._erro(500, "Não consegui gerar o Excel. Tente de novo.")
            try:
                self._enviar(
                    200,
                    arquivo.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    {"Content-Disposition": f'attachment; filename="{nome}"'},
                )
            finally:
                arquivo.unlink(missing_ok=True)

    servidor.RequestHandlerClass = Handler
    return servidor


def criar_app(parar: Event) -> App:
    from rodar_mensal import config_pipeline

    config = config_pipeline()
    return App(Path(config.parquet_output_dir), Path(config.temp_dir), parar)


def iniciar_em_segundo_plano(parar: Event, porta: int = PORTA) -> ThreadingHTTPServer | None:
    """Sobe a página numa thread do serviço; para quando `parar` for sinalizado."""
    app = criar_app(parar)
    try:
        servidor = criar_servidor(app, porta)
    except OSError as e:
        logger.error(f"Tela local não iniciou: porta {porta} ocupada por outro programa ({e})")
        return None
    threading.Thread(target=servidor.serve_forever, daemon=True, name="tela").start()

    def desligar() -> None:
        parar.wait()
        servidor.shutdown()
        servidor.server_close()

    threading.Thread(target=desligar, daemon=True, name="tela-parada").start()
    logger.info(f"Tela local em http://127.0.0.1:{servidor.server_port}")
    return servidor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tela local da Prospecção Receita (sem o serviço)")
    parser.add_argument("--porta", type=int, default=PORTA)
    parser.add_argument("--nao-abrir", action="store_true", help="Não abre o navegador")
    args = parser.parse_args(argv)
    os.chdir(RAIZ)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    parar = Event()
    servidor = iniciar_em_segundo_plano(parar, args.porta)
    if servidor is None:
        return 1
    url = f"http://127.0.0.1:{servidor.server_port}/"
    print(f"Tela em {url} (Ctrl+C para sair)")
    if not args.nao_abrir:
        webbrowser.open(url)
    try:
        while not parar.wait(1):
            pass
    except KeyboardInterrupt:
        parar.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
