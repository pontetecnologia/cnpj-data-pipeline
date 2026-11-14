"""Envio ao GestaoRevenda e rodar_mensal, contra um GestaoRevenda falso (HTTP local)."""

import csv
import json
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event, Thread

import pytest

from enviar_gestaorevenda import ClienteGestaoRevenda, ConfigEnvio, EnvioError, enviar, linha_para_payload
from estado_prospeccao import CNPJ_INEDITO, FILIAL_NOVA, EstadoProspeccao
from tests.test_prospeccao import source_files as source_files  # noqa: F401 - cenário da Receita


class FakeGR:
    """Estado do GestaoRevenda falso: dedup por CNPJ, um CNPJ bloqueado, falhas programáveis."""

    def __init__(self) -> None:
        self.leads: dict[str, dict] = {}
        self.bloqueados = {"11111111000211"}
        self.tokens_emitidos = 0
        self.falhar_proximos: list[int] = []  # status HTTP a devolver nas próximas chamadas de import
        self.payloads: list[dict] = []
        self.sem_resultados = False


@pytest.fixture
def fake_gr() -> Iterator[tuple[str, FakeGR]]:
    gr = FakeGR()

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, corpo: object) -> None:
            body = json.dumps(corpo).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/auth/token":
                if corpo.get("client_secret") != "segredo":
                    return self._json(401, {"message": "Credenciais inválidas"})
                gr.tokens_emitidos += 1
                return self._json(
                    201, {"access_token": f"t{gr.tokens_emitidos}", "expires_in": 3600, "escopos": ["leads"]}
                )
            assert self.path == "/api/leads/importar"
            if self.headers.get("Authorization") != f"Bearer t{gr.tokens_emitidos}":
                return self._json(401, {"message": "Unauthorized"})
            if gr.falhar_proximos:
                return self._json(gr.falhar_proximos.pop(0), {"message": "falha programada"})
            gr.payloads.append(corpo)
            res = {"importados": 0, "duplicados": 0, "bloqueados": 0, "erros": 0, "detalhes": [], "resultados": []}
            for row in corpo["rows"]:
                if row["cnpj"] in gr.leads:
                    res["duplicados"] += 1
                    res["resultados"].append({"cnpj": row["cnpj"], "resultado": "DUPLICADO"})
                    continue
                gr.leads[row["cnpj"]] = row
                res["importados"] += 1
                bloq = row["cnpj"] in gr.bloqueados
                res["bloqueados"] += bloq
                res["resultados"].append({"cnpj": row["cnpj"], "resultado": "BLOQUEADO" if bloq else "IMPORTADO"})
            if gr.sem_resultados:
                del res["resultados"]
            return self._json(201, res)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/api", gr
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _cfg(api_url: str, **kw: object) -> ConfigEnvio:
    base = {"client_id": "id", "client_secret": "segredo", "lote": 2, "tentativas": 3, "espera_base": 0}
    base.update(kw)
    return ConfigEnvio(api_url=api_url, **base)  # type: ignore[arg-type]


def _linha(cnpj: str, novidade: str = CNPJ_INEDITO) -> dict:
    return {
        "cnpj": cnpj,
        "cnpj_basico": cnpj[:8],
        "novidade": novidade,
        "razao_social": f"EMPRESA {cnpj}",
        "nome_fantasia": None,
        "telefones": [{"tipo": "Fixo", "numero": "(47) 33331234"}],
        "cnaes_secundarios": [],
        "situacao": "ATIVA",
        "simples_nacional": False,
        "uf": "SC",
    }


@pytest.fixture(autouse=True)
def _isolado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    # Config.from_env() acharia o .env real do projeto (load_dotenv procura a partir de config.py)
    monkeypatch.setattr("config.load_dotenv", lambda *a, **k: False)


def test_linha_para_payload_tira_vazios_e_mantem_false() -> None:
    p = linha_para_payload(_linha("11111111000100"))
    assert p["telefone"] == "(47) 33331234" and p["simples_nacional"] is False
    assert "nome_fantasia" not in p and "cnaes_secundarios" not in p
    assert p["situacao_cadastral"] == "ATIVA"


def test_envio_em_lotes_registra_estado_e_backup(fake_gr: tuple[str, FakeGR], tmp_path: Path) -> None:
    api, gr = fake_gr
    gr.leads["33333333000100"] = {}  # já existe no GestaoRevenda
    gr.falhar_proximos = [503]  # primeiro lote falha uma vez e é repetido
    linhas = [_linha("11111111000100"), _linha("11111111000211"), _linha("33333333000100")]
    with EstadoProspeccao(tmp_path / "e.sqlite") as estado:
        execucao = estado.iniciar_execucao("2026-10", "novos")
        res = enviar(linhas, "2026-10", estado, execucao, _cfg(api), pasta_backup=tmp_path)
        assert (res.enviadas, res.importados, res.bloqueados, res.duplicados, res.lotes) == (3, 2, 1, 1, 2)
        assert all(p["origem"] == "RECEITA_FEDERAL" and p["dataBase"] == "2026-10-01" for p in gr.payloads)
        assert all(p["enfileirarConsultaCnpj"] is False for p in gr.payloads)
        assert len(gr.payloads[0]["rows"]) == 2 and len(gr.payloads[1]["rows"]) == 1
        # Tudo definitivo → nada volta como novo; filial de empresa conhecida seria FILIAL_NOVA
        reclass = estado.classificar([dict(linha) for linha in linhas] + [_linha("11111111000300")])
        assert [linha["novidade"] for linha in reclass] == ["JA_ENVIADO"] * 3 + [FILIAL_NOVA]
    assert res.backup is not None
    with open(res.backup, encoding="utf-8-sig", newline="") as f:
        assert [r["resultado"] for r in csv.DictReader(f, delimiter=";")] == ["IMPORTADO", "BLOQUEADO", "DUPLICADO"]


def test_token_renovado_em_401(fake_gr: tuple[str, FakeGR]) -> None:
    api, gr = fake_gr
    cliente = ClienteGestaoRevenda(_cfg(api))
    cliente.importar([linha_para_payload(_linha("11111111000100"))], "2026-10-01")
    gr.tokens_emitidos += 1  # servidor "revoga" o token atual
    cliente._token = "t1"
    cliente.importar([linha_para_payload(_linha("22222222000100"))], "2026-10-01")
    assert gr.tokens_emitidos == 3 and len(gr.leads) == 2


def test_erros_que_nao_adianta_repetir(fake_gr: tuple[str, FakeGR], tmp_path: Path) -> None:
    api, gr = fake_gr
    with pytest.raises(EnvioError, match="Credenciais"):
        ClienteGestaoRevenda(_cfg(api, client_secret="errado")).importar([], "2026-10-01")
    gr.falhar_proximos = [400]
    with pytest.raises(EnvioError, match="recusado"):
        ClienteGestaoRevenda(_cfg(api)).importar([], "2026-10-01")
    gr.sem_resultados = True
    with pytest.raises(EnvioError, match="importarReceita"):
        ClienteGestaoRevenda(_cfg(api)).importar([], "2026-10-01")


def test_falha_persistente_guarda_o_que_ja_foi(fake_gr: tuple[str, FakeGR], tmp_path: Path) -> None:
    api, gr = fake_gr
    linhas = [_linha("11111111000100"), _linha("22222222000100"), _linha("44444444000100")]
    with EstadoProspeccao(tmp_path / "e.sqlite") as estado:
        execucao = estado.iniciar_execucao("2026-10", "novos")
        cliente = ClienteGestaoRevenda(_cfg(api))
        # Primeiro lote passa; o segundo falha nas 3 tentativas
        original = cliente.importar

        def importar(rows: list[dict], data_base: str) -> dict:
            if rows[0]["cnpj"] == "44444444000100":
                gr.falhar_proximos = [503, 503, 503]
            return original(rows, data_base)

        cliente.importar = importar  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="3 tentativas"):
            enviar(linhas, "2026-10", estado, execucao, _cfg(api), pasta_backup=tmp_path, cliente=cliente)
        reclass = estado.classificar([dict(linha) for linha in linhas])
        assert [linha["novidade"] for linha in reclass] == ["JA_ENVIADO", "JA_ENVIADO", CNPJ_INEDITO]
    assert list(tmp_path.glob("enviados_2026-10_*.csv"))  # backup parcial gravado


def test_config_exige_credenciais(monkeypatch: pytest.MonkeyPatch) -> None:
    for nome in ("GR_API_URL", "GR_CLIENT_ID", "GR_CLIENT_SECRET"):
        monkeypatch.delenv(nome, raising=False)
    with pytest.raises(EnvioError, match="GR_API_URL"):
        ConfigEnvio.from_env()


# ── rodar_mensal ponta a ponta: Receita falsa (WebDAV) + GestaoRevenda falso ────────────


def test_rodar_mensal_ponta_a_ponta(
    source_server: tuple[str, Event, list[str]],
    fake_gr: tuple[str, FakeGR],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rodar_mensal

    base_url, _fail, downloads = source_server
    api, gr = fake_gr
    monkeypatch.chdir(tmp_path)
    env = {
        "BASE_URL": base_url,
        "SHARE_TOKEN": "test",
        "OUTPUT_FORMAT": "parquet",
        "PARQUET_OUTPUT_DIR": str(tmp_path / "parquet"),
        "PARQUET_TYPED_OUTPUT": "true",
        "TEMP_DIR": str(tmp_path / "temp"),
        "RETRY_ATTEMPTS": "1",
        "RETRY_DELAY": "0",
        "PROSPECCAO_FILTRO": "true",
        "FILTRO_UF": "SC",
        "FILTRO_MUNICIPIOS": "JOINVILLE",
        "FILTRO_INICIO_ATIVIDADE_MESES": "12",
        "FILTRO_PRIMEIRA_EXECUCAO_MESES": "0",
        "PROSPECCAO_ESTADO": str(tmp_path / "estado.sqlite"),
        "PROSPECCAO_LOGS": str(tmp_path / "logs"),
        "GR_API_URL": api,
        "GR_CLIENT_ID": "id",
        "GR_CLIENT_SECRET": "segredo",
        "GR_LOTE": "1",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("FILTRO_CNAES", raising=False)
    monkeypatch.delenv("FILTRO_SITUACAO", raising=False)

    # Teste com --limite: só 1 enviado, os outros continuam novos
    assert rodar_mensal.executar(["--mes", "2024-01", "--limite", "1"]) == 0
    assert sorted(gr.leads) == ["11111111000100"]
    gr.leads.clear()
    import sqlite3

    with sqlite3.connect(tmp_path / "estado.sqlite") as conn:  # volta ao estado "nunca enviado"
        conn.executescript("DELETE FROM execucao_itens; DELETE FROM cnpjs_enviados; DELETE FROM execucoes;")

    # 1ª execução: janela 0 meses → além dos 2 estabelecimentos de 11111111 (2026), entra também
    # 55555555 (ativa em Joinville, aberta em 2020). O GestaoRevenda falso bloqueia a filial 0002.
    assert rodar_mensal.executar(["--mes", "2024-01", "--force"]) == 0
    assert "/2024-01/Socios0.zip" not in downloads
    assert sorted(gr.leads) == ["11111111000100", "11111111000211", "55555555000100"]
    assert not [p for p in (tmp_path / "temp").rglob("*") if p.is_file() and p.name != ".pipeline.lock"]
    with EstadoProspeccao(tmp_path / "estado.sqlite") as estado:
        ultima = estado.ultima_execucao()
    assert ultima is not None and ultima["status"] == "OK"
    c = ultima["contagens"]
    assert c["janela_inicio_atividade_meses"] == 0
    assert c["linhas_lidas"]["estabelecimentos"] == 6 and c["linhas_filtradas"]["estabelecimentos"] == 3
    assert (c["exportadas"], c["novas"]) == (3, 3)
    assert c["envio"]["enviadas"] == 3 and c["envio"]["importados"] == 3 and c["envio"]["bloqueados"] == 1
    assert list((tmp_path / "logs").glob("rodar_mensal_*.log"))
    assert (tmp_path / "saida" / "prospeccao_2024-01.csv").exists()

    # 2ª execução, mesmo mês: janela passa a 12 meses e não há nada novo para enviar
    gr.payloads.clear()
    assert rodar_mensal.executar(["--mes", "2024-01", "--force"]) == 0
    assert gr.payloads == []
    with EstadoProspeccao(tmp_path / "estado.sqlite") as estado:
        ultima = estado.ultima_execucao()
    assert ultima is not None
    assert ultima["contagens"]["janela_inicio_atividade_meses"] == 12
    assert ultima["contagens"]["selecionadas"] == 0 and ultima["contagens"]["envio"] == {"enviadas": 0}


def test_rodar_mensal_exige_filtro(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import rodar_mensal

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROSPECCAO_FILTRO", "false")
    monkeypatch.setenv("PROSPECCAO_LOGS", str(tmp_path / "logs"))
    assert rodar_mensal.executar([]) == 1
