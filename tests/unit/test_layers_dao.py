"""A camada DAO (`classes/db_*_classe.php`) nas camadas de um fonte.

Grafo sintético mínimo: um service chama `getDao('x')`/`usarDao('y')` e usa os métodos do DAO.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from php_map.analysis.layers import MAX_DOWN_NODES, LayerOptions, file_layers
from php_map.cli import app as cli_app
from php_map.graph import store
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
from php_map.mcp import tools
from php_map.mcp.catalog import ProjectCatalog
from php_map.report.layers import to_text

SERVICE_FILE = "app/Domain/X/Services/FooService.php"
SERVICE_CLASS = "app\\domain\\x\\services\\fooservice"
SERVICE_METHOD = method_id(SERVICE_CLASS, "run")


def _edge(
    builder: GraphBuilder, src: str, dst: str, kind: EdgeType, via: str | None = None
) -> None:
    evidence: dict[str, object] = {"path": SERVICE_FILE, "line": 10}
    if via:
        evidence["via"] = via
    builder.add_edge(Edge(src, dst, kind, EdgeSource.PHP_AST, 1.0, evidence))


def _dao_class(builder: GraphBuilder, name: str, methods: tuple[str, ...]) -> None:
    path = f"classes/db_{name.removeprefix('cl_')}_classe.php"
    builder.add_node(Node(file_id(path), NodeLabel.FILE, {"path": path}))
    builder.add_node(Node(class_id(name), NodeLabel.CLASS, {"name": name, "path": path}))
    for method in ("__construct", *methods):
        node = method_id(name, method)
        attrs = {"name": method, "class": name, "path": path}
        builder.add_node(Node(node, NodeLabel.METHOD, attrs))
        builder.add_edge(
            Edge(class_id(name), node, EdgeType.DECLARES, EdgeSource.PHP_AST, 1.0, {"path": path})
        )


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    builder = GraphBuilder()
    builder.add_node(Node(file_id(SERVICE_FILE), NodeLabel.FILE, {"path": SERVICE_FILE}))
    builder.add_node(
        Node(
            class_id(SERVICE_CLASS),
            NodeLabel.CLASS,
            {"name": SERVICE_CLASS, "path": SERVICE_FILE},
        )
    )
    builder.add_node(
        Node(
            SERVICE_METHOD,
            NodeLabel.METHOD,
            {"name": "run", "class": SERVICE_CLASS, "path": SERVICE_FILE},
        )
    )
    builder.add_edge(
        Edge(
            file_id(SERVICE_FILE),
            SERVICE_METHOD,
            EdgeType.DECLARES,
            EdgeSource.PHP_AST,
            1.0,
            {"path": SERVICE_FILE},
        )
    )
    # cl_alfa: instanciado por getDao; chama incluir e sql_record; incluir grava na tabela.
    _dao_class(builder, "cl_alfa", ("incluir", "sql_record", "naochamado"))
    builder.add_node(Node(table_id("alfa", "protocolo"), NodeLabel.TABLE, {"name": "alfa"}))
    _edge(builder, SERVICE_METHOD, class_id("cl_alfa"), EdgeType.INSTANTIATES, "dao_factory:getdao")
    _edge(
        builder,
        SERVICE_METHOD,
        method_id("cl_alfa", "incluir"),
        EdgeType.CALLS,
        "dao_factory:getdao",
    )
    _edge(builder, SERVICE_METHOD, method_id("cl_alfa", "sql_record"), EdgeType.CALLS)
    _edge(builder, method_id("cl_alfa", "incluir"), table_id("alfa", "protocolo"), EdgeType.INSERTS)
    # cl_beta: usarDao.
    _dao_class(builder, "cl_beta", ("alterar",))
    _edge(
        builder, SERVICE_METHOD, class_id("cl_beta"), EdgeType.INSTANTIATES, "dao_factory:usardao"
    )
    _edge(builder, SERVICE_METHOD, method_id("cl_beta", "alterar"), EdgeType.CALLS)
    # cl_gama: só um método (receptor tipado), sem a aresta INSTANTIATES para a classe.
    _dao_class(builder, "cl_gama", ("excluir",))
    _edge(
        builder,
        SERVICE_METHOD,
        method_id("cl_gama", "excluir"),
        EdgeType.CALLS,
        "dao_factory:getdao",
    )
    # cl_delta: declarada também numa cópia (`db_delta_antes.php`), como `cl_cgm` no e-cidade:
    # o caminho do nó e de um método é o da cópia, mas a classe é um DAO.
    _dao_class(builder, "cl_delta", ("incluir",))
    builder.add_node(
        Node(
            class_id("cl_delta"),
            NodeLabel.CLASS,
            {
                "name": "cl_delta",
                "path": "classes/db_delta_antes.php",
                "declared_in": ["classes/db_delta_antes.php", "classes/db_delta_classe.php"],
            },
        ),
        replace=True,
    )
    builder.add_node(
        Node(
            method_id("cl_delta", "incluir"),
            NodeLabel.METHOD,
            {"name": "incluir", "class": "cl_delta", "path": "classes/db_delta_antes.php"},
        ),
        replace=True,
    )
    _edge(
        builder, SERVICE_METHOD, class_id("cl_delta"), EdgeType.INSTANTIATES, "dao_factory:getdao"
    )
    _edge(builder, SERVICE_METHOD, method_id("cl_delta", "incluir"), EdgeType.CALLS)
    # Uma classe legada fora do padrão (`classes/empenho.php`) continua `outro`.
    builder.add_node(
        Node(file_id("classes/empenho.php"), NodeLabel.FILE, {"path": "classes/empenho.php"})
    )
    builder.add_node(
        Node(
            class_id("empenho"), NodeLabel.CLASS, {"name": "empenho", "path": "classes/empenho.php"}
        )
    )
    _edge(builder, SERVICE_METHOD, class_id("empenho"), EdgeType.INSTANTIATES)
    path = tmp_path_factory.mktemp("dao") / "graph.db"
    store.save(builder, path, {"source": "x", "profile": "ecidade"})
    return path


@pytest.fixture
def conn(db: Path) -> Iterator[sqlite3.Connection]:
    with closing(store.connect_readonly(db)) as connection:
        yield connection


def _dao(result, name: str):  # type: ignore[no-untyped-def]
    found = [d for d in result.down if d.layer == "dao" and d.name == name]
    assert len(found) == 1, [d.name for d in result.down]
    return found[0]


def test_dao_is_one_item_per_class_with_members(conn: sqlite3.Connection) -> None:
    result = file_layers(conn, file_id(SERVICE_FILE))

    alfa = _dao(result, "cl_alfa")
    assert alfa.node == "Class:cl_alfa" and alfa.label == "Class"
    assert [m.name for m in alfa.members] == ["incluir", "sql_record"]  # sem __construct
    assert {m.edge_type for m in alfa.members} == {"CALLS"}
    assert alfa.factory == "getDao"
    assert alfa.edge_type == "INSTANTIATES" and alfa.depth == 1
    assert alfa.path == "classes/db_alfa_classe.php"
    # Nenhum método do DAO aparece como item solto.
    assert not [d for d in result.down if d.label == "Method" and d.layer == "dao"]
    assert result.down_by_layer["dao"] == 4  # classes, não métodos
    assert sum(result.down_by_layer.values()) == result.down_total


def test_table_child_hangs_from_the_class(conn: sqlite3.Connection) -> None:
    result = file_layers(conn, file_id(SERVICE_FILE))

    table = next(d for d in result.down if d.node == "Table:protocolo.alfa")
    assert table.layer == "tabela" and table.edge_type == "INSERTS"
    assert table.parent == "Class:cl_alfa"
    assert table.depth > _dao(result, "cl_alfa").depth


def test_factory_getdao_usardao_and_unknown(conn: sqlite3.Connection) -> None:
    result = file_layers(conn, file_id(SERVICE_FILE))

    assert _dao(result, "cl_alfa").factory == "getDao"
    assert _dao(result, "cl_beta").factory == "usarDao"
    assert [m.name for m in _dao(result, "cl_beta").members] == ["alterar"]


def test_method_reached_without_the_class(conn: sqlite3.Connection) -> None:
    result = file_layers(conn, file_id(SERVICE_FILE))

    gama = _dao(result, "cl_gama")
    assert gama.node == "Class:cl_gama" and gama.label == "Class"
    assert [m.name for m in gama.members] == ["excluir"]
    assert gama.factory == "getDao" and gama.edge_type == "CALLS"  # a aresta do método
    assert gama.parent == SERVICE_METHOD


def test_class_declared_in_a_copy_is_still_a_dao(conn: sqlite3.Connection) -> None:
    result = file_layers(conn, file_id(SERVICE_FILE))

    delta = _dao(result, "cl_delta")
    assert [m.name for m in delta.members] == ["incluir"]
    assert delta.path == "classes/db_delta_classe.php" and delta.line is None
    assert not [d for d in result.down if d.name.startswith("cl_delta::")]


def test_legacy_class_outside_the_dao_pattern_stays_other(conn: sqlite3.Connection) -> None:
    result = file_layers(conn, file_id(SERVICE_FILE))

    assert any(d.name == "empenho" and d.layer == "outro" for d in result.down)


def test_no_layer_is_dropped_by_the_limit(conn: sqlite3.Connection) -> None:
    assert MAX_DOWN_NODES == 500
    result = file_layers(conn, file_id(SERVICE_FILE))

    assert len(result.down) == min(result.down_total, MAX_DOWN_NODES)
    tight = file_layers(conn, file_id(SERVICE_FILE), LayerOptions(max_down_nodes=2))
    assert len(tight.down) <= 2 and tight.down_by_layer == result.down_by_layer


def test_json_has_members_and_factory(conn: sqlite3.Connection) -> None:
    data = file_layers(conn, file_id(SERVICE_FILE)).to_json()

    alfa = next(d for d in data["down"] if d["name"] == "cl_alfa")
    assert alfa["factory"] == "getDao"
    assert [m["name"] for m in alfa["members"]] == ["incluir", "sql_record"]
    assert set(alfa["members"][0]) == {"name", "node", "edge_type", "confidence"}
    other = next(d for d in data["down"] if d["name"] == "empenho")
    assert other["members"] == [] and other["factory"] is None


def test_text_line_of_the_dao(conn: sqlite3.Connection) -> None:
    text = to_text(file_layers(conn, file_id(SERVICE_FILE)))

    assert "DAO (4):" in text
    assert "cl_alfa — via getDao('alfa'): incluir(), sql_record()" in text
    assert "cl_beta — via usarDao('beta'): alterar()" in text


def test_text_per_layer_limit(conn: sqlite3.Connection) -> None:
    text = to_text(file_layers(conn, file_id(SERVICE_FILE)), per_layer=1)

    assert "cl_alfa" in text and "cl_beta" not in text and "… +3" in text


def test_cli_and_mcp(db: Path) -> None:
    out = CliRunner().invoke(
        cli_app, ["layers", "--graph", str(db), SERVICE_FILE, "--per-layer", "9"]
    )
    assert out.exit_code == 0, out.output
    assert "cl_gama — via getDao('gama'): excluir()" in out.output

    catalog = ProjectCatalog.from_dbs([db])
    slug = catalog.projects[0].slug
    data = tools.layers(catalog, slug, SERVICE_FILE, max_down=500)
    alfa = next(d for d in data["down"] if d["name"] == "cl_alfa")
    assert alfa["factory"] == "getDao" and alfa["members"][0]["name"] == "incluir"
