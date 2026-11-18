"""Leitor dos XML de requisição do webservice do e-cidade.

Arquivos escritos em `tmp_path` (sem Docker, sem o e-cidade): cada teste monta só o XML que
exercita uma regra. As formas reais (BOM, espaço antes da declaração, `tabela="cgm a   "`, a forma
antiga e a nova do processamento) foram medidas na instalação em 2026-10-02.
"""

from pathlib import Path

from php_map.ingest.ws_xml import read_ws_xml
from php_map.profiles.base import WsXmlSource

CONSULTA = WsXmlSource("soap_consulta", "ws/consulta", "consulta")
PROCESSAMENTO = WsXmlSource("soap_processamento", "ws/processamento", "processamento")
SOURCES = (CONSULTA, PROCESSAMENTO)
IGNORE = ("modelo.",)


def _write(root: Path, rel: str, content: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


def _read(root: Path, encoding: str = "latin-1"):  # type: ignore[no-untyped-def]
    return read_ws_xml(root, SOURCES, IGNORE, encoding)


def test_consulta_reads_the_table_and_the_filter(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "ws/consulta/getCgm.xml",
        "<?xml version='1.0' ?><consulta><campos><campo campo='z01_nome' label='nome'/></campos>"
        "<from tabela='cgm' /><filtros>"
        "<filtro label='cpf' condicao=\"z01_cgccpf = '$cpf'\" obrigatorio='true'/>"
        "<filtro label='nome' condicao='z01_nome ilike $nome' obrigatorio='false'/>"
        "</filtros></consulta>",
    )

    records, errors, stats = _read(tmp_path)

    [record] = records
    assert (record.name, record.kind, record.path) == (
        "getCgm",
        "soap_consulta",
        "ws/consulta/getCgm.xml",
    )
    assert record.tables == ("cgm",) and record.join_tables == 0
    assert [(p.name, p.required, p.type) for p in record.params] == [
        ("cpf", True, None),
        ("nome", False, None),
    ]
    assert not errors and (stats.found, stats.endpoints) == (1, 1)


def test_consulta_takes_the_first_token_of_from_and_join_tables(tmp_path: Path) -> None:
    """`tabela="cgm a   "` é a tabela `cgm` com alias; o `<juncoes>` traz tabelas de verdade."""
    _write(
        tmp_path,
        "ws/consulta/tributario/getDadosEmpresa.xml",
        "<consulta><from tabela='issbase' /><juncoes>"
        "<join tipo='inner' tabela='cgm a   ' condicao='a.z01_numcgm = q02_numcgm'/>"
        "<join tipo='left ' tabela='escrito ' condicao='q02_inscr = q10_inscr'/>"
        "<join tipo='left ' tabela='cgm b   ' condicao='q10_numcgm = b.z01_numcgm'/>"
        "</juncoes><filtros><filtro label='' condicao='q02_dtbaix is null' obrigatorio='true'/>"
        "</filtros></consulta>",
    )

    [record], _errors, stats = _read(tmp_path)

    assert record.tables == ("issbase", "cgm", "escrito")  # `cgm` repetida só entra uma vez
    assert record.join_tables == 2 and stats.join_tables == 2
    assert record.params == ()  # filtro sem label é condição fixa, não parâmetro


def test_processamento_new_form_has_params_class_and_exec_methods(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "ws/processamento/tributario/geraRecibo.xml",
        "<processamento><parametros>"
        "<parametro nome='cnpj_cpf' obrigatorio='true' tipo='integer' />"
        "<parametro nome='email' obrigatorio='false' tipo='string' />"
        "</parametros><configuracoes>"
        "<classe referencia='oRecibo' nome='ReciboAvulso' parametros=''"
        " caminho='model/R.model.php'/>"
        "<exec><metodo referencia='oRecibo' nome='setCnpjCpf' parametros='$cnpj_cpf'/>"
        "<metodo referencia='outra' nome='naoMeu' parametros=''/>"
        "<metodo referencia='oRecibo' nome='gerarRecibo' parametros=''/></exec>"
        "</configuracoes></processamento>",
    )

    [record], _errors, _stats = _read(tmp_path)

    assert [(p.name, p.required, p.type) for p in record.params] == [
        ("cnpj_cpf", True, "integer"),
        ("email", False, "string"),
    ]
    [ws_class] = record.classes
    assert (ws_class.name, ws_class.path) == ("ReciboAvulso", "model/R.model.php")
    assert ws_class.methods == ("setCnpjCpf", "gerarRecibo")  # o método de outra referência fica


def test_processamento_old_form_reads_the_nested_methods(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "ws/processamento/liquida.xml",
        "<processamento><configuracoes>"
        "<classe nome='empenho' parametros='$x' caminho='classes/empenho.php'><metodos>"
        "<metodo nome='setEncode' parametros='true'/><metodo nome='liquidar'/>"
        "<metodo nome='setEncode' parametros='false'/></metodos></classe>"
        "</configuracoes></processamento>",
    )

    [record], _errors, _stats = _read(tmp_path)

    assert record.classes[0].methods == ("setEncode", "liquidar")  # sem repetir


def test_latin1_xml_without_declaration_is_decoded_with_the_profile_encoding(
    tmp_path: Path,
) -> None:
    raw = (
        "<consulta><from tabela='cgm'/><filtros>"
        "<filtro label='descrição' condicao='x'/></filtros></consulta>"
    )
    _write(tmp_path, "ws/consulta/getAcento.xml", "<?xml version='1.0' ?>" + raw)
    _write(
        tmp_path,
        "ws/consulta/getLatin.xml",
        ("<?xml version='1.0' ?>" + raw).encode("latin-1"),
    )

    records, errors, _stats = _read(tmp_path, "latin-1")

    assert not errors
    assert {r.name: r.params[0].name for r in records} == {
        "getAcento": "descrição",  # UTF-8 válido: UTF-8 estrito decide
        "getLatin": "descrição",  # latin-1 (inválido como UTF-8): o encoding do perfil
    }


def test_declared_encoding_wins(tmp_path: Path) -> None:
    body = (
        "<consulta><from tabela='cgm'/><filtros>"
        "<filtro label='ação' condicao='x'/></filtros></consulta>"
    )
    _write(
        tmp_path,
        "ws/consulta/getDeclarado.xml",
        ('<?xml version="1.0" encoding="ISO-8859-1"?>' + body).encode("latin-1"),
    )

    [record], errors, _stats = _read(tmp_path, "utf-8")  # o perfil diria UTF-8; o arquivo manda

    assert not errors and record.params[0].name == "ação"


def test_bom_and_blank_lines_before_the_declaration_are_tolerated(tmp_path: Path) -> None:
    """Existem na instalação real (`getCalculoAliquotaSimples`, `getInicioAtividade`)."""
    body = "<?xml version='1.0' ?><consulta><from tabela='cgm'/></consulta>"
    _write(tmp_path, "ws/consulta/getComBom.xml", b"\xef\xbb\xbf" + body.encode())
    _write(tmp_path, "ws/consulta/getLinhaEmBranco.xml", ("\n\n  " + body).encode())

    records, errors, stats = _read(tmp_path)

    assert not errors and sorted(r.name for r in records) == ["getComBom", "getLinhaEmBranco"]
    assert stats.parse_errors == 0


def test_malformed_xml_is_an_error_and_does_not_stop_the_scan(tmp_path: Path) -> None:
    _write(tmp_path, "ws/consulta/getQuebrado.xml", "<consulta><from tabela='cgm'>")
    _write(tmp_path, "ws/consulta/getBom.xml", "<consulta><from tabela='cgm'/></consulta>")

    records, errors, stats = _read(tmp_path)

    assert [r.name for r in records] == ["getBom"]
    assert [e.path for e in errors] == ["ws/consulta/getQuebrado.xml"]
    assert stats.parse_errors == 1 and stats.found == 2


def test_model_files_and_other_roots_are_ignored_and_counted(tmp_path: Path) -> None:
    _write(tmp_path, "ws/processamento/modelo.1.2.xml", "<processamento/>")
    _write(tmp_path, "ws/consulta/getOutro.xml", "<processamento/>")  # raiz errada para a pasta
    _write(tmp_path, "ws/consulta/consulta.xml.template", "<consulta/>")  # nem é .xml
    _write(tmp_path, "ws/consulta/getOk.xml", "<consulta><from tabela='cgm'/></consulta>")

    records, errors, stats = _read(tmp_path)

    assert [r.name for r in records] == ["getOk"] and not errors
    assert (stats.found, stats.ignored_by_name, stats.ignored_not_request) == (3, 1, 1)


def test_duplicate_names_are_counted(tmp_path: Path) -> None:
    _write(tmp_path, "ws/consulta/a/getX.xml", "<consulta><from tabela='cgm'/></consulta>")
    _write(tmp_path, "ws/consulta/b/getX.xml", "<consulta><from tabela='iptubase'/></consulta>")

    records, _errors, stats = _read(tmp_path)

    assert len(records) == 2 and stats.duplicate_names == 1


def test_missing_directory_is_skipped(tmp_path: Path) -> None:
    records, errors, stats = _read(tmp_path)

    assert records == [] and errors == [] and stats.found == 0
