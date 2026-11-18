"""Dois grafos sintéticos (um serve, um chama) para os testes do `cross` e da tela
Comunicação.

- `serve` (projeto `ecidade-t`, perfil `ecidade`): serve `getA`, `getB`, `procA` e uma ação de RPC,
  e chama `Prestacao` (o sentido contrário);
- `chama` (projeto `zend-t`, perfil `zend1`): chama `getA`, `procA` e `Missing`, e serve
  `Prestacao`.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from php_map.graph import store
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    endpoint_id,
    file_id,
    method_id,
    module_id,
    table_id,
)

GET_A = endpoint_id("ecidade", "soap", "getA")
GET_B = endpoint_id("ecidade", "soap", "getB")
PROC_A = endpoint_id("ecidade", "soap", "procA")
PRESTACAO = endpoint_id("zend1app", "soap", "Prestacao")
MISSING = endpoint_id("ecidade", "soap", "Missing")
DYNAMIC = endpoint_id("ecidade", "soap", "?")
EXTERNAL = endpoint_id("externo", "http", "?")
RPC = endpoint_id("ecidade", "rpc", "x.RPC.php#salvar")


def _node(builder: GraphBuilder, node_id: str, label: NodeLabel, **attrs: Any) -> None:
    builder.add_node(Node(node_id, label, {"name": node_id.partition(":")[2], **attrs}))


def _edge(
    builder: GraphBuilder,
    src: str,
    dst: str,
    type_: EdgeType,
    source: EdgeSource = EdgeSource.PHP_AST,
    confidence: float = 1.0,
    **evidence: Any,
) -> None:
    builder.add_edge(
        Edge(src, dst, type_, source, confidence, {"path": src.partition(":")[2], **evidence})
    )


def _endpoint(builder: GraphBuilder, node_id: str, **attrs: Any) -> None:
    _, system, protocol, name = node_id.split(":", 3)
    _node(
        builder,
        node_id,
        NodeLabel.ENDPOINT,
        system=system,
        protocol=protocol,
        **{**{"name": name}, **attrs},
    )


def _placeholder(builder: GraphBuilder, node_id: str) -> None:
    _, system, protocol, name = node_id.split(":", 3)
    builder.ensure_node(node_id, NodeLabel.ENDPOINT, name=name, system=system, protocol=protocol)


def build_serve() -> GraphBuilder:
    b = GraphBuilder()
    xml_a, xml_proc = file_id("ws/getA.xml"), file_id("ws/procA.xml")
    server = file_id("ws/server.php")
    for path in (xml_a, xml_proc, server, file_id("classes/x.php"), file_id("app/Helper.php")):
        _node(b, path, NodeLabel.FILE, path=path.partition(":")[2])
    _endpoint(
        b,
        GET_A,
        kind="soap_consulta",
        params=[{"name": "cnpj", "required": True, "type": None, "source": "ws_xml"}],
        path="ws/getA.xml",
        line=1,
    )
    _endpoint(b, GET_B, kind="soap_consulta", path="ws/getB.xml", line=1)
    _endpoint(b, PROC_A, kind="soap_processamento", path="ws/procA.xml", line=1)
    _endpoint(b, RPC, kind="rpc_action", path="x.RPC.php", line=9)
    for endpoint, xml in ((GET_A, xml_a), (PROC_A, xml_proc)):
        _edge(b, xml, endpoint, EdgeType.SERVES, EdgeSource.WS_XML, line=1)
        # como no e-cidade real: o despachante serve, e a evidência é o XML que ele despacha
        xml_path = xml.partition(":")[2]
        _edge(b, server, endpoint, EdgeType.SERVES, EdgeSource.WS_XML, line=1, path=xml_path)
    _edge(b, file_id("ws/getB.xml"), GET_B, EdgeType.SERVES, EdgeSource.WS_XML, line=1)
    _edge(b, file_id("x.RPC.php"), RPC, EdgeType.SERVES, line=9)

    # getA lê duas tabelas direto; procA instancia uma classe e chama um método que escreve,
    # e uma cadeia de 3 chamadas até uma tabela distante (fora de depth=3).
    for table in ("t1", "t3"):
        _edge(b, GET_A, table_id(table), EdgeType.QUERIES, EdgeSource.WS_XML, path="ws/getA.xml")
    run = method_id("cl_x", "run")
    _node(b, run, NodeLabel.METHOD, path="classes/x.php", class_="cl_x")
    _edge(b, PROC_A, "Class:cl_x", EdgeType.INSTANTIATES, EdgeSource.WS_XML, path="ws/procA.xml")
    _edge(b, PROC_A, run, EdgeType.CALLS, EdgeSource.WS_XML, path="ws/procA.xml")
    _edge(b, run, table_id("t2"), EdgeType.UPDATES, line=7, path="classes/x.php")
    _edge(b, run, table_id("t2"), EdgeType.UPDATES, line=8, path="classes/x.php")  # agregada
    chain = [method_id("deep", name) for name in ("a", "b", "c")]
    for method in chain:
        _node(b, method, NodeLabel.METHOD, path="classes/x.php")
    _edge(b, run, chain[0], EdgeType.CALLS, path="classes/x.php")
    _edge(b, chain[0], chain[1], EdgeType.CALLS, path="classes/x.php")
    _edge(b, chain[1], chain[2], EdgeType.CALLS, path="classes/x.php")
    _edge(b, chain[2], table_id("far"), EdgeType.QUERIES, path="classes/x.php")
    # infraestrutura não é atravessada
    _node(b, "Function:db_query", NodeLabel.FUNCTION, path="libs/db.php", is_infra=True)
    _edge(b, run, "Function:db_query", EdgeType.CALLS, path="classes/x.php")
    _edge(b, "Function:db_query", table_id("infra_only"), EdgeType.QUERIES, path="libs/db.php")

    # módulos: 1.0 e uma regra de nome mais fraca para o mesmo arquivo
    _edge(b, file_id("classes/x.php"), module_id("empenho"), EdgeType.BELONGS_TO, line=1)
    b.add_edge(
        Edge(
            file_id("classes/x.php"),
            module_id("outro"),
            EdgeType.BELONGS_TO,
            EdgeSource.NAMING_RULE,
            0.6,
            {"path": "classes/x.php"},
        )
    )

    # o sentido contrário: o e-cidade chama o endpoint que o projeto zend1 serve
    helper = method_id("helper", "prestar")
    _node(b, helper, NodeLabel.METHOD, path="app/Helper.php")
    _placeholder(b, PRESTACAO)
    _edge(
        b,
        helper,
        PRESTACAO,
        EdgeType.CALLS_REMOTE,
        path="app/Helper.php",
        line=49,
        args={"argc": 2, "literals": ["P"]},
    )
    _edge(b, file_id("app/Helper.php"), module_id("tributario"), EdgeType.BELONGS_TO, line=1)
    _placeholder(b, EXTERNAL)
    for line in (60, 61, 62):
        b.add_edge(
            Edge(
                helper,
                EXTERNAL,
                EdgeType.CALLS_REMOTE,
                EdgeSource.PHP_AST_AMBIGUOUS,
                0.5,
                {"path": "app/Helper.php", "line": line, "via": "$url"},
            )
        )
    # uma rota do Laravel (entra pelo `ROUTES_TO`; o módulo vem do arquivo do controller)
    route = "Route:GET /v4/api/x/{id}"
    _node(
        b,
        route,
        NodeLabel.ROUTE,
        verb="GET",
        uri="/v4/api/x/{id}",
        area="api",
        middleware=["auth"],
        namespace="",
        params=[{"name": "id", "required": True}],
        action="App.X@show",
        path="routes/api.php",
        line=5,
    )
    show = method_id("app_x", "show")
    _node(b, show, NodeLabel.METHOD, path="app/X.php", line_start=10)
    laravel = EdgeSource.LARAVEL_RESOLVER
    _edge(b, route, show, EdgeType.ROUTES_TO, laravel, path="routes/api.php", line=5)
    _edge(b, file_id("app/X.php"), module_id("tributario"), EdgeType.BELONGS_TO, line=1)
    return b


def build_chama() -> GraphBuilder:
    b = GraphBuilder()
    models = {
        "fiscal": file_id("application/modules/fiscal/models/A.php"),
        "contribuinte": file_id("application/modules/contribuinte/models/B.php"),
        "webservice": file_id("application/modules/webservice/models/P.php"),
    }
    for module, path in models.items():
        _node(b, path, NodeLabel.FILE, path=path.partition(":")[2])
        _edge(
            b,
            path,
            module_id(module),
            EdgeType.BELONGS_TO,
            EdgeSource.ZEND1_RESOLVER,
            line=1,
        )
    m_a, m_b = method_id("fiscal_a", "x"), method_id("contribuinte_b", "y")
    prestacao = method_id("webservice_p", "prestacao")
    _node(b, m_a, NodeLabel.METHOD, path="application/modules/fiscal/models/A.php")
    _node(b, m_b, NodeLabel.METHOD, path="application/modules/contribuinte/models/B.php")
    _node(b, prestacao, NodeLabel.METHOD, path="application/modules/webservice/models/P.php")
    paths = {
        m_a: "application/modules/fiscal/models/A.php",
        m_b: "application/modules/contribuinte/models/B.php",
    }
    for caller, target, line in (
        (m_a, GET_A, 10),
        (m_b, GET_A, 20),
        (m_b, GET_A, 21),  # mesma aresta (src, dst): agregada em 2 ocorrências
        (m_b, PROC_A, 30),
        (m_b, MISSING, 40),
    ):
        _placeholder(b, target)
        _edge(
            b,
            caller,
            target,
            EdgeType.CALLS_REMOTE,
            line=line,
            path=paths[caller],
            args={"argc": 2, "literals": [target.rpartition(":")[2]]},
        )
    _placeholder(b, DYNAMIC)
    b.add_edge(
        Edge(
            m_a,
            DYNAMIC,
            EdgeType.CALLS_REMOTE,
            EdgeSource.PHP_AST_AMBIGUOUS,
            0.5,
            {"path": "application/modules/fiscal/models/A.php", "line": 99},
        )
    )
    _endpoint(b, PRESTACAO, kind="soap_class", path="application/modules/webservice/P.php", line=3)
    _edge(b, prestacao, PRESTACAO, EdgeType.SERVES, line=63)
    _edge(b, prestacao, table_id("zend_t"), EdgeType.UPDATES, line=70)
    # tela (phtml) do projeto zend1 que chama o método: entrada do `trace`
    view = file_id("application/modules/fiscal/views/scripts/a.phtml")
    _node(b, view, NodeLabel.FILE, path=view.partition(":")[2])
    _edge(b, view, m_b, EdgeType.CALLS, line=2)
    _edge(b, view, m_a, EdgeType.CALLS, line=3)
    # uma rota do Zend 1: o módulo é o atributo da própria rota
    zroute = "Route:fiscal/pedido/index"
    _node(b, zroute, NodeLabel.ROUTE, module="fiscal", controller="pedido", action="index")
    action = method_id("fiscal_pedidocontroller", "indexaction")
    ctrl_path = "application/modules/fiscal/controllers/N.php"
    _node(b, action, NodeLabel.METHOD, path=ctrl_path, line_start=12)
    _edge(
        b,
        zroute,
        action,
        EdgeType.ROUTES_TO,
        EdgeSource.ZEND1_RESOLVER,
        path="application/modules/fiscal/controllers/N.php",
        line=12,
    )
    return b


META_SERVE = {"project": "ecidade-t", "project_name": "e-cidade teste", "profile": "ecidade"}
META_CHAMA = {"project": "zend-t", "project_name": "Zend teste", "profile": "zend1"}


def save(tmp_path: Path, name: str, builder: GraphBuilder, meta: Mapping[str, str]) -> Path:
    db = tmp_path / name / "graph.db"
    store.save(builder, db, meta)
    return db
