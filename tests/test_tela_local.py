"""Tela local: base rápida, consultas (consulta_base) e a página (tela_local), contra uma base de
verdade carregada pelo pipeline a partir de uma Receita falsa, e um GestaoRevenda falso."""

from __future__ import annotations

import argparse
import json
import time
import zipfile
from collections.abc import Iterator
from http.client import HTTPConnection
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest

import rodar_mensal
import tela_local
from config import Config
from consulta_base import Consulta, Filtros, FiltrosInvalidosError, gerar_excel, info_base, linha_previa
from estado_prospeccao import EstadoProspeccao
from filtro import FiltroProspeccao
from main import main
from tests.test_envio_gestaorevenda import fake_gr as fake_gr  # noqa: F401 - fixture
from tests.test_prospeccao import estab, simples

MES = "2024-01"


def empresa(basico: str, nome: str, capital: str = "10.000,00", porte: str = "01") -> str:
    return f'"{basico}";"{nome}";"2062";"49";"{capital}";"{porte}";""\n'


@pytest.fixture
def source_files() -> dict[str, dict[str, str]]:
    return {
        "Cnaes.zip": {"CNAECSV.csv": '"4711302";"Supermercados"\n"9999999";"Outra atividade"\n'},
        "Municipios.zip": {"MUNICCSV.csv": '"8179";"JOINVILLE"\n"8105";"BLUMENAU"\n"7535";"CURITIBA"\n'},
        "Naturezas.zip": {"NATJUCSV.csv": '"2062";"Sociedade Empresária Limitada"\n'},
        "Estabelecimentos0.zip": {
            "0.ESTABELE.csv": (
                estab("11111111")  # matriz, Joinville, 2026
                + estab("11111111", "0002", "11", matriz="2", telefone="99887766")  # filial
                + estab("22222222", situacao="08")  # baixada
                + estab("33333333", cnae="9999999")  # outra atividade
                + estab("44444444", municipio="8105", telefone="", email="")  # Blumenau, sem contato, MEI
                + estab("55555555", inicio="20200101")  # antiga, capital alto
                + estab("66666666", uf="PR", municipio="7535")  # outro estado: fora da base
            )
        },
        "Empresas0.zip": {
            "0.EMPRECSV.csv": empresa("11111111", "MERCADO UM LTDA")
            + empresa("22222222", "BAIXADA LTDA")
            + empresa("33333333", "OUTRA LTDA")
            + empresa("44444444", "FULANO MEI", "1.000,00")
            + empresa("55555555", "ANTIGA LTDA", "500.000,00", "03")
            + empresa("66666666", "PARANA LTDA")
        },
        "Simples.zip": {"F.K03200$W.SIMPLES.CSV.D61011": simples("11111111") + simples("44444444", "N", "S")},
        "Socios0.zip": {"0.SOCIOCSV.csv": '"11111111";"2";"FULANO";"***123456**";"49";"20200101";"";"";"";"00";"5"\n'},
    }


def _sem_dotenv(*_a: object, **_k: object) -> bool:
    return False  # não ler o .env real do projeto


@pytest.fixture
def ambiente(
    source_server: tuple[str, Event, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Base de SC carregada em tmp/parquet/2024-01 (como o rodar_mensal grava) e estado em tmp."""
    base_url, _fail, _downloads = source_server
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("config.load_dotenv", _sem_dotenv)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("PROSPECCAO_ESTADO", str(tmp_path / "estado" / "prospeccao.sqlite"))
    for nome in ("GR_API_URL", "GR_CLIENT_ID", "GR_CLIENT_SECRET"):
        monkeypatch.delenv(nome, raising=False)
    cfg = Config(
        database_url="",
        base_url=base_url,
        share_token="test",
        temp_dir=str(tmp_path / "temp"),
        output_format="parquet",
        parquet_output_dir=str(tmp_path / "parquet" / MES),
        parquet_typed_output=True,
        retry_attempts=1,
        retry_delay=0,
        connect_timeout=2,
        read_timeout=2,
        stall_timeout=2,
    )
    main(cfg, args=argparse.Namespace(list=False, month=MES, force=False), filtro=FiltroProspeccao.base("SC"))
    app = tela_local.App(tmp_path / "parquet", tmp_path / "temp", Event())
    app.disco_insuficiente = lambda: None
    return {"tmp": tmp_path, "app": app, "pasta": tmp_path / "parquet" / MES}


def _contar(pasta: Path, enviados: list[str] | None = None, **filtros: Any) -> dict[str, int]:
    with Consulta(pasta, enviados or []) as c:
        return c.contar(Filtros.de_json(filtros))


# ── Base e consultas ──────────────────────────────────────────────────────────


def test_base_rapida_guarda_a_uf_inteira(ambiente: dict[str, Any]) -> None:
    base = info_base(ambiente["tmp"] / "parquet")
    assert base is not None
    assert (base.mes, base.uf, base.estabelecimentos) == (MES, "SC", 6)  # o de PR ficou de fora
    assert base.tamanho_bytes > 0 and base.meses_guardados == [MES]
    with Consulta(base.pasta) as c:
        assert [m["nome"] for m in c.municipios()] == ["BLUMENAU", "JOINVILLE"]
        assert {x["codigo"] for x in c.cnaes()} == {"4711302", "9999999"}


def test_filtros_da_tela(ambiente: dict[str, Any]) -> None:
    pasta = ambiente["pasta"]
    padrao = _contar(pasta, situacoes=["02"])
    assert padrao == {"encontradas": 5, "ja_enviadas": 0, "novas": 5, "novas_mei": 1, "novas_sem_contato": 1}
    assert _contar(pasta)["encontradas"] == 6  # nenhuma situação marcada = todas (inclui a baixada)
    assert _contar(pasta, situacoes=["02"], cnaes=["4711-3/02"], municipios=["8179"])["encontradas"] == 3
    assert _contar(pasta, situacoes=["02"], abertura_de="2026-01-01")["encontradas"] == 4  # sem a antiga
    assert _contar(pasta, situacoes=["02"], abertura_ate="2021-12-31")["encontradas"] == 1
    assert _contar(pasta, situacoes=["02"], matriz_filial="filial")["encontradas"] == 1
    assert _contar(pasta, situacoes=["02"], capital_ligado=True, capital_min="100000")["encontradas"] == 1
    assert _contar(pasta, situacoes=["02"], capital_ligado=True, capital_max="5000")["encontradas"] == 1
    # Capital desligado ignora os valores
    assert _contar(pasta, situacoes=["02"], capital_ligado=False, capital_min="100000")["encontradas"] == 5
    sem_mei = _contar(pasta, situacoes=["02"], excluir_mei=True)
    assert sem_mei["encontradas"] == 4 and sem_mei["novas_mei"] == 0

    enviada = _contar(pasta, ["11111111000100"], situacoes=["02"])
    assert (enviada["ja_enviadas"], enviada["novas"]) == (1, 4)
    with Consulta(pasta, ["11111111000100"]) as c:
        linhas = c.linhas(Filtros.de_json({"situacoes": ["02"]}), mes_base=MES)
        novas = c.linhas(Filtros.de_json({"situacoes": ["02"]}), mes_base=MES, so_novas=True)
    assert [linha["ja_enviada"] for linha in linhas] == [False, False, False, False, True]  # novas primeiro
    assert "11111111000100" not in {linha["cnpj"] for linha in novas} and len(novas) == 4
    previa = linha_previa(next(linha for linha in linhas if linha["cnpj"] == "44444444000100"))
    assert previa["cnpj"] == "44.444.444/0001-00" and previa["mei"] is True and previa["telefone"] is None
    assert previa["atividade"] == "4711-3/02 Supermercados" and previa["capital"] == 1000.0


@pytest.mark.parametrize(
    ("filtros", "trecho"),
    [
        ({"capital_ligado": True}, "capital social mínimo"),
        ({"capital_ligado": True, "capital_min": "10", "capital_max": "5"}, "maior que o máximo"),
        ({"capital_ligado": True, "capital_min": "-1"}, "negativo"),
        ({"abertura_de": "2026-02-01", "abertura_ate": "2026-01-01"}, "depois"),
        ({"abertura_de": "01/02/2026"}, "Data de abertura"),
        ({"cnaes": ["47"]}, "CNAE"),
        ({"municipios": ["Joinville"]}, "Cidade"),
        ({"matriz_filial": "outra"}, "matriz"),
    ],
)
def test_filtros_invalidos(filtros: dict[str, Any], trecho: str) -> None:
    with pytest.raises(FiltrosInvalidosError, match=trecho):
        Filtros.de_json(filtros)


def test_resumo_em_portugues() -> None:
    f = Filtros.de_json(
        {
            "cnaes": ["4711302"],
            "municipios": ["8179"],
            "abertura_de": "2026-01-01",
            "situacoes": ["02"],
            "matriz_filial": "matriz",
            "capital_ligado": True,
            "capital_min": "50000",
            "excluir_mei": True,
        }
    )
    resumo = f.resumo({"4711302": "Supermercados"}, {"8179": "JOINVILLE"})
    assert resumo == (
        "Atividades: 4711-3/02 Supermercados · Cidades: JOINVILLE · Abertura de 01/01/2026 até hoje · "
        "Situação: Ativa · Só matriz · Capital a partir de R$ 50.000 · Sem MEI"
    )
    assert Filtros.de_json(f.como_json()) == f  # "Repetir" do histórico volta aos mesmos filtros


def test_excel(ambiente: dict[str, Any], tmp_path: Path) -> None:
    with Consulta(ambiente["pasta"], ["11111111000100"]) as c:
        linhas = c.linhas(Filtros.de_json({"situacoes": ["02"]}), mes_base=MES)
    caminho = tmp_path / "lista.xlsx"
    assert gerar_excel(linhas, caminho) == 5
    with zipfile.ZipFile(caminho) as xlsx:
        conteudo = "".join(xlsx.read(n).decode("utf-8") for n in xlsx.namelist() if n.endswith(".xml"))
    assert "11.111.111/0001-00" in conteudo and "Já enviada" in conteudo and "Razão social" in conteudo


def test_limpar_meses_antigos(tmp_path: Path) -> None:
    for mes, completa in (("2026-08", True), ("2026-09", True), ("2026-10", True), ("2026-11", False)):
        (tmp_path / mes).mkdir()
        if completa:
            (tmp_path / mes / "manifest.json").write_text("{}")
    assert rodar_mensal.limpar_meses_antigos(tmp_path) == ["2026-08"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-09", "2026-10", "2026-11"]  # carga em andamento fica


# ── Tela: ver resultado, enviar, atualizar base ───────────────────────────────


def _esperar(condicao: Any, segundos: float = 20) -> None:
    fim = time.monotonic() + segundos
    while not condicao():
        assert time.monotonic() < fim, "tempo esgotado"
        time.sleep(0.05)


def test_consultar_registra_historico_sem_repetir(ambiente: dict[str, Any]) -> None:
    app: tela_local.App = ambiente["app"]
    filtros = {"situacoes": ["02"], "municipios": ["8179"]}
    r = app.consultar({"filtros": filtros})
    assert r["mes_base"] == MES and r["mes_texto"] == "janeiro de 2024"
    assert r["contagens"]["encontradas"] == 4 and len(r["previa"]) == 4
    assert r["resumo"].startswith("Todas as atividades · Cidades: JOINVILLE")
    assert app.consultar({"filtros": filtros})["varredura_id"] == r["varredura_id"]  # mesma busca: atualiza
    outra = app.consultar({"filtros": {"situacoes": ["02"]}})
    assert outra["varredura_id"] != r["varredura_id"]
    historico = app.historico()
    assert [v["id"] for v in historico] == [outra["varredura_id"], r["varredura_id"]]
    assert historico[1]["filtros"]["municipios"] == ["8179"]
    opcoes = app.opcoes()
    assert opcoes["uf"] == "SC" and opcoes["situacoes"][0] == {"codigo": "02", "nome": "Ativa"}


def test_sem_base_avisa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROSPECCAO_ESTADO", str(tmp_path / "estado.sqlite"))
    app = tela_local.App(tmp_path / "parquet", tmp_path / "temp", Event())
    assert app.estado()["base"] is None
    with pytest.raises(tela_local.ErroTela, match="Atualizar base agora"):
        app.consultar({"filtros": {}})


def test_enviar_pela_tela(
    ambiente: dict[str, Any],
    fake_gr: tuple[str, Any],
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    api, gr = fake_gr
    app: tela_local.App = ambiente["app"]
    filtros = {"situacoes": ["02"], "municipios": ["8179"]}
    r = app.consultar({"filtros": filtros})
    pedido = {"filtros": r["filtros"], "quantidade": 4, "mes_base": MES, "varredura_id": r["varredura_id"]}

    # Sem GR_* no .env: não envia
    with pytest.raises(tela_local.ErroTela, match="GR_API_URL"):
        app.enviar(pedido)
    for nome, valor in (("GR_API_URL", api), ("GR_CLIENT_ID", "id"), ("GR_CLIENT_SECRET", "segredo"), ("GR_LOTE", "2")):
        monkeypatch.setenv(nome, valor)

    # Trava de quantidade (aqui 3, para o teste): acima disso só com o número digitado
    monkeypatch.setattr(tela_local, "LIMITE_CONFIRMACAO", 3)
    with pytest.raises(tela_local.ErroTela, match="digite o número exato \\(4\\)"):
        app.enviar(pedido)
    with pytest.raises(tela_local.ErroTela, match="digite"):
        app.enviar({**pedido, "confirmacao": "5"})
    # O resultado mudou desde a busca / a base mudou
    with pytest.raises(tela_local.ErroTela, match="agora são 4 novas"):
        app.enviar({**pedido, "quantidade": 2})
    with pytest.raises(tela_local.ErroTela, match="base foi atualizada"):
        app.enviar({**pedido, "mes_base": "2023-12", "confirmacao": "4"})
    # Serviço trabalhando (trava do rodar_mensal): não envia
    with rodar_mensal.trava_prospeccao(rodar_mensal.caminho_trava()) as obtida:
        assert obtida
        with pytest.raises(tela_local.ErroTela, match="trabalhando na base"):
            app.enviar({**pedido, "confirmacao": "4"})

    assert app.enviar({**pedido, "confirmacao": "4"}) == {"iniciado": True, "total": 4}
    _esperar(lambda: app.envio is not None and app.envio["fim"])
    envio = app.envio
    assert envio is not None and envio["ok"] is True and envio["enviadas"] == 4
    # O GestaoRevenda falso bloqueia a filial 11111111000211
    assert envio["resultado"] == {"enviadas": 4, "importadas": 3, "ja_existiam": 0, "bloqueadas": 1, "recusadas": 0}
    leads: list[str] = list(gr.leads)
    assert sorted(leads) == ["11111111000100", "11111111000211", "33333333000100", "55555555000100"]

    with EstadoProspeccao() as estado:
        assert estado.ultima_execucao() is None  # envio da tela não é execução do envio automático
        assert estado.ultima_concluida(MES, aceitar_sem_envio=True) is None and not estado.ja_teve_envio_completo()
        tela = estado.ultima_execucao(incluir_tela=True)
        assert tela is not None and (tela["modo"], tela["status"]) == ("tela", "OK")
        assert tela["contagens"]["filtros_tela"].startswith("Todas as atividades")
        assert estado.varreduras()[0]["envio"]["importadas"] == 3
    # Histórico do GestaoRevenda com origem "tela"
    recebidos: list[tuple[str, dict[str, Any]]] = gr.recebidos
    [execucao] = [p for rota, p in recebidos if rota == "execucoes"]
    assert execucao["origem"] == "tela" and execucao["enviadas"] == 4 and execucao["status"] == "OK"
    assert execucao["detalhes"]["filtros_tela"].startswith("Todas as atividades")

    # A trava foi solta, e as enviadas viram "já enviadas" (a busca seguinte não tem nada novo)
    with rodar_mensal.trava_prospeccao(rodar_mensal.caminho_trava()) as obtida:
        assert obtida
    depois = app.consultar({"filtros": filtros})["contagens"]
    assert (depois["ja_enviadas"], depois["novas"]) == (4, 0)
    with pytest.raises(tela_local.ErroTela, match="Não há empresas novas"):
        app.enviar({**pedido, "quantidade": 0})


def test_envio_que_para_no_meio(
    ambiente: dict[str, Any],
    fake_gr: tuple[str, Any],
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    api, gr = fake_gr
    for nome, valor in (("GR_API_URL", api), ("GR_CLIENT_ID", "id"), ("GR_CLIENT_SECRET", "segredo")):
        monkeypatch.setenv(nome, valor)
    monkeypatch.setenv("GR_LOTE", "2")
    monkeypatch.setenv("GR_TENTATIVAS", "1")
    app: tela_local.App = ambiente["app"]
    r = app.consultar({"filtros": {"situacoes": ["02"], "municipios": ["8179"]}})

    from enviar_gestaorevenda import ClienteGestaoRevenda, EnvioError

    original: Any = cast(Any, ClienteGestaoRevenda).importar
    lotes = {"n": 0}

    def importar_com_falha(self: ClienteGestaoRevenda, rows: list[dict[str, Any]], data_base: str) -> dict[str, Any]:
        lotes["n"] += 1
        if lotes["n"] == 2:  # o 2º lote é recusado
            raise EnvioError("Lote recusado (400): campo inválido")
        return original(self, rows, data_base)

    monkeypatch.setattr(ClienteGestaoRevenda, "importar", importar_com_falha)
    app.enviar({"filtros": r["filtros"], "quantidade": 4, "mes_base": MES, "varredura_id": r["varredura_id"]})
    _esperar(lambda: app.envio is not None and app.envio["fim"])
    envio = app.envio
    assert envio is not None and envio["ok"] is False and "parou no meio" in envio["erro"]
    payloads: list[Any] = gr.payloads
    assert envio["resultado"]["enviadas"] == 2 and len(payloads) == 1
    with EstadoProspeccao() as estado:
        tela = estado.ultima_execucao(incluir_tela=True)
        assert tela is not None and tela["status"] == "FALHOU" and "campo inválido" in tela["contagens"]["erro"]
    # O que passou não volta: só as 2 do lote recusado continuam novas
    assert app.consultar({"filtros": r["filtros"]})["contagens"]["novas"] == 2


def test_atualizar_base(ambiente: dict[str, Any]) -> None:
    app: tela_local.App = ambiente["app"]
    chamadas: list[tuple[str, bool]] = []
    codigo = {"valor": 0}

    def rodar(mes: str, forcar: bool) -> int:
        chamadas.append((mes, forcar))
        return codigo["valor"]

    app.rodar_base = rodar
    app.mes_mais_recente = lambda: MES
    assert app.atualizar_base({}) == {"ja_atualizada": True, "mes": MES, "mes_texto": "janeiro de 2024"}
    assert chamadas == []
    assert app.atualizar_base({"forcar": True})["iniciado"] is True
    _esperar(lambda: app.atualizacao is not None and app.atualizacao["fim"])
    assert chamadas == [(MES, True)] and app.atualizacao is not None and app.atualizacao["ok"] is True
    assert "pronta" in app.atualizacao["mensagem"]

    app.mes_mais_recente = lambda: "2024-02"  # mês novo na Receita
    codigo["valor"] = rodar_mensal.CODIGO_OCUPADO
    app.atualizar_base({})
    _esperar(lambda: app.atualizacao is not None and app.atualizacao["fim"])
    assert app.atualizacao is not None and app.atualizacao["ok"] is False
    assert "já está trabalhando" in app.atualizacao["mensagem"]

    app.disco_insuficiente = lambda: "espaço insuficiente em C:"
    with pytest.raises(tela_local.ErroTela, match="Pouco espaço"):
        app.atualizar_base({})

    def fora_do_ar() -> str:
        raise ConnectionError("timeout")

    app.mes_mais_recente = fora_do_ar
    with pytest.raises(tela_local.ErroTela, match="site da Receita"):
        app.atualizar_base({})


# ── HTTP: segurança da página ─────────────────────────────────────────────────


@pytest.fixture
def servidor(ambiente: dict[str, Any]) -> Iterator[tuple[int, tela_local.App]]:
    import threading

    app: tela_local.App = ambiente["app"]
    srv = tela_local.criar_servidor(app, 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_port, app
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _pedir(
    porta: int, metodo: str, caminho: str, corpo: Any = None, **cabecalhos: str
) -> tuple[int, dict[str, str], bytes]:
    conexao = HTTPConnection("127.0.0.1", porta, timeout=20)
    padrao = {"Host": f"127.0.0.1:{porta}"}
    dados = None
    if corpo is not None:
        dados = json.dumps(corpo).encode()
        padrao.update({"Content-Type": "application/json", "X-Prospeccao": "1"})
    padrao.update({k.replace("_", "-"): v for k, v in cabecalhos.items()})
    conexao.request(metodo, caminho, body=dados, headers=padrao)
    resp = conexao.getresponse()
    corpo_resp = resp.read()
    conexao.close()
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, corpo_resp


def test_pagina_e_api(servidor: tuple[int, tela_local.App]) -> None:
    porta, _app = servidor
    status, cab, corpo = _pedir(porta, "GET", "/")
    assert status == 200 and b"Prospec" in corpo
    assert "frame-ancestors 'none'" in cab["content-security-policy"] and cab["x-frame-options"] == "DENY"
    assert _pedir(porta, "GET", "/app.js")[0] == 200 and _pedir(porta, "GET", "/app.css")[0] == 200
    status, _cab, corpo = _pedir(porta, "GET", "/api/estado")
    estado = json.loads(corpo)
    assert status == 200 and estado["base"]["mes"] == MES and estado["limite_confirmacao"] == 500
    assert estado["gestaorevenda"]["configurado"] is False

    status, _cab, corpo = _pedir(porta, "POST", "/api/consultar", {"filtros": {"situacoes": ["02"]}})
    assert status == 200 and json.loads(corpo)["contagens"]["encontradas"] == 5
    status, _cab, corpo = _pedir(porta, "POST", "/api/consultar", {"filtros": {"capital_ligado": True}})
    assert status == 400 and "capital" in json.loads(corpo)["erro"]
    status, cab, corpo = _pedir(porta, "POST", "/api/excel", {"filtros": {"situacoes": ["02"]}})
    assert status == 200 and cab["content-type"].startswith("application/vnd.openxmlformats")
    assert 'filename="prospeccao_2024-01_' in cab["content-disposition"] and corpo[:2] == b"PK"
    assert _pedir(porta, "GET", "/../estado/prospeccao.sqlite")[0] == 404


def test_pagina_recusa_outros_sites(servidor: tuple[int, tela_local.App]) -> None:
    porta, _app = servidor
    # DNS rebinding: outro nome apontando para 127.0.0.1
    assert _pedir(porta, "GET", "/api/estado", Host=f"site-malicioso.com:{porta}")[0] == 403
    # Outro site mandando POST pelo navegador: sem o cabeçalho, ou com Origin de fora
    filtros = {"filtros": {"situacoes": ["02"]}}
    assert _pedir(porta, "POST", "/api/consultar", filtros, X_Prospeccao="0")[0] == 403
    assert _pedir(porta, "POST", "/api/consultar", filtros, Origin="https://site-malicioso.com")[0] == 403
    assert _pedir(porta, "POST", "/api/consultar", filtros, Origin=f"http://localhost:{porta}")[0] == 200
    assert _pedir(porta, "POST", "/api/consultar", filtros, Content_Type="text/plain")[0] == 415


def test_servidor_so_escuta_neste_computador(servidor: tuple[int, tela_local.App]) -> None:
    import socket

    porta, _app = servidor
    ips = {str(info[4][0]) for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)} - {"127.0.0.1"}
    for ip in ips:  # pelos IPs da rede, a porta não atende
        with pytest.raises(OSError), socket.create_connection((ip, porta), timeout=2):
            pass
    with socket.create_connection(("127.0.0.1", porta), timeout=2):
        pass
