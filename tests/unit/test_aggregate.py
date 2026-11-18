"""Agregados do mapeamento: `analysis/aggregate.py` sobre um grafo sintético.

Três módulos (`a`, `b`, `c`), um arquivo em dois módulos (`shared.php`, peso 1/2), um sem
módulo, uma lib de infraestrutura e uma DAO (`cl_ta`) do módulo `a`. Os números esperados
foram calculados à mão na docstring de cada teste.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from php_map.analysis.aggregate import (
    NO_MODULE,
    Analytics,
    build_analytics,
    write_analytics,
)
from php_map.config import Settings
from php_map.errors import StoreError
from php_map.graph import store
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    business_action_id,
    class_id,
    file_id,
    method_id,
    module_id,
    table_id,
)

A, B, C = module_id("a"), module_id("b"), module_id("c")
A1, B1, C1 = file_id("aaa1_tela001.php"), file_id("bbb1_tela001.php"), file_id("ccc1_tela001.php")
SHARED, NOMOD = file_id("shared.php"), file_id("nomod.php")
STDLIB, DAO = file_id("libs/db_stdlib.php"), file_id("classes/db_ta_classe.php")
TA, TB = table_id("ta"), table_id("tb")
CLS, METHOD = class_id("cl_ta"), method_id("cl_ta", "incluir")


def _edge(
    builder: GraphBuilder,
    src: str,
    dst: str,
    type_: EdgeType,
    source: EdgeSource = EdgeSource.PHP_AST,
    confidence: float = 1.0,
) -> None:
    evidence = {"path": src.split(":", 1)[1], "line": 7}
    builder.add_edge(Edge(src, dst, type_, source, confidence, evidence))


@pytest.fixture
def graph() -> GraphBuilder:
    builder = GraphBuilder()
    for module, name in ((A, "Módulo A"), (B, "Módulo B"), (C, "Módulo C")):
        builder.add_node(Node(module, NodeLabel.MODULE, {"name": name}))
    for node in (A1, B1, C1, SHARED, NOMOD, DAO):
        builder.add_node(Node(node, NodeLabel.FILE, {"is_infra": False}))
    builder.add_node(Node(STDLIB, NodeLabel.FILE, {"is_infra": True}))
    builder.add_node(Node(CLS, NodeLabel.CLASS, {"is_infra": False}))
    builder.add_node(Node(METHOD, NodeLabel.METHOD, {"is_infra": False}))
    for table in (TA, TB):
        builder.add_node(Node(table, NodeLabel.TABLE, {}))
    catalog, naming = EdgeSource.ECIDADE_CATALOG, EdgeSource.NAMING_RULE
    for src, dst, source, confidence in (
        (A1, A, catalog, 1.0),
        (B1, B, catalog, 1.0),
        (C1, C, catalog, 1.0),
        (SHARED, A, catalog, 1.0),
        (SHARED, B, catalog, 1.0),
        (DAO, A, naming, 0.9),
        (TA, A, catalog, 1.0),
        (TB, B, catalog, 1.0),
    ):
        _edge(builder, src, dst, EdgeType.BELONGS_TO, source, confidence)
    _edge(builder, DAO, CLS, EdgeType.DECLARES)
    _edge(builder, CLS, METHOD, EdgeType.DECLARES)
    action = business_action_id("menu:1")
    builder.add_node(Node(action, NodeLabel.BUSINESS_ACTION, {"name": "Tela A"}))
    _edge(builder, A1, action, EdgeType.IMPLEMENTS_ACTION, catalog)
    # Dependências que contam.
    _edge(builder, A1, B1, EdgeType.REFERENCES)  # a → b
    _edge(builder, A1, C1, EdgeType.INCLUDES)  # a → c
    _edge(builder, B1, C1, EdgeType.REFERENCES)  # b → c
    _edge(builder, SHARED, C1, EdgeType.REFERENCES)  # a → c e b → c, 1/2 cada
    _edge(builder, B1, TA, EdgeType.QUERIES, EdgeSource.SQL_AST)  # b lê tabela de a
    _edge(builder, C1, CLS, EdgeType.INSTANTIATES)  # c → a (a classe é do arquivo da DAO)
    _edge(builder, METHOD, TA, EdgeType.INSERTS, EdgeSource.SQL_AST)  # a → a: não conta
    _edge(builder, NOMOD, A1, EdgeType.REFERENCES)  # (sem módulo) → a
    # Ignoradas: infraestrutura, confiança < 0.5 e tipo fora de TRAVERSED.
    _edge(builder, A1, STDLIB, EdgeType.INCLUDES)
    _edge(builder, STDLIB, B1, EdgeType.REFERENCES)
    _edge(builder, A1, TB, EdgeType.QUERIES, EdgeSource.SQL_REGEX, 0.35)
    _edge(builder, C1, A1, EdgeType.CALLS_DYNAMIC, EdgeSource.HEURISTIC_AI, 0.9)
    return builder


def _analytics(graph: GraphBuilder, **settings: object) -> Analytics:
    return build_analytics(graph, Settings(**settings))  # type: ignore[arg-type]


def test_module_edges_with_weights_and_kinds(graph: GraphBuilder) -> None:
    """a→c: a1→c1 (1) + shared→c1 (1/2); b→c: b1→c1 (1) + shared→c1 (1/2); b→a é leitura."""
    edges = {(e.src, e.dst, e.kind): (e.weight, e.edges) for e in _analytics(graph).module_edges}

    assert edges == {
        (A, B, "code"): (1.0, 1),
        (A, C, "code"): (1.5, 2),
        (B, C, "code"): (1.5, 2),
        (B, A, "table_read"): (1.0, 1),
        (C, A, "code"): (1.0, 1),
        (NO_MODULE, A, "code"): (1.0, 1),
    }


def test_ca_ce_instability_and_weights(graph: GraphBuilder) -> None:
    """Ca/Ce sem contar `(sem módulo)` como vizinho; I = Ce / (Ca + Ce)."""
    rows = {r.module: r for r in _analytics(graph).modules}

    a, b, c, none = rows[A], rows[B], rows[C], rows[NO_MODULE]
    assert (a.ca, a.ce, a.instability, a.weight_in, a.weight_out) == (2, 2, 0.5, 2.0, 2.5)
    assert (b.ca, b.ce, b.instability, b.weight_in, b.weight_out) == (1, 2, 0.6667, 1.0, 2.5)
    assert (c.ca, c.ce, c.instability, c.weight_in, c.weight_out) == (2, 1, 0.3333, 3.0, 1.0)
    assert (none.ca, none.ce, none.instability, none.weight_out) == (0, 1, 1.0, 1.0)
    assert a.name == "Módulo A" and none.name == NO_MODULE
    # Arquivos: a1, shared e a DAO em `a`; shared também em `b`; a lib (infra) fica de fora.
    assert (a.files, b.files, c.files, none.files) == (3, 2, 1, 1)
    assert (a.screens, b.screens, c.screens, a.tables, b.tables) == (1, 1, 1, 1, 1)
    assert (a.shared_tables, b.shared_tables) == (1, 0)


def test_isolated_module_has_no_instability() -> None:
    builder = GraphBuilder()
    builder.add_node(Node(A, NodeLabel.MODULE, {"name": "A"}))
    (row,) = [r for r in build_analytics(builder, Settings()).modules if r.module == A]
    assert (row.ca, row.ce, row.instability) == (0, 0, None)


def test_route_edges_stay_out_and_maps_to_counts_as_code() -> None:
    """`CALLS_HTTP`/`ROUTES_TO` ligam a rota, que não tem módulo, e ficam de fora do
    acoplamento; `MAPS_TO` (model de um módulo → tabela de outro) conta como `code`."""
    builder = GraphBuilder()
    for module in (A, B):
        builder.add_node(Node(module, NodeLabel.MODULE, {"name": module}))
    screen, controller_file = "File:tela.php", "File:app/Controller.php"
    model_file, model = "File:app/Model.php", "Class:app\\model"
    route, method, table = "Route:GET /v4/x", "Method:app\\controller::index", "Table:public.t"
    for node, label in (
        (screen, NodeLabel.FILE),
        (controller_file, NodeLabel.FILE),
        (model_file, NodeLabel.FILE),
        (model, NodeLabel.CLASS),
        (route, NodeLabel.ROUTE),
        (method, NodeLabel.METHOD),
        (table, NodeLabel.TABLE),
    ):
        builder.add_node(Node(node, label, {"is_infra": False}))
    catalog = EdgeSource.ECIDADE_CATALOG
    for src, dst in ((screen, A), (controller_file, B), (model_file, A), (table, B)):
        _edge(builder, src, dst, EdgeType.BELONGS_TO, catalog)
    _edge(builder, model_file, model, EdgeType.DECLARES)
    _edge(builder, controller_file, method, EdgeType.DECLARES)
    _edge(builder, screen, route, EdgeType.CALLS_HTTP, EdgeSource.JS_URL, 0.9)
    _edge(builder, route, method, EdgeType.ROUTES_TO, EdgeSource.LARAVEL_RESOLVER)
    _edge(builder, model, table, EdgeType.MAPS_TO, EdgeSource.ELOQUENT_MODEL)

    edges = {(e.src, e.dst, e.kind): e.edges for e in _analytics(builder).module_edges}

    assert edges == {(A, B, "code"): 1}  # só o MAPS_TO; a ponte tela → rota → controller não conta


def test_shared_tables(graph: GraphBuilder) -> None:
    """`ta` é de `a`, lida por `b` e gravada pela DAO de `a`; `tb` só tem aresta ignorada."""
    tables = {t.table_id: t for t in _analytics(graph).tables}

    ta, tb = tables[TA], tables[TB]
    assert (ta.owner_modules, ta.reader_modules, ta.writer_modules) == ((A,), {B: 1}, {A: 1})
    assert (ta.readers, ta.writers, ta.shared) == (1, 1, True)
    assert (tb.owner_modules, tb.reader_modules, tb.shared) == ((B,), {}, False)


def test_screens(graph: GraphBuilder) -> None:
    """c1 grava `ta` pela classe que instancia; é alcançada por a1, b1 e shared."""
    screens = {s.file_id: s for s in _analytics(graph).screens}

    assert set(screens) == {A1, B1, C1}
    assert (screens[A1].in_menu, screens[A1].menu_items, screens[B1].in_menu) == (True, 1, False)
    assert (screens[C1].tables_written, screens[C1].tables_read) == (1, 0)
    assert (screens[B1].tables_written, screens[B1].tables_read) == (0, 1)
    assert (screens[C1].reached_by, screens[A1].reached_by, screens[B1].reached_by) == (3, 1, 1)
    assert screens[A1].module == A and screens[C1].module == C


def test_hotspots_skip_infra_and_rank_by_label(graph: GraphBuilder) -> None:
    hotspots = _analytics(graph, analytics_hotspots=1).hotspots

    by_label = {h.label: h for h in hotspots}
    assert set(by_label) == {"File", "Table"}  # só labels com arestas de entrada contadas
    assert (by_label["File"].node_id, by_label["File"].fan_in_modules) == (C1, 2)
    assert (by_label["File"].in_degree, by_label["File"].rank) == (3, 1)
    assert (by_label["Table"].node_id, by_label["Table"].fan_in_modules) == (TA, 2)
    assert all(h.node_id != STDLIB for h in _analytics(graph).hotspots)


def test_stats(graph: GraphBuilder) -> None:
    stats = _analytics(graph).stats

    assert (stats.modules, stats.module_edges, stats.tables_shared) == (4, 6, 1)
    assert (stats.screens, stats.screens_in_menu, stats.edges_counted) == (3, 1, 8)


def test_min_confidence_is_configurable(graph: GraphBuilder) -> None:
    edges = {
        (e.src, e.dst, e.kind) for e in _analytics(graph, analytics_min_confidence=0.3).module_edges
    }
    assert (A, B, "table_read") in edges  # a1 → tb (SQL_REGEX 0.35) passa a contar


def test_write_analytics(graph: GraphBuilder, tmp_path: Path) -> None:
    db = tmp_path / "grafo.db"
    store.save(graph, db, {"source": "teste"})
    analytics = _analytics(graph)
    write_analytics(db, analytics)
    write_analytics(db, analytics)  # recria sem duplicar

    with closing(sqlite3.connect(db)) as conn:
        counts = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "analytics_modules",
                "analytics_module_edges",
                "analytics_tables",
                "analytics_hotspots",
                "analytics_screens",
            )
        }
        samples = conn.execute(
            "SELECT samples FROM analytics_module_edges WHERE src = ? AND kind = 'table_read'",
            (B,),
        ).fetchone()[0]
        owners = conn.execute(
            "SELECT owner_modules, shared FROM analytics_tables WHERE table_id = ?", (TA,)
        ).fetchone()
    assert counts == {
        "analytics_modules": 4,
        "analytics_module_edges": 6,
        "analytics_tables": 2,
        "analytics_hotspots": len(analytics.hotspots),
        "analytics_screens": 3,
    }
    assert json.loads(samples) == [
        {"src_node": B1, "dst_node": TA, "type": "QUERIES", "path": "bbb1_tela001.php", "line": 7}
    ]
    assert (json.loads(owners[0]), owners[1]) == ([A], 1)
    assert "analytics_built_at" in store.load_meta(db)


def test_write_analytics_without_db(graph: GraphBuilder, tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="banco do grafo inexistente"):
        write_analytics(tmp_path / "nao-existe.db", _analytics(graph))


def test_local_call_to_a_homonymous_function_is_not_coupling() -> None:
    """`imprimirRodape` é declarada em telas de Escola e de Farmácia, e o
    índice global tem um nó só. A chamada de dentro da tela de Escola é local e não
    pode virar Escola → Farmácia. De um terceiro arquivo, a chamada segue dividida (1/2)."""
    builder = GraphBuilder()
    escola, farmacia, outra = (
        file_id("edu2_x002.php"),
        file_id("far4_y002.php"),
        file_id("pat2_z.php"),
    )
    function = "Function:imprimirrodape"
    for module in (A, B, C):
        builder.add_node(Node(module, NodeLabel.MODULE, {"name": module}))
    for node, module in ((escola, A), (farmacia, B), (outra, C)):
        builder.add_node(Node(node, NodeLabel.FILE, {"is_infra": False}))
        _edge(builder, node, module, EdgeType.BELONGS_TO, EdgeSource.ECIDADE_CATALOG)
    builder.add_node(Node(function, NodeLabel.FUNCTION, {"is_infra": False}))
    _edge(builder, escola, function, EdgeType.DECLARES)
    _edge(builder, farmacia, function, EdgeType.DECLARES)
    _edge(builder, escola, function, EdgeType.CALLS)  # local
    _edge(builder, outra, function, EdgeType.CALLS)  # de fora: ambígua de verdade

    edges = {(e.src, e.dst): e.weight for e in build_analytics(builder, Settings()).module_edges}

    assert (A, B) not in edges
    assert edges == {(C, A): 0.5, (C, B): 0.5}
