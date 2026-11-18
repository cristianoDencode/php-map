"""A ficha de regras de negócio inclui as regras das `PgFunction` chamadas
(`CALLS_RPC`), com a condição e a função de origem."""

import sqlite3
from pathlib import Path

from php_map.config import Settings
from php_map.graph import routines
from php_map.graph import store as graph_store
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Edge, EdgeSource, EdgeType, Node, NodeLabel
from php_map.ingest.plpgsql_rules import scan_routine
from php_map.search.index import build_documents, write_index
from php_map.web.rules import business_rules

ENTRY = "File:emp4_x.php"
FUNC = "PgFunction:public.fc_calcula(int4)"

DDL = (
    "CREATE FUNCTION public.fc_calcula(a integer) RETURNS void LANGUAGE plpgsql AS $f$\n"
    "BEGIN\n"
    "  -- Confere o saldo antes de calcular\n"
    "  IF a < 0 THEN\n"
    "    RAISE EXCEPTION 'saldo invalido para calculo';\n"
    "  END IF;\n"
    "END;\n"
    "$f$;\n"
)


def _db(tmp_path: Path) -> Path:
    builder = GraphBuilder()
    builder.add_node(Node(ENTRY, NodeLabel.FILE, {"path": "emp4_x.php"}))
    builder.add_node(Node(FUNC, NodeLabel.PG_FUNCTION, {"name": "fc_calcula", "schema": "public"}))
    builder.add_edge(
        Edge(
            ENTRY, FUNC, EdgeType.CALLS_RPC, EdgeSource.SQL_AST, 1.0,
            {"path": "emp4_x.php", "line": 10},
        )
    )  # fmt: skip
    db = tmp_path / "g.db"
    graph_store.save(builder, db, {"source": "x", "scanned_at": "2026-11-04T00:00:00-03:00"})
    docs, stats = build_documents(lambda: iter(()), builder, Settings())
    write_index(db, docs, stats)
    rules, profile = scan_routine(DDL)
    routines.write_rules(db, [(FUNC, rules, profile)])
    return db


def test_called_function_rules_enter_the_messages_and_notes(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        sheet = business_rules(conn, ENTRY)

    [message] = [m for m in sheet.messages if m.scope == FUNC]
    assert "quando: a < 0" in message.text
    assert "saldo invalido para calculo" in message.text
    assert message.file == "public.fc_calcula(int4)"
    assert message.category == "validação"

    [note] = [n for n in sheet.notes if n.scope == FUNC]
    assert note.text == "Confere o saldo antes de calcular"
    assert note.file == "public.fc_calcula(int4)"


def test_no_rule_tables_is_a_graceful_no_op(tmp_path: Path) -> None:
    builder = GraphBuilder()
    builder.add_node(Node(ENTRY, NodeLabel.FILE, {"path": "emp4_x.php"}))
    builder.add_node(Node(FUNC, NodeLabel.PG_FUNCTION, {"name": "fc_calcula", "schema": "public"}))
    builder.add_edge(
        Edge(
            ENTRY, FUNC, EdgeType.CALLS_RPC, EdgeSource.SQL_AST, 1.0,
            {"path": "emp4_x.php", "line": 10},
        )
    )  # fmt: skip
    db = tmp_path / "g.db"
    graph_store.save(builder, db, {"source": "x", "scanned_at": "2026-11-04T00:00:00-03:00"})
    docs, stats = build_documents(lambda: iter(()), builder, Settings())
    write_index(db, docs, stats)

    with sqlite3.connect(db) as conn:
        sheet = business_rules(conn, ENTRY)
    assert sheet.messages == [] and sheet.notes == []
