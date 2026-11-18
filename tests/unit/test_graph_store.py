import sqlite3
import time
from pathlib import Path

import networkx as nx
import pytest

from php_map.errors import StoreError
from php_map.graph import store
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    file_id,
    function_id,
    table_id,
)


def _synthetic(edges: int) -> GraphBuilder:
    """`edges` arestas distintas (módulos primos entre si garantem pares únicos)."""
    builder = GraphBuilder()
    for i in range(1000):
        path = f"módulo/arquivo_{i}.php"  # acento de propósito (JSON com ensure_ascii=False)
        builder.add_node(Node(file_id(path), NodeLabel.FILE, {"path": path, "parse_status": "ok"}))
    for i in range(edges):
        path = f"módulo/arquivo_{i % 1000}.php"
        evidence = {"path": path, "line": i}
        if i % 2:
            dst, type_, source, conf = (
                function_id(f"f{i % 7919}"),
                EdgeType.CALLS,
                EdgeSource.PHP_AST,
                1.0,
            )
        else:
            dst, type_, source, conf = (
                table_id(f"t{i % 997}"),
                EdgeType.QUERIES,
                EdgeSource.SQL_REGEX,
                0.7,
            )
        builder.add_edge(
            Edge(file_id(path), dst, type_, source, conf, evidence, {"via": "db_query"})
        )
    return builder


def _snapshot(graph: "nx.MultiDiGraph[str]") -> tuple[object, object]:
    return dict(graph.nodes(data=True)), sorted(
        (u, v, k, sorted(d.items(), key=str)) for u, v, k, d in graph.edges(keys=True, data=True)
    )


def test_round_trip_preserves_graph(tmp_path: Path) -> None:
    builder = _synthetic(10_000)
    db = tmp_path / "out/graph.db"

    started = time.perf_counter()
    store.save(builder, db, {"source": r"D:\ecidade", "counts": {"files": 1000}})
    loaded = store.load(db)
    elapsed = time.perf_counter() - started

    assert elapsed < 5
    assert loaded.number_of_edges() == builder.stats().edges == 10_000
    assert _snapshot(loaded) == _snapshot(builder.to_networkx())
    assert not db.with_name("graph.db.tmp").exists()


def test_meta_is_saved(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    store.save(_synthetic(10), db, {"source": "D:/e-cidade", "counts": {"files": 3}})

    meta = store.load_meta(db)
    assert meta["schema_version"] == "1"
    assert meta["source"] == "D:/e-cidade"
    assert meta["counts"] == '{"files":3}'
    assert meta["edge_count"] == "10"


def test_save_replaces_previous_database(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    store.save(_synthetic(50), db, {})
    store.save(_synthetic(10), db, {})

    assert store.load(db).number_of_edges() == 10


def test_other_schema_version_asks_for_new_scan(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    store.save(_synthetic(10), db, {})
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    conn.close()

    with pytest.raises(StoreError, match="rode o scan novamente"):
        store.load(db)
    with pytest.raises(StoreError, match="rode o scan novamente"):
        store.load_meta(db)


def test_missing_or_foreign_database(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="inexistente"):
        store.load(tmp_path / "nada.db")

    foreign = tmp_path / "outro.db"
    with sqlite3.connect(foreign) as conn:
        conn.execute("CREATE TABLE x (a)")
    conn.close()
    with pytest.raises(StoreError, match="não é um banco"):
        store.load(foreign)


def test_failed_save_keeps_previous_database(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    store.save(_synthetic(10), db, {})

    with pytest.raises(TypeError):
        store.save(_synthetic(10), db, {"ruim": object()})

    assert store.load(db).number_of_edges() == 10
    assert not db.with_name("g.db.tmp").exists()
