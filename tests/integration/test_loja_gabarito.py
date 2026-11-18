"""Gabarito do legado fictício (`loja-mini`): resolver PHP, SQL e catálogo.

O JSONL de referência permite rodar sem Docker. O gabarito está em
`fixtures/expected/loja_edges.json` e `loja_unresolved.json`, escrito à mão a partir do fonte
inventado da fixture."""

import json
from pathlib import Path
from typing import Any

import pytest

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder, StoredEdge
from php_map.ingest.php_extractor import read_cache
from php_map.pipeline import build_graph
from php_map.resolve.unresolved import Unresolved, UnresolvedWriter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REFERENCE = FIXTURES / "ast/loja-mini.jsonl"
DDL = FIXTURES / "schema/loja_mini.sql"
CATALOG = FIXTURES / "catalog/loja_mini.sql"
EXPECTED = FIXTURES / "expected"
ENTRY = "File:ped4_cancelarpedido001.php"


@pytest.fixture(scope="module")
def built() -> tuple[GraphBuilder, list[Unresolved], dict[tuple[str, str, str], StoredEdge]]:
    writer = UnresolvedWriter()
    result = build_graph(
        lambda: read_cache(REFERENCE), [DDL], writer, Settings(boilerplate_min_files=3),
        catalog=[CATALOG],
    )  # fmt: skip
    writer.close()
    edges = {(e.src, e.type.value, e.dst): e for e in result.builder.iter_edges()}
    return result.builder, writer.records, edges


def _expected() -> list[dict[str, Any]]:
    data = json.loads((EXPECTED / "loja_edges.json").read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return data


@pytest.mark.parametrize(
    "expected", _expected(), ids=lambda e: f"{e['group']}:{e['src']}-{e['type']}-{e['dst']}"
)
def test_gabarito_edge(built: Any, expected: dict[str, Any]) -> None:
    _builder, _pending, edges = built
    edge = edges.get((expected["src"], expected["type"], expected["dst"]))
    assert edge is not None, "aresta ausente"
    assert edge.source == expected["source"]
    assert edge.confidence >= expected["min_confidence"] - 1e-9


def test_gabarito_unresolved(built: Any) -> None:
    _builder, pending, _edges = built
    found = {(r.kind, r.path, r.hint) for r in pending}
    expected = json.loads((EXPECTED / "loja_unresolved.json").read_text(encoding="utf-8"))
    missing = [u for u in expected["unresolved"] if (u["kind"], u["path"], u["hint"]) not in found]
    assert missing == []


def test_gabarito_has_the_three_groups() -> None:
    assert {e["group"] for e in _expected()} == {"resolver", "sql", "catalog"}


def test_db_query_tiebreak_by_include_closure(built: Any) -> None:
    _builder, _pending, edges = built
    edge = edges["File:ped4_baixaestoque004.php", "CALLS", "Function:db_query"]
    assert edge.source == "PHP_AST" and edge.confidence == 1.0
    assert edge.evidence["declared_in"] == "libs/db_stdlib.php"


def test_db_query_in_dao_is_single_ambiguous_edge(built: Any) -> None:
    _builder, pending, edges = built
    to_db_query = [
        e
        for (src, _t, dst), e in edges.items()
        if src == "Method:cl_pedido::incluir" and dst == "Function:db_query"
    ]
    assert len(to_db_query) == 1
    (edge,) = to_db_query
    assert edge.source == "PHP_AST_AMBIGUOUS" and edge.confidence == 0.5
    assert edge.evidence["candidates"] == ["libs/db_stdlib.php", "libs/db_stdlibalt.php"]
    assert any(
        r.kind == "ambiguous_function" and r.path == "classes/db_pedido_classe.php" for r in pending
    )


def test_form_method_through_include(built: Any) -> None:
    _builder, _pending, edges = built
    edge = edges["File:forms/db_frmcancelarpedido.php", "CALLS", "Method:cl_pedido::sql_record"]
    assert edge.source == "PHP_AST" and edge.confidence == 1.0
    assert edge.evidence["via_include"] == "ped4_cancelarpedido001.php"


def test_native_functions_have_no_node(built: Any) -> None:
    builder, _pending, _edges = built
    for native in ("Function:strlen", "Function:pg_query", "Function:pg_fetch_all"):
        assert native not in builder
    assert "Class:exception" not in builder


def test_node_attributes(built: Any) -> None:
    builder, _pending, _edges = built
    baixa = builder.node_attrs("File:ped4_baixaestoque004.php")
    assert baixa["dispatch_actions"] == ["cancelar", "getItens", "getEntregas"]
    assert builder.node_attrs("File:ped4_liberarpedidos.RPC.php")["dispatch_actions"] == [
        "pesquisaPedidos",
        "liberarPedidos",
    ]
    assert builder.node_attrs(ENTRY)["module_hint"] == "ped"
    assert builder.node_attrs("Function:db_query")["is_infra"] is True
    assert builder.node_attrs("Function:db_query")["declared_in"] == [
        "libs/db_stdlib.php",
        "libs/db_stdlibalt.php",
    ]
    assert builder.node_attrs("File:lixo_parse_error.php")["parse_status"] in {
        "parse_error",
        "partial",
    }
    assert "Class:pedido" in builder and builder.node_attrs("Class:pedido")["is_infra"] is False


def test_vendor_folder_is_not_scanned(built: Any) -> None:
    builder, _pending, _edges = built
    assert "File:vendor/fake/Ignorar.php" not in builder


def test_table_without_ddl_is_unresolved_and_schema_is_explicit(built: Any) -> None:
    builder, _pending, _edges = built
    assert builder.node_attrs("Table:public.entrega")["resolved"] is False
    assert builder.node_attrs("Table:public.pedido")["resolved"] is True
    assert "Table:varejo.promocao" in builder  # schema explícito


def test_invalid_statement_does_not_stop_the_schema(built: Any) -> None:
    builder, pending, _edges = built
    assert "Table:public.transportadora" in builder
    assert any(r.kind == "sql_parse_error" for r in pending)
