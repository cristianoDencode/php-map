"""`Endpoint`, `SERVES` e `CALLS_REMOTE`, sem Docker.

Registros à mão no formato do extrator (schema_version 6); os XML do e-cidade ficam em `tmp_path`.
`declares[].name` e `class` em minúsculas, e a caixa original só em `display_name`, como o
extrator de verdade devolve.
"""

from pathlib import Path
from typing import Any

from conftest import PhpRecords

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Edge, EdgeSource, EdgeType, Node, NodeLabel, file_id
from php_map.ingest.records import PhpFileRecord
from php_map.ingest.table_directory import TableDirectory
from php_map.profiles.base import RemoteCallPattern, WsXmlSource
from php_map.resolve.endpoints import apply_endpoints
from php_map.resolve.unresolved import UnresolvedWriter

ZEND_CALLS = (RemoteCallPattern("ecidade", frozenset({"consultar", "processar"}), "ecidade"),)
ECIDADE_CALLS = (RemoteCallPattern("integracaonfsehelper", frozenset({"request"}), "zend1app"),)
SOAP_SERVER = ("soapserver", "zend_soap_server")
XML_SOURCES = (
    WsXmlSource(
        "soap_consulta",
        "webservices/consulta",
        "consulta",
        ("webservices/consulta/Servidor.php", "webservices/requisicao.php"),
    ),
    WsXmlSource(
        "soap_processamento", "webservices/proc", "processamento", ("webservices/requisicao.php",)
    ),
)


def _call(  # noqa: PLR0913
    php: PhpRecords,
    kind: str,
    name: str,
    *,
    cls: str | None = None,
    arg0: str | None = None,
    receiver: str | None = None,
    scope: str = "method:app_model::run",
    line: int = 7,
) -> dict[str, Any]:
    call = php.call(kind, name, cls=cls, receiver=receiver, line=line, scope=scope)
    return {**call, "arg0": arg0, "argc": 2}


def _run(
    records: list[PhpFileRecord],
    settings: Settings,
    builder: GraphBuilder | None = None,
    directory: TableDirectory | None = None,
) -> tuple[GraphBuilder, UnresolvedWriter, Any]:
    builder = builder or GraphBuilder()
    unresolved = UnresolvedWriter()
    stats = apply_endpoints(records, builder, unresolved, settings, directory)
    unresolved.close()
    return builder, unresolved, stats


def _edges(builder: GraphBuilder, type_: EdgeType) -> dict[tuple[str, str], Any]:
    return {(e.src, e.dst): e for e in builder.iter_edges() if e.type == type_}


def _zend(**fields: Any) -> Settings:
    base: dict[str, Any] = {
        "endpoint_system": "zend1app",
        "remote_calls": ZEND_CALLS,
        "soap_server_classes": SOAP_SERVER,
    }
    return Settings(**{**base, **fields})


# ------------------------------------------------------------------ quem chama


def test_literal_remote_call_makes_calls_remote_to_a_placeholder_endpoint(php: PhpRecords) -> None:
    record = php.record(
        "application/modules/administrativo/models/Aidof.php",
        calls=[
            _call(
                php,
                "static",
                "consultar",
                cls="webservice_model_ecidade",
                arg0="getAidof",
                line=111,
            )
        ],
    )

    builder, unresolved, stats = _run([record], _zend())

    edge = _edges(builder, EdgeType.CALLS_REMOTE)[
        ("Method:app_model::run", "Endpoint:ecidade:soap:getAidof")
    ]
    assert (edge.source, edge.confidence) == (EdgeSource.PHP_AST, 1.0)
    assert dict(edge.evidence)["line"] == 111
    assert edge.evidence["args"] == {"argc": 2, "literals": ["getAidof"]}
    attrs = builder.node_attrs("Endpoint:ecidade:soap:getAidof")
    assert attrs["resolved"] is False  # quem serve está no grafo do outro projeto
    assert (attrs["system"], attrs["protocol"], attrs["name"]) == ("ecidade", "soap", "getAidof")
    assert (stats.remote_calls, stats.called_endpoints) == (1, 1) and not unresolved.counts


def test_processar_counts_and_the_same_call_repeated_is_aggregated(php: PhpRecords) -> None:
    calls = [
        _call(php, "static", "processar", cls="webservice_model_ecidade", arg0="DadosPrefeitura"),
        _call(php, "static", "processar", cls="webservice_model_ecidade", arg0="DadosPrefeitura"),
    ]
    record = php.record("application/modules/administrativo/models/Prefeitura.php", calls=calls)

    builder, _unresolved, stats = _run([record], _zend())

    edge = _edges(builder, EdgeType.CALLS_REMOTE)[
        ("Method:app_model::run", "Endpoint:ecidade:soap:DadosPrefeitura")
    ]
    assert stats.remote_calls == 2
    assert [e for e in builder.iter_edges() if e.type == EdgeType.CALLS_REMOTE] == [edge]
    assert edge.attrs["occurrences"] == 2  # a mesma chamada repetida soma, não duplica


def test_non_literal_method_name_makes_the_question_mark_endpoint_and_a_pending(
    php: PhpRecords,
) -> None:
    record = php.record(
        "application/modules/fiscal/models/ConsultaEmpresa.php",
        calls=[
            _call(php, "static", "consultar", cls="webservice_model_ecidade", arg0=None, line=18)
        ],
    )

    builder, unresolved, stats = _run([record], _zend())

    edge = _edges(builder, EdgeType.CALLS_REMOTE)[
        ("Method:app_model::run", "Endpoint:ecidade:soap:?")
    ]
    assert (edge.source, edge.confidence) == (EdgeSource.PHP_AST_AMBIGUOUS, 0.5)
    [pending] = [r for r in unresolved.records if r.kind == "dynamic_endpoint"]
    assert (pending.line, pending.hint) == (18, "method:app_model::run")
    assert stats.remote_calls_dynamic == 1 and stats.called_endpoints == 0


def test_calls_to_other_classes_or_methods_are_not_remote(php: PhpRecords) -> None:
    calls = [
        _call(php, "static", "consultar", cls="app_model_banco", arg0="getX"),  # outra classe
        _call(
            php, "static", "formatar", cls="webservice_model_ecidade", arg0="getY"
        ),  # outro método
        _call(php, "method", "consultar", receiver="webservice_model_ecidade", arg0="getZ"),
    ]
    record = php.record("application/modules/fiscal/models/X.php", calls=calls)

    builder, _unresolved, stats = _run([record], _zend())

    assert not _edges(builder, EdgeType.CALLS_REMOTE) and stats.remote_calls == 0


def test_ecidade_calls_the_other_system_by_the_integration_helper(php: PhpRecords) -> None:
    helper = "app" + chr(92) + "domain" + chr(92) + "helpers" + chr(92) + "integracaonfsehelper"
    record = php.record(
        "app/Domain/Tributario/Helpers/IntegracaoNfseHelper.php",
        calls=[
            _call(php, "static", "request", cls=helper, arg0="PrestacaoContas", scope="method:x::p")
        ],
    )
    settings = Settings(endpoint_system="ecidade", remote_calls=ECIDADE_CALLS)

    builder, _unresolved, _stats = _run([record], settings)

    # O ID nomeia quem serve: o e-cidade chama, então o endpoint é do `zend-app`.
    assert ("Method:x::p", "Endpoint:zend1app:soap:PrestacaoContas") in _edges(
        builder, EdgeType.CALLS_REMOTE
    )


# ------------------------------------------------------------------ quem serve: setClass


def _served_class(php: PhpRecords, server_class: str = "soapserver") -> list[PhpFileRecord]:
    methods = [
        {**php.decl("method", "acessoautenticado", cls="webservice_model_integracao", line=39),
         "visibility": "protected", "display_name": "acessoAutenticado", "params": []},
        {**php.decl("method", "acesso", cls="webservice_model_integracao", line=98),
         "visibility": "public", "display_name": "Acesso",
         "params": [{"name": "sXml", "has_default": False, "type": None}]},
        {**php.decl("method", "prestacaocontas", cls="webservice_model_integracao", line=234),
         "visibility": "public", "display_name": "PrestacaoContas",
         "params": [{"name": "sXml", "has_default": False, "type": "string"},
                    {"name": "lDetalhar", "has_default": True, "type": None}]},
        {**php.decl("method", "__construct", cls="webservice_model_integracao", line=30),
         "visibility": "public", "display_name": "__construct", "params": []},
    ]  # fmt: skip
    model = php.record("application/modules/webservice/models/Integracao.php", declares=methods)
    controller = php.record(
        "application/modules/webservice/controllers/IndexController.php",
        calls=[
            _call(
                php,
                "method",
                "setclass",
                arg0="WebService_Model_Integracao",
                receiver=server_class,
                line=126,
                scope="method:webservice_indexcontroller::integracaoaction",
            )
        ],
    )
    return [controller, model]


def test_set_class_makes_one_endpoint_per_public_method(php: PhpRecords) -> None:
    builder, _unresolved, stats = _run(_served_class(php), _zend())

    served = {e.dst: e for e in builder.iter_edges() if e.type == EdgeType.SERVES}
    assert sorted(served) == [
        "Endpoint:zend1app:soap:Acesso",
        "Endpoint:zend1app:soap:PrestacaoContas",
    ]
    edge = _edges(builder, EdgeType.SERVES)[
        (
            "Method:webservice_model_integracao::prestacaocontas",
            "Endpoint:zend1app:soap:PrestacaoContas",
        )
    ]
    assert (edge.source, edge.confidence) == (EdgeSource.PHP_AST, 1.0)
    assert dict(edge.evidence)["line"] == 126  # onde foi publicado
    attrs = builder.node_attrs("Endpoint:zend1app:soap:PrestacaoContas")
    assert (attrs["kind"], attrs["resolved"], attrs["wsdl"]) == ("soap_class", True, None)
    assert attrs["params"] == [
        {"name": "sXml", "required": True, "type": "string", "source": "php_signature"},
        {"name": "lDetalhar", "required": False, "type": None, "source": "php_signature"},
    ]
    assert (stats.served_classes, stats.served_by_class) == (1, 2)


def test_set_class_on_a_receiver_that_is_not_a_soap_server_is_ignored(php: PhpRecords) -> None:
    builder, _unresolved, stats = _run(_served_class(php, server_class="outraclasse"), _zend())

    assert not _edges(builder, EdgeType.SERVES) and stats.served_classes == 0


def test_the_ecidade_profile_does_not_turn_its_set_class_into_endpoints(php: PhpRecords) -> None:
    """`$oSoapServer->setClass("ConsultaDados")` é despacho: os endpoints são os XML."""
    settings = Settings(endpoint_system="ecidade", remote_calls=ECIDADE_CALLS)  # sem classes SOAP

    builder, _unresolved, _stats = _run(_served_class(php), settings)

    assert not _edges(builder, EdgeType.SERVES)


def test_add_function_with_a_literal_makes_a_function_endpoint(php: PhpRecords) -> None:
    builder = GraphBuilder()
    builder.add_node(Node("Function:recepcionarloterps", NodeLabel.FUNCTION, {"name": "x"}))
    record = php.record(
        "application/modules/webservice/controllers/IndexController.php",
        calls=[
            _call(php, "method", "addfunction", arg0="RecepcionarLoteRps", receiver="soapserver")
        ],
    )

    builder, _unresolved, _stats = _run([record], _zend(), builder)

    assert ("Function:recepcionarloterps", "Endpoint:zend1app:soap:recepcionarloterps") in _edges(
        builder, EdgeType.SERVES
    )


# ------------------------------------------------------------------ quem serve: XML do e-cidade


def _xml(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _ecidade(root: Path) -> Settings:
    return Settings(
        source=root,
        endpoint_system="ecidade",
        ws_xml=XML_SOURCES,
        ws_xml_ignore_prefixes=("modelo.",),
        encoding="latin-1",
        search_path=("public",),
    )


def test_consulta_xml_makes_the_endpoint_serves_and_queries(tmp_path: Path) -> None:
    _xml(
        tmp_path,
        "webservices/consulta/getCgm.xml",
        "<consulta><from tabela='cgm'/><juncoes><join tipo='left' tabela='cidadao c' condicao='x'/>"
        "</juncoes><filtros><filtro label='cpf' condicao='x' obrigatorio='true'/></filtros>"
        "</consulta>",
    )
    builder = GraphBuilder()
    builder.add_node(
        Node(file_id("webservices/consulta/Servidor.php"), NodeLabel.FILE, {"path": "x"})
    )
    directory = TableDirectory(("public",))
    directory.add("public", "cgm")

    builder, unresolved, stats = _run([], _ecidade(tmp_path), builder, directory)

    eid = "Endpoint:ecidade:soap:getCgm"
    attrs = builder.node_attrs(eid)
    assert (attrs["kind"], attrs["path"], attrs["resolved"]) == (
        "soap_consulta",
        "webservices/consulta/getCgm.xml",
        True,
    )
    assert attrs["params"] == [{"name": "cpf", "required": True, "type": None, "source": "ws_xml"}]
    serves = {e.src: e for e in builder.iter_edges() if e.type == EdgeType.SERVES}
    # O XML e o servidor PHP que existe entre os arquivos; o `requisicao.php` não foi escaneado.
    assert sorted(serves) == [
        "File:webservices/consulta/Servidor.php",
        "File:webservices/consulta/getCgm.xml",
    ]
    assert all(e.source == EdgeSource.WS_XML and e.confidence == 1.0 for e in serves.values())
    queries = _edges(builder, EdgeType.QUERIES)
    assert sorted(dst for (_src, dst) in queries) == ["Table:public.cgm", "Table:public.cidadao"]
    assert (stats.served_xml, stats.queries_edges, stats.join_table_edges) == (1, 2, 1)
    assert (stats.serves_edges, stats.serves_server_edges) == (1, 1) and not unresolved.counts


def test_processamento_xml_confirms_the_class_by_the_file_that_declares_it(tmp_path: Path) -> None:
    _xml(
        tmp_path,
        "webservices/proc/liquida.xml",
        "<processamento><configuracoes><classe nome='empenho' caminho='classes/empenho.php'>"
        "<metodos><metodo nome='liquidar'/></metodos></classe></configuracoes></processamento>",
    )
    builder = GraphBuilder()
    builder.add_node(Node(file_id("classes/empenho.php"), NodeLabel.FILE, {"path": "x"}))
    builder.add_node(Node("Class:empenho", NodeLabel.CLASS, {"name": "empenho"}))
    builder.add_node(Node("Method:empenho::liquidar", NodeLabel.METHOD, {"name": "liquidar"}))
    evidence = {"path": "classes/empenho.php", "line": 1}
    builder.add_edge(
        Edge(
            "File:classes/empenho.php",
            "Class:empenho",
            EdgeType.DECLARES,
            EdgeSource.PHP_AST,
            1.0,
            evidence,
        )
    )

    builder, unresolved, stats = _run([], _ecidade(tmp_path), builder)

    eid = "Endpoint:ecidade:soap:liquida"
    assert (eid, "Class:empenho") in _edges(builder, EdgeType.INSTANTIATES)
    assert (eid, "Method:empenho::liquidar") in _edges(builder, EdgeType.CALLS)
    assert (stats.classes_confirmed, stats.classes_missing) == (1, 0)
    assert "ws_class_missing" not in unresolved.counts


def test_processamento_xml_with_a_class_the_code_does_not_have_is_reported(tmp_path: Path) -> None:
    _xml(
        tmp_path,
        "webservices/proc/gera.xml",
        "<processamento><configuracoes><classe nome='Fantasma' caminho='model/Fantasma.model.php'/>"
        "</configuracoes></processamento>",
    )

    builder, unresolved, stats = _run([], _ecidade(tmp_path))

    # A aresta existe (1.0: o XML declara) e aponta para o placeholder; a pendência diz o que falta.
    assert ("Endpoint:ecidade:soap:gera", "Class:fantasma") in _edges(
        builder, EdgeType.INSTANTIATES
    )
    assert builder.node_attrs("Class:fantasma")["resolved"] is False
    [pending] = [r for r in unresolved.records if r.kind == "ws_class_missing"]
    assert (pending.snippet, pending.candidates) == ("Fantasma", ["model/Fantasma.model.php"])
    assert (stats.classes_confirmed, stats.classes_missing) == (0, 1)


def test_malformed_xml_and_model_files_do_not_become_endpoints(tmp_path: Path) -> None:
    _xml(tmp_path, "webservices/consulta/getQuebrado.xml", "<consulta><from tabela='cgm'>")
    _xml(tmp_path, "webservices/proc/modelo.1.0.xml", "<processamento/>")

    builder, unresolved, stats = _run([], _ecidade(tmp_path))

    assert not [n for n in builder.iter_nodes() if n.label == NodeLabel.ENDPOINT]
    assert unresolved.counts["ws_xml_parse_error"] == 1
    assert (stats.xml_parse_errors, stats.xml_ignored_by_name) == (1, 1)


def test_duplicate_endpoint_names_are_reported_without_overwriting(tmp_path: Path) -> None:
    _xml(tmp_path, "webservices/consulta/a/getX.xml", "<consulta><from tabela='cgm'/></consulta>")
    _xml(
        tmp_path,
        "webservices/consulta/b/getX.xml",
        "<consulta><from tabela='iptubase'/></consulta>",
    )

    builder, unresolved, stats = _run([], _ecidade(tmp_path))

    assert unresolved.counts["ws_endpoint_duplicate"] == 1 and stats.served_xml == 1
    assert {dst for (_src, dst) in _edges(builder, EdgeType.QUERIES)} == {
        "Table:public.cgm",
        "Table:public.iptubase",
    }  # as duas fontes continuam registradas


def test_a_profile_without_endpoints_does_nothing(php: PhpRecords) -> None:
    record = php.record(
        "x.php", calls=[_call(php, "static", "consultar", cls="x_ecidade", arg0="getX")]
    )

    builder, _unresolved, stats = _run([record], Settings())

    assert len(builder) == 0 and stats.remote_calls == 0
