"""Filtro de prospecção, exportação e controle de novos (adaptação GestaoRevenda)."""

import csv
from datetime import date
from pathlib import Path
from threading import Event

import polars as pl
import pytest

from config import Config
from estado_prospeccao import CNPJ_INEDITO, FILIAL_NOVA, JA_ENVIADO, EstadoProspeccao, ItemResultado
from exportar_prospeccao import escrever_csv, exportar, formatar_telefone, montar_linha, where_filtro
from file_types import FILTERED_DEPENDENCY_GROUPS, group_files_by_dependency
from filtro import ContadoresFiltro, FiltroInvalidoError, FiltroProspeccao, normalizar_cnae, subtrair_meses
from main import main

HOJE = date(2026, 11, 13)


def estab(
    basico: str,
    ordem: str = "0001",
    dv: str = "00",
    *,
    matriz: str = "1",
    fantasia: str = "LOJA",
    situacao: str = "02",
    inicio: str = "20260601",
    cnae: str = "4711302",
    uf: str = "SC",
    municipio: str = "8179",
    ddd: str = "47",
    telefone: str = "34331234",
    email: str = "CONTATO@LOJA.COM.BR",
) -> str:
    campos = [
        basico, ordem, dv, matriz, fantasia, situacao, "20200101", "00", "", "", inicio, cnae,
        "4712100,4789004", "RUA", "DAS FLORES", "100", "SALA 2", "CENTRO", "89201000", uf, municipio,
        ddd, telefone, "", "", "", "", email, "", "",
    ]  # fmt: skip
    assert len(campos) == 30
    return ";".join(f'"{c}"' for c in campos) + "\n"


def empresa(basico: str, nome: str, porte: str = "01") -> str:
    return f'"{basico}";"{nome}";"2062";"49";"10.000,00";"{porte}";""\n'


def simples(basico: str, simples_: str = "S", mei: str = "N") -> str:
    return f'"{basico}";"{simples_}";"20200101";"";"{mei}";"";""\n'


# ── Unidades ──────────────────────────────────────────────────────────────────


def test_normalizar_cnae_aceita_formatos() -> None:
    assert normalizar_cnae("4711-3/02") == "4711302"
    assert normalizar_cnae("4711302") == "4711302"
    with pytest.raises(FiltroInvalidoError):
        normalizar_cnae("4711-3")


def test_subtrair_meses_ajusta_fim_de_mes() -> None:
    assert subtrair_meses(date(2026, 11, 13), 12) == date(2025, 11, 13)
    assert subtrair_meses(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert subtrair_meses(date(2026, 1, 15), 1) == date(2025, 12, 15)


def test_filtro_desligado_por_padrao(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROSPECCAO_FILTRO", raising=False)
    assert FiltroProspeccao.from_env() is None
    monkeypatch.setenv("PROSPECCAO_FILTRO", "false")
    assert FiltroProspeccao.from_env() is None


def test_filtro_padroes_e_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for nome in ("FILTRO_SITUACAO", "FILTRO_CNAES", "FILTRO_UF", "FILTRO_MUNICIPIOS", "FILTRO_INICIO_ATIVIDADE_MESES"):
        monkeypatch.delenv(nome, raising=False)
    monkeypatch.setenv("PROSPECCAO_FILTRO", "true")
    f = FiltroProspeccao.from_env()
    assert f is not None
    assert f.situacoes == {"02"}
    assert f.cnaes == {"4711302", "4712100", "4711301", "4744099", "4744001", "4530703", "4789004"}
    assert f.uf is None and f.municipios_config == [] and f.meses_inicio_atividade == 12

    monkeypatch.setenv("FILTRO_UF", "sc")
    monkeypatch.setenv("FILTRO_CNAES", "4711-3/02")
    monkeypatch.setenv("FILTRO_MUNICIPIOS", "Joinville; 8105")
    monkeypatch.setenv("FILTRO_INICIO_ATIVIDADE_MESES", "0")
    f = FiltroProspeccao.from_env()
    assert f is not None
    assert (f.uf, f.cnaes, f.data_inicio_minima) == ("SC", {"4711302"}, None)
    assert f.municipios_config == ["Joinville", "8105"]

    monkeypatch.setenv("FILTRO_UF", "Santa Catarina")
    with pytest.raises(FiltroInvalidoError):
        FiltroProspeccao.from_env()


def test_resolver_municipios_por_nome_codigo_e_ambiguidade() -> None:
    tabela = {"8179": "JOINVILLE", "9001": "BOM JESUS", "9002": "BOM JESUS", "8105": "SÃO JOSÉ"}
    f = FiltroProspeccao(municipios_config=["joinville", "Sao Jose", "123"], hoje=HOJE)
    f.resolver_municipios(tabela)
    assert f.municipios == {"8179", "8105", "0123"}

    with pytest.raises(FiltroInvalidoError, match="mais de um estado"):
        FiltroProspeccao(municipios_config=["Bom Jesus"]).resolver_municipios(tabela)
    com_uf = FiltroProspeccao(municipios_config=["Bom Jesus"], uf="PI")
    com_uf.resolver_municipios(tabela)
    assert com_uf.municipios == {"9001", "9002"}  # a UF desambigua na hora de filtrar

    with pytest.raises(FiltroInvalidoError, match="não encontrado"):
        FiltroProspeccao(municipios_config=["Atlantida"]).resolver_municipios(tabela)

    so_codigos = FiltroProspeccao(municipios_config=["8179"])
    assert not so_codigos.precisa_tabela_municipios
    so_codigos.resolver_municipios(None)
    assert so_codigos.municipios == {"8179"}


@pytest.mark.parametrize("tipada", [False, True])
def test_aplicar_estabelecimentos(tipada: bool) -> None:
    df = pl.DataFrame(
        {
            "cnpj_basico": ["1", "2", "3", "4", "5", "6"],
            "situacao_cadastral": ["02", "08", "02", "02", "02", "02"],
            "cnae_fiscal_principal": ["4711302", "4711302", "9999999", "4711302", "4711302", "4711302"],
            "uf": ["SC", "SC", "SC", "PR", "SC", "SC"],
            "municipio": ["8179", "8179", "8179", "8179", "8179", "8105"],
            "data_inicio_atividade": ["20260101", "20260101", "20260101", "20260101", "20240101", "20260101"],
        }
    )
    if tipada:
        df = df.with_columns(pl.col("data_inicio_atividade").str.strptime(pl.Date, "%Y%m%d"))
    f = FiltroProspeccao(uf="SC", municipios_config=["8179"], hoje=HOJE)
    f.resolver_municipios()
    assert f.aplicar(df, "ESTABELE")["cnpj_basico"].to_list() == ["1"]

    sem_data = FiltroProspeccao(uf="SC", meses_inicio_atividade=0, hoje=HOJE)
    assert sem_data.aplicar(df, "ESTABELE")["cnpj_basico"].to_list() == ["1", "5", "6"]


def test_aplicar_por_cnpj_basico_exige_conjunto() -> None:
    df = pl.DataFrame({"cnpj_basico": ["1", "2"]})
    f = FiltroProspeccao()
    with pytest.raises(RuntimeError):
        f.aplicar(df, "EMPRECSV")
    f.cnpjs_basicos = {"2"}
    assert f.aplicar(df, "SIMPLESCSV")["cnpj_basico"].to_list() == ["2"]
    assert f.aplicar(df, "CNAECSV").height == 2  # referência passa inteira


def test_grupos_com_filtro_tiram_socios_e_invertem_ordem() -> None:
    arquivos = ["Socios0.zip", "Empresas0.zip", "Simples.zip", "Estabelecimentos0.zip", "Cnaes.zip"]
    assert group_files_by_dependency(arquivos, FILTERED_DEPENDENCY_GROUPS) == [
        ["Cnaes.zip"],
        ["Estabelecimentos0.zip"],
        ["Empresas0.zip", "Simples.zip"],
    ]


def test_formatar_telefone() -> None:
    assert formatar_telefone("47", "34331234") == {"tipo": "Fixo", "numero": "(47) 34331234"}
    assert formatar_telefone(" 047", "99887766") == {"tipo": "Celular", "numero": "(47) 99887766"}
    assert formatar_telefone("47", "") is None
    assert formatar_telefone("47", "00000000") is None


def test_where_filtro_espelha_expressao() -> None:
    f = FiltroProspeccao(cnaes={"4711302"}, uf="SC", municipios_config=["8179"], hoje=HOJE)
    f.resolver_municipios()
    where, params = where_filtro(f, "?")
    assert where.count("?") == len(params)
    assert params == ["02", "4711302", "SC", "8179", date(2025, 11, 13)]
    assert where_filtro(None, "?") == ("", [])


def test_estado_classifica_e_ignora_erros(tmp_path: Path) -> None:
    with EstadoProspeccao(tmp_path / "estado.sqlite") as estado:
        execucao = estado.iniciar_execucao("2026-10", "novos")
        estado.registrar_resultados(
            execucao,
            [
                ItemResultado("11111111000100", "11111111", CNPJ_INEDITO, "IMPORTADO"),
                ItemResultado("22222222000100", "22222222", CNPJ_INEDITO, "ERRO"),
                ItemResultado("33333333000100", "33333333", CNPJ_INEDITO, "DUPLICADO"),
            ],
        )
        estado.finalizar_execucao(execucao, "OK", {"enviadas": 3})
        linhas = estado.classificar(
            [
                {"cnpj": "11111111000100", "cnpj_basico": "11111111"},
                {"cnpj": "11111111000200", "cnpj_basico": "11111111"},
                {"cnpj": "22222222000100", "cnpj_basico": "22222222"},
                {"cnpj": "33333333000100", "cnpj_basico": "33333333"},
            ]
        )
        assert [linha["novidade"] for linha in linhas] == [JA_ENVIADO, FILIAL_NOVA, CNPJ_INEDITO, JA_ENVIADO]
        ultima = estado.ultima_execucao()
        assert ultima is not None and ultima["status"] == "OK" and ultima["contagens"] == {"enviadas": 3}
        with pytest.raises(ValueError):
            estado.iniciar_execucao("2026-10", "alguns")


# ── Ponta a ponta (Parquet, servidor WebDAV local) ───────────────────────────


@pytest.fixture
def source_files() -> dict[str, dict[str, str]]:
    return {
        "Cnaes.zip": {"CNAECSV.csv": '"4711302";"Supermercados"\n"4712100";"Minimercados"\n'},
        "Municipios.zip": {"MUNICCSV.csv": '"8179";"JOINVILLE"\n"8105";"BLUMENAU"\n'},
        "Naturezas.zip": {"NATJUCSV.csv": '"2062";"Sociedade Empresária Limitada"\n'},
        "Estabelecimentos0.zip": {
            "0.ESTABELE.csv": (
                estab("11111111")  # matriz OK
                + estab("11111111", "0002", "11", matriz="2", telefone="99887766")  # filial OK
                + estab("22222222", situacao="08")  # baixada
                + estab("33333333", cnae="9999999")  # CNAE fora
                + estab("44444444", municipio="8105")  # outra cidade
                + estab("55555555", inicio="20200101")  # antiga
            )
        },
        "Empresas0.zip": {
            "0.EMPRECSV.csv": empresa("11111111", "MERCADO UM LTDA")
            + empresa("22222222", "BAIXADA LTDA")
            + empresa("55555555", "ANTIGA LTDA", "03")
        },
        "Simples.zip": {"F.K03200$W.SIMPLES.CSV.D61011": simples("11111111") + simples("55555555", "N", "S")},
        "Socios0.zip": {"0.SOCIOCSV.csv": '"11111111";"2";"FULANO";"***123456**";"49";"20200101";"";"";"";"00";"5"\n'},
    }


def _config(base_url: str, tmp_path: Path, typed: bool) -> Config:
    return Config(
        database_url="",
        base_url=base_url,
        share_token="test",
        temp_dir=str(tmp_path / "downloads"),
        output_format="parquet",
        parquet_output_dir=str(tmp_path / "parquet"),
        parquet_typed_output=typed,
        retry_attempts=1,
        retry_delay=0,
        connect_timeout=2,
        read_timeout=2,
        stall_timeout=2,
    )


@pytest.mark.parametrize("typed", [False, True])
def test_pipeline_filtrado_exporta_e_marca_novos(
    source_server: tuple[str, Event, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typed: bool
) -> None:
    base_url, _fail, downloads = source_server
    monkeypatch.setattr("sys.argv", ["cnpj-pipeline", "--month", "2024-01"])
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    cfg = _config(base_url, tmp_path, typed)
    filtro = FiltroProspeccao(uf="SC", municipios_config=["Joinville"], hoje=HOJE)
    contadores = ContadoresFiltro()

    assert main(cfg, filtro=filtro, contadores=contadores) == "2024-01"

    assert "/2024-01/Socios0.zip" not in downloads  # sócios nem são baixados
    saida = tmp_path / "parquet"
    assert not (saida / "socios.parquet").exists()
    estabs = pl.read_parquet(saida / "estabelecimentos.parquet")
    assert sorted(estabs["cnpj_ordem"].to_list()) == ["0001", "0002"]
    assert set(estabs["cnpj_basico"]) == {"11111111"}
    assert pl.read_parquet(saida / "empresas.parquet")["cnpj_basico"].to_list() == ["11111111"]
    assert pl.read_parquet(saida / "dados_simples.parquet")["cnpj_basico"].to_list() == ["11111111"]
    assert contadores.lidas["estabelecimentos"] == 6 and contadores.mantidas["estabelecimentos"] == 2
    assert contadores.lidas["empresas"] == 3 and contadores.mantidas["empresas"] == 1

    # Exportação (DuckDB) + controle de novos
    export_filtro = FiltroProspeccao(uf="SC", municipios_config=["Joinville"], hoje=HOJE)
    with EstadoProspeccao(tmp_path / "estado.sqlite") as estado:
        linhas, selecionadas, mes = exportar(cfg, export_filtro, estado)
        assert mes == "2024-01"
        assert [linha["cnpj"] for linha in selecionadas] == ["11111111000100", "11111111000211"]
        matriz, filial = selecionadas
        assert matriz["novidade"] == CNPJ_INEDITO and filial["novidade"] == CNPJ_INEDITO
        assert matriz == {
            "cnpj": "11111111000100",
            "cnpj_basico": "11111111",
            "razao_social": "MERCADO UM LTDA",
            "nome_fantasia": "LOJA",
            "matriz_filial": "Matriz",
            "cnae_codigo": "4711302",
            "cnae_descricao": "Supermercados",
            "cnaes_secundarios": [{"id": "4712100", "text": "Minimercados"}, {"id": "4789004", "text": ""}],
            "data_inicio_atividade": "2026-06-01",
            "situacao": "ATIVA",
            "data_situacao": "2020-01-01",
            "logradouro": "RUA DAS FLORES",
            "numero": "100",
            "complemento": "SALA 2",
            "bairro": "CENTRO",
            "cep": "89201-000",
            "municipio_nome": "JOINVILLE",
            "municipio_codigo": "8179",
            "uf": "SC",
            "endereco_completo": "RUA DAS FLORES, 100, SALA 2, CENTRO, JOINVILLE, SC, 89201-000",
            "telefones": [{"tipo": "Fixo", "numero": "(47) 34331234"}],
            "email": "contato@loja.com.br",
            "porte": "ME",
            "natureza_juridica": "Sociedade Empresária Limitada",
            "capital_social": 10000.0,
            "simples_nacional": True,
            "mei": False,
            "mes_base": "2024-01",
            "novidade": CNPJ_INEDITO,
        }
        assert filial["matriz_filial"] == "Filial"
        assert filial["telefones"] == [{"tipo": "Celular", "numero": "(47) 99887766"}]

        caminho = escrever_csv(selecionadas, tmp_path / "saida.csv")
        with open(caminho, encoding="utf-8-sig", newline="") as f:
            registros = list(csv.DictReader(f, delimiter=";"))
        assert registros[0]["cnpj"] == "11111111000100" and registros[0]["simples_nacional"] == "S"
        assert registros[0]["cnaes_secundarios"] == "4712100,4789004"

        # Só a matriz foi aceita: na próxima, a matriz é JA_ENVIADO (fica de fora) e a filial,
        # que deu erro, continua sendo da mesma empresa já conhecida → FILIAL_NOVA.
        execucao = estado.iniciar_execucao(mes, "novos")
        estado.registrar_resultados(
            execucao,
            [
                ItemResultado(matriz["cnpj"], matriz["cnpj_basico"], matriz["novidade"], "IMPORTADO"),
                ItemResultado(filial["cnpj"], filial["cnpj_basico"], filial["novidade"], "ERRO"),
            ],
        )
        _linhas, selecionadas, _mes = exportar(cfg, export_filtro, estado)
        assert [(linha["cnpj"], linha["novidade"]) for linha in selecionadas] == [("11111111000211", FILIAL_NOVA)]
        linhas, selecionadas, _mes = exportar(cfg, export_filtro, estado, tudo=True)
        assert [linha["novidade"] for linha in selecionadas] == [JA_ENVIADO, FILIAL_NOVA]


def test_pipeline_filtrado_que_nao_sobra_nada(
    source_server: tuple[str, Event, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_url, _fail, _downloads = source_server
    monkeypatch.setattr("sys.argv", ["cnpj-pipeline", "--month", "2024-01"])
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    cfg = _config(base_url, tmp_path, typed=True)
    filtro = FiltroProspeccao(uf="AM", hoje=HOJE)

    assert main(cfg, filtro=filtro) == "2024-01"
    assert not (tmp_path / "parquet" / "estabelecimentos.parquet").exists()
    with EstadoProspeccao(tmp_path / "estado.sqlite") as estado:
        assert exportar(cfg, FiltroProspeccao(uf="AM", hoje=HOJE), estado)[1] == []


def test_montar_linha_sem_empresa_usa_fantasia() -> None:
    bruta = dict.fromkeys(
        [
            "tipo_logradouro", "logradouro", "numero", "complemento", "bairro", "cep", "municipio_nome", "uf",
            "ddd_1", "telefone_1", "ddd_2", "telefone_2", "cnae_fiscal_secundaria", "correio_eletronico",
            "capital_social", "razao_social", "cnae_descricao", "data_inicio_atividade", "situacao_cadastral",
            "data_situacao_cadastral", "municipio", "porte", "natureza_juridica", "opcao_pelo_simples",
            "opcao_pelo_mei", "identificador_matriz_filial", "cnae_fiscal_principal",
        ]
    )  # fmt: skip
    bruta.update(cnpj_basico="1", cnpj_ordem="0001", cnpj_dv="00", nome_fantasia="  BAR  DO ZE ")
    linha = montar_linha(bruta, {}, None)
    assert linha["razao_social"] == "BAR DO ZE"
    assert linha["simples_nacional"] is None and linha["matriz_filial"] is None and linha["endereco_completo"] is None
