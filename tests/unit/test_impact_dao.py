"""Arquivos dos métodos alcançados e `dao_users` no `impact` (grafo sintético).

Um service Laravel (caminho com maiúsculas, nome do nó em minúsculas) chama `incluir` de um DAO;
o DAO chama um método dele mesmo; um método sem `path`; um chamador num arquivo de infraestrutura.
"""

from php_map.analysis.impact import ImpactOptions, impact
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    class_id,
    file_id,
    method_id,
    table_id,
)

SERVICE_FILE = "app/Domain/X/Services/FooService.php"
SERVICE_CLASS = "app\\domain\\x\\services\\fooservice"
DAO_FILE = "classes/db_widget_classe.php"
INFRA_FILE = "libs/infra.php"
TABLE = table_id("widget", "public")


def _method(builder: GraphBuilder, cls: str, name: str, path: str | None, **attrs: object) -> str:
    node = method_id(cls, name)
    data: dict[str, object] = {"name": name, "class": cls, **attrs}
    if path:
        data["path"] = path
    builder.add_node(Node(node, NodeLabel.METHOD, data))
    return node


def _link(
    builder: GraphBuilder, src: str, dst: str, kind: EdgeType, confidence: float = 1.0
) -> None:
    evidence = {"path": SERVICE_FILE, "line": 7}
    source = EdgeSource.PHP_AST if confidence == 1.0 else EdgeSource.PHP_AST_AMBIGUOUS
    builder.add_edge(Edge(src, dst, kind, source, confidence, evidence))


def _graph() -> GraphBuilder:
    builder = GraphBuilder()
    for path in (SERVICE_FILE, DAO_FILE):
        builder.add_node(Node(file_id(path), NodeLabel.FILE, {"path": path}))
    builder.add_node(
        Node(file_id(INFRA_FILE), NodeLabel.FILE, {"path": INFRA_FILE, "is_infra": True})
    )
    builder.add_node(Node(TABLE, NodeLabel.TABLE, {"name": "widget"}))
    builder.add_node(Node(class_id("cl_widget"), NodeLabel.CLASS, {"name": "cl_widget"}))
    incluir = _method(builder, "cl_widget", "incluir", DAO_FILE)
    sql_query = _method(builder, "cl_widget", "sql_query", DAO_FILE)
    _link(builder, incluir, TABLE, EdgeType.INSERTS)
    _link(builder, sql_query, TABLE, EdgeType.QUERIES)
    _link(builder, incluir, sql_query, EdgeType.CALLS)  # o DAO chamando a si mesmo
    run = _method(builder, SERVICE_CLASS, "run", SERVICE_FILE)
    _link(builder, run, incluir, EdgeType.CALLS)
    _link(builder, run, sql_query, EdgeType.CALLS)
    weak = _method(builder, SERVICE_CLASS, "weak", SERVICE_FILE)
    _link(builder, weak, incluir, EdgeType.CALLS, 0.5)
    lost = _method(builder, "ghost", "go", None)  # sem `path`: o nó é ignorado
    _link(builder, lost, incluir, EdgeType.CALLS)
    infra = _method(builder, "infra", "go", INFRA_FILE)
    _link(builder, infra, incluir, EdgeType.CALLS)
    return builder


def test_file_of_a_reached_method_uses_the_node_path_not_the_class_name() -> None:
    result = impact(_graph().to_networkx(), ImpactOptions("widget", "public"))
    by_path = {f.path: f for f in result.files}
    # `path` mantém a caixa; o nome do nó (`fooservice`) é minúsculo
    assert by_path[SERVICE_FILE].via == "método"
    assert by_path[DAO_FILE].via == "método" and by_path[DAO_FILE].depth == 1
    assert by_path[SERVICE_FILE].depth == 2
    assert "ghost" not in " ".join(by_path) and INFRA_FILE not in by_path


def test_include_infra_brings_the_infra_file_back() -> None:
    options = ImpactOptions("widget", "public", include_infra=True)
    result = impact(_graph().to_networkx(), options)
    assert INFRA_FILE in {f.path for f in result.files}


def test_dao_users_group_by_origin_and_operation_and_skip_the_dao_itself() -> None:
    result = impact(_graph().to_networkx(), ImpactOptions("widget", "public"))
    rows = [(u.origin, u.operation, u.dao_method, u.confidence) for u in result.dao_users]
    run = method_id(SERVICE_CLASS, "run")
    weak = method_id(SERVICE_CLASS, "weak")
    assert (run, "INSERT", "incluir", 1.0) in rows
    assert (run, "SELECT", "sql_query", 1.0) in rows
    assert (weak, "INSERT", "incluir", 0.5) in rows
    assert not [r for r in rows if r[0].startswith("Method:cl_widget::")]  # DAO → DAO fica fora
    assert len(rows) == len(set(rows))


def test_min_confidence_drops_the_weak_caller_from_the_dao_list_and_files() -> None:
    result = impact(_graph().to_networkx(), ImpactOptions("widget", "public", min_confidence=0.8))
    weak = method_id(SERVICE_CLASS, "weak")
    assert weak not in {u.origin for u in result.dao_users}
    assert method_id(SERVICE_CLASS, "run") in {u.origin for u in result.dao_users}
