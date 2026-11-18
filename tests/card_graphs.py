"""Banco de teste das fichas de função: uma função comum, um trigger e uma
gerada pela auditoria, com texto, regras, consultas e campos. Compartilhado entre os testes."""

from pathlib import Path

from php_map.graph import routines as routine_store
from php_map.graph import store as graph_store
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Node, NodeLabel
from php_map.ingest.plpgsql_rules import scan_routine
from php_map.ingest.sql_fields import analyze_statement
from php_map.ingest.table_directory import TableDirectory

FUNC = "PgFunction:public.fc_calcula(int4,numeric)"
TABLE = "Table:public.empempenho"
READ_AT = "2026-11-04T10:00:00-03:00"

DDL = (
    "CREATE OR REPLACE FUNCTION public.fc_calcula(a integer, b numeric) RETURNS numeric\n"
    " LANGUAGE plpgsql\nAS $f$\n"
    "BEGIN\n"
    "  -- Confere o saldo antes de calcular o valor final\n"
    "  IF b < 0 THEN\n"
    "    RAISE EXCEPTION 'Saldo % invalido', b;\n"
    "  END IF;\n"
    "  UPDATE empempenho SET e60_vlranu = b WHERE e60_numemp = a;\n"
    "  RETURN b;\n"
    "END;\n"
    "$f$;\n"
)

TRIGGER_DDL = (
    "CREATE OR REPLACE FUNCTION public.fc_trig() RETURNS trigger\n"
    " LANGUAGE plpgsql\nAS $f$\nBEGIN\n  RETURN new;\nEND;\n$f$;\n"
)

GENERATED_DDL = (
    "CREATE OR REPLACE FUNCTION public.fc_auditoria_tabela_abc() RETURNS void\n"
    " LANGUAGE plpgsql\nAS $f$\nBEGIN\n  perform 1;\nEND;\n$f$;\n"
)


def _db(tmp_path: Path, name: str = "g.db") -> Path:
    builder = GraphBuilder()
    builder.add_node(
        Node(
            TABLE,
            NodeLabel.TABLE,
            {
                "name": "empempenho",
                "schema": "public",
                "catalog": {"label": "Empenhos", "description": "Empenhos lançados"},
                "columns_catalog": {"e60_vlranu": {"label": "Valor anulado"}},
            },
        )
    )
    builder.add_node(
        Node(
            FUNC,
            NodeLabel.PG_FUNCTION,
            {
                "name": "fc_calcula",
                "schema": "public",
                "args": ["int4", "numeric"],
                "returns": "numeric",
            },
        )
    )
    builder.add_node(
        Node(
            "PgFunction:public.fc_trig()",
            NodeLabel.PG_FUNCTION,
            {"name": "fc_trig", "schema": "public", "args": [], "returns": "trigger"},
        )
    )
    builder.add_node(
        Node(
            "PgFunction:public.fc_auditoria_tabela_abc()",
            NodeLabel.PG_FUNCTION,
            {"name": "fc_auditoria_tabela_abc", "schema": "public", "args": [], "returns": "void"},
        )
    )
    db = tmp_path / name
    graph_store.save(builder, db, {"source": "x", "scanned_at": "2026-11-04T09:00:00-03:00"})

    rows = [
        routine_store.make_source(FUNC, "public", "fc_calcula", "plpgsql", DDL, READ_AT),
        routine_store.make_source(
            "PgFunction:public.fc_trig()", "public", "fc_trig", "plpgsql", TRIGGER_DDL, READ_AT
        ),
        routine_store.make_source(
            "PgFunction:public.fc_auditoria_tabela_abc()",
            "public",
            "fc_auditoria_tabela_abc",
            "plpgsql",
            GENERATED_DDL,
            READ_AT,
        ),
    ]
    routine_store.write_routines(db, rows)

    rule_results = []
    for node_id, ddl in ((FUNC, DDL), ("PgFunction:public.fc_trig()", TRIGGER_DDL)):
        rules, profile = scan_routine(ddl)
        rule_results.append((node_id, rules, profile))
    routine_store.write_rules(db, rule_results)

    directory = TableDirectory()
    directory.add("public", "empempenho", columns=("e60_numemp", "e60_vlranu"))
    facts = analyze_statement(
        "UPDATE empempenho SET e60_vlranu = b WHERE e60_numemp = a",
        directory=directory,
        variables=("a", "b"),
    )
    routine_store.write_fields(db, [routine_store.QueryRow(FUNC, 0, 9, 9, facts)])
    return db
