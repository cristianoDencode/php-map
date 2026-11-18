import pytest

from php_map.errors import GraphContractError
from php_map.graph.builder import MAX_SAMPLE_LINES, GraphBuilder
from php_map.graph.model import Edge, EdgeSource, EdgeType, Node, NodeLabel, table_id

SCREEN = "File:emp4_anularempenho001.php"
TABLE = table_id("empempenho")


def _query(line: int, source: EdgeSource = EdgeSource.SQL_AST, confidence: float = 1.0) -> Edge:
    return Edge(
        SCREEN,
        TABLE,
        EdgeType.QUERIES,
        source,
        confidence,
        {"path": "emp4_anularempenho001.php", "line": line},
    )


def test_same_edge_1000_times_is_aggregated() -> None:
    builder = GraphBuilder()
    for line in range(1, 1001):
        builder.add_edge(_query(line))

    (edge,) = builder.iter_edges()
    assert edge.attrs["occurrences"] == 1000
    assert len(edge.attrs["sample_lines"]) == MAX_SAMPLE_LINES == 5
    assert [s["line"] for s in edge.attrs["sample_lines"]] == [1, 2, 3, 4, 5]
    assert edge.evidence["line"] == 1  # primeira evidência
    assert builder.stats().edges == 1


def test_different_source_is_a_different_edge() -> None:
    builder = GraphBuilder()
    builder.add_edge(_query(1))
    builder.add_edge(_query(2, EdgeSource.SQL_REGEX, 0.7))

    assert builder.stats().edges_by_source == {"SQL_AST": 1, "SQL_REGEX": 1}
    graph = builder.to_networkx()
    assert set(graph[SCREEN][TABLE]) == {"QUERIES", "QUERIES:SQL_REGEX"}


def test_missing_endpoints_become_placeholders() -> None:
    builder = GraphBuilder()
    builder.add_edge(_query(1))

    assert builder.node_attrs(TABLE) == {"name": "public.empempenho", "resolved": False}
    assert builder.stats().placeholders == 2


def test_placeholder_becomes_real_node() -> None:
    builder = GraphBuilder()
    builder.add_edge(_query(1))
    builder.add_node(Node(TABLE, NodeLabel.TABLE, {"name": "empempenho", "schema": "public"}))

    assert builder.node_attrs(TABLE) == {"name": "empempenho", "schema": "public", "resolved": True}
    assert builder.stats().placeholders == 1


def test_ensure_node_never_downgrades_real_node() -> None:
    builder = GraphBuilder()
    builder.add_node(Node(TABLE, NodeLabel.TABLE, {"name": "empempenho"}))
    builder.ensure_node(TABLE, NodeLabel.TABLE, name="outro", module_hint="emp")

    assert builder.node_attrs(TABLE) == {
        "name": "empempenho",
        "resolved": True,
        "module_hint": "emp",
    }


def test_real_node_attrs_are_merged() -> None:
    builder = GraphBuilder()
    builder.add_node(Node("Function:db_query", NodeLabel.FUNCTION, {"is_infra": True}))
    builder.add_node(Node("Function:db_query", NodeLabel.FUNCTION, {"path": "libs/db_conn.php"}))

    assert builder.node_attrs("Function:db_query") == {
        "is_infra": True,
        "resolved": True,
        "path": "libs/db_conn.php",
    }


def test_label_must_match_id() -> None:
    builder = GraphBuilder()
    with pytest.raises(GraphContractError):
        builder.add_node(Node("Table:public.x", NodeLabel.CLASS))
    with pytest.raises(GraphContractError):
        builder.ensure_node("Table:public.x", NodeLabel.CLASS)


def test_invalid_edge_is_not_added() -> None:
    builder = GraphBuilder()
    with pytest.raises(GraphContractError):
        builder.add_edge(_query(1, EdgeSource.PHP_AST, 0.5))
    assert len(builder) == 0


def test_stats_and_networkx_attributes() -> None:
    builder = GraphBuilder()
    builder.add_node(Node(SCREEN, NodeLabel.FILE, {"path": "emp4_anularempenho001.php"}))
    builder.add_edge(_query(10))
    builder.add_edge(_query(20))

    stats = builder.stats()
    assert stats.nodes_by_label == {"File": 1, "Table": 1}
    assert stats.edges_by_type == {"QUERIES": 1}

    graph = builder.to_networkx()
    assert graph.nodes[SCREEN]["label"] == "File"
    data = graph.edges[SCREEN, TABLE, "QUERIES"]
    assert data["type"] == "QUERIES"
    assert data["source"] == "SQL_AST"
    assert data["confidence"] == 1.0
    assert data["evidence"] == {"path": "emp4_anularempenho001.php", "line": 10}
    assert data["occurrences"] == 2


def test_remove_edges_from_and_replace() -> None:
    builder = GraphBuilder()
    builder.add_edge(_query(1))
    builder.add_node(Node(SCREEN, NodeLabel.FILE, {"path": "x", "old": 1}))
    assert builder.remove_edges_from(SCREEN) == 1
    assert list(builder.iter_edges()) == []
    builder.add_node(Node(SCREEN, NodeLabel.FILE, {"path": "y"}), replace=True)
    assert builder.node_attrs(SCREEN) == {"path": "y", "resolved": True}


def test_drop_node_keeps_placeholder_when_still_referenced() -> None:
    builder = GraphBuilder()
    builder.add_node(Node(TABLE, NodeLabel.TABLE, {"name": "empempenho"}))
    builder.add_edge(_query(1))
    builder.drop_node(TABLE)
    assert builder.node_attrs(TABLE) == {"name": "public.empempenho", "resolved": False}

    builder.remove_edges_from(SCREEN)
    builder.drop_node(TABLE)
    assert TABLE not in builder
    builder.drop_node("Table:public.inexistente")  # sem erro
