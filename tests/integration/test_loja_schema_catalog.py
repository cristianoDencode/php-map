"""Schema (DDL) e catálogo (dicionário + menus) do legado fictício `loja-mini`."""

from collections import Counter
from pathlib import Path

import pytest

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder, StoredEdge
from php_map.ingest.catalog_sql import load_catalog
from php_map.ingest.php_extractor import read_cache
from php_map.ingest.sql_schema import SchemaStats, load_schema
from php_map.pipeline import build_graph
from php_map.resolve.catalog import CatalogStats, apply_catalog
from php_map.resolve.unresolved import Unresolved, UnresolvedWriter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REFERENCE = FIXTURES / "ast/loja-mini.jsonl"
DDL = FIXTURES / "schema/loja_mini.sql"
CATALOG = FIXTURES / "catalog/loja_mini.sql"
FUNCTION = "PgFunction:public.fc_saldo_estoque(int4,numeric)"

Loaded = tuple[GraphBuilder, list[Unresolved], SchemaStats, dict[tuple[str, str, str], StoredEdge]]
Applied = tuple[GraphBuilder, CatalogStats, Counter[str], Counter[str]]


@pytest.fixture(scope="module")
def loaded() -> Loaded:
    builder, writer = GraphBuilder(), UnresolvedWriter()
    stats = load_schema([DDL], builder, writer)
    writer.close()
    edges = {(e.src, e.type.value, e.dst): e for e in builder.iter_edges()}
    return builder, writer.records, stats, edges


@pytest.fixture(scope="module")
def applied() -> Applied:
    """Monta o grafo sem catálogo, conta os nós e só então aplica o catálogo."""
    writer = UnresolvedWriter()
    builder = build_graph(lambda: read_cache(REFERENCE), [DDL], writer, Settings()).builder
    writer.close()
    before = Counter(node.label.value for node in builder.iter_nodes())
    stats = apply_catalog(load_catalog([CATALOG]), builder)
    after = Counter(node.label.value for node in builder.iter_nodes())
    return builder, stats, before, after


def _edges(builder: GraphBuilder) -> dict[tuple[str, str, str], StoredEdge]:
    return {(e.src, e.type.value, e.dst): e for e in builder.iter_edges()}


# --------------------------------------------------------------------------- schema


def test_tables_functions_and_triggers_are_loaded(loaded: Loaded) -> None:
    builder, _pending, stats, _edges_ = loaded
    for node_id in (
        "Table:public.pedido",
        "Table:public.itempedido",
        "Table:varejo.promocao",
        FUNCTION,
        "PgFunction:public.tg_pedido_auditoria()",
    ):
        assert builder.node_attrs(node_id)["resolved"] is True, node_id
    assert (stats.tables, stats.functions, stats.triggers) == (10, 2, 1)


def test_function_and_trigger_edges(loaded: Loaded) -> None:
    edges = loaded[3]
    assert edges[FUNCTION, "QUERIES", "Table:public.estoque"].source == "SQL_AST"
    assert (FUNCTION, "INSERTS", "Table:public.estoque") in edges
    assert (FUNCTION, "QUERIES", "Table:public.parametroloja") in edges
    assert ("Table:public.pedido", "TRIGGERS", "PgFunction:public.tg_pedido_auditoria()") in edges
    assert (
        "PgFunction:public.tg_pedido_auditoria()",
        "INSERTS",
        "Table:public.auditoria_loja",
    ) in (edges)


def test_invalid_statement_is_pending_and_load_continues(loaded: Loaded) -> None:
    builder, pending, stats, _edges_ = loaded
    assert [(r.kind, r.path) for r in pending] == [("sql_parse_error", "loja_mini.sql")]
    assert "Table:public.transportadora" in builder  # vem depois da instrução quebrada
    assert stats.parse_errors == 1


def test_evidence_points_to_the_fixture_lines(loaded: Loaded) -> None:
    edges = loaded[3]
    assert edges[FUNCTION, "QUERIES", "Table:public.parametroloja"].evidence == {
        "path": "loja_mini.sql",
        "line": 81,
    }
    assert edges[FUNCTION, "INSERTS", "Table:public.estoque"].evidence["line"] == 96


# --------------------------------------------------------------------------- catálogo


def test_menu_path_and_module_edges(applied: Applied) -> None:
    builder = applied[0]
    edges = _edges(builder)
    assert ("Table:public.pedido", "BELONGS_TO", "Module:pedidos") in edges
    assert ("File:ped4_cancelarpedido001.php", "BELONGS_TO", "Module:pedidos") in edges
    actions = [
        dst
        for src, type_, dst in edges
        if src == "File:ped4_cancelarpedido001.php" and type_ == "IMPLEMENTS_ACTION"
    ]
    assert actions == ["BusinessAction:menu:7001"]  # o item 7005 foi apagado
    attrs = builder.node_attrs(actions[0])
    assert attrs["menu_path"] == "Pedidos > Procedimentos > Cancelar Pedido"


def test_table_catalog_attrs(applied: Applied) -> None:
    attrs = applied[0].node_attrs("Table:public.pedido")
    assert attrs["resolved"] is True  # continua sendo a tabela real do schema
    assert attrs["catalog"]["prefix"] == "ped"
    assert attrs["columns_catalog"]["ped01_situacao"]["label"] == "Situação"
    assert applied[0].node_attrs("Table:public.pedidocancelado")["catalog"]["prefix"] == "can"


def test_only_module_and_business_action_nodes_are_created(applied: Applied) -> None:
    _builder, _stats, before, after = applied
    assert after - before == Counter({"Module": 2, "BusinessAction": 3})
    assert before["Module"] == before["BusinessAction"] == 0


def test_catalog_stats(applied: Applied) -> None:
    stats = applied[1]
    assert stats.tables_unreferenced == 1  # devolucao: só existe no dicionário
    assert stats.tables_with_module == 5
    assert stats.menu_missing_files == 1
    assert stats.menu_missing_examples == ["7004: est1_transportadora001.php"]
    assert stats.modules_merged == 2  # pedidos/Pedidos e estoque/Estoque
    assert stats.edges["IMPLEMENTS_ACTION"] == 3


def test_inactive_item_keeps_confidence(applied: Applied) -> None:
    builder = applied[0]
    assert builder.node_attrs("BusinessAction:menu:7002")["active"] is False
    edge = _edges(builder)[
        ("File:ped4_baixaestoque004.php", "IMPLEMENTS_ACTION", "BusinessAction:menu:7002")
    ]
    assert edge.confidence == 1.0


def test_menu_item_in_two_modules_gives_two_memberships(applied: Applied) -> None:
    edges = _edges(applied[0])
    assert ("File:func_pedido.php", "BELONGS_TO", "Module:pedidos") in edges
    assert ("File:func_pedido.php", "BELONGS_TO", "Module:estoque") in edges
