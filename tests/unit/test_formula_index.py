"""Índice de fórmulas e regras por assunto (`graph/formula_index.py`)."""

import json
import sqlite3
from pathlib import Path

import pytest

from php_map.errors import QueryError
from php_map.graph import formula_index, routines, store
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Node, NodeLabel
from php_map.ingest.plpgsql_rules import scan_routine

READ_AT = "2026-11-09T10:00:00-03:00"

FC_A = "PgFunction:public.fc_a(int4)"
FC_B = "PgFunction:public.fc_b()"
FC_BAD = "PgFunction:public.fc_bad()"
TRIBOB = "Table:public.tribob"

# fc_a: uma fórmula por chamada (fc_multa), uma por dicionário (coluna "valx", rótulo com "desc"),
# uma por nome (coluna "descx", o nome já tem "desc") e uma sem assunto algum ("qtd").
FC_A_DDL = """CREATE OR REPLACE FUNCTION public.fc_a(integer)
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
declare
  numpre alias for $1;
  total  numeric;
begin
  total := fc_multa(numpre) + 1;
  update tribob set valx = valx + total where numpre = numpre;
  update tribob set descx = descx + 1 where numpre = numpre;
  update tribob set qtd = qtd + 1 where numpre = numpre;
  return total;
end;
$function$
"""

# fc_b: as duas regras do teste de assunto (por mensagem e por condição).
FC_B_DDL = """CREATE OR REPLACE FUNCTION public.fc_b()
 RETURNS void
 LANGUAGE plpgsql
AS $function$
begin
  IF saldo < 0 AND vljuros IS NULL THEN
    RAISE WARNING 'saldo nao pode ser negativo';
  END IF;
  IF a < 0 THEN
    RAISE EXCEPTION 'valor da multa invalido';
  END IF;
end;
$function$
"""

FC_BAD_DDL = """CREATE OR REPLACE FUNCTION public.fc_bad()
 RETURNS void
 LANGUAGE plpgsql
AS $function$
begin
  perform 1;
end;
$function$
"""


def _builder() -> GraphBuilder:
    b = GraphBuilder()
    for node_id, name in ((FC_A, "fc_a"), (FC_B, "fc_b"), (FC_BAD, "fc_bad")):
        b.add_node(
            Node(node_id, NodeLabel.PG_FUNCTION, {"name": name, "schema": "public", "args": []})
        )
    b.add_node(
        Node(
            TRIBOB,
            NodeLabel.TABLE,
            {
                "name": "tribob",
                "schema": "public",
                "columns": ["numpre", "valx", "descx", "qtd"],
                "columns_catalog": {"valx": {"label": "Valor de desconto aplicado"}},
            },
        )
    )
    return b


def _db(tmp_path: Path, name: str = "formula_index") -> Path:
    path = tmp_path / name / "graph.db"
    store.save(_builder(), path, {"source": "x"})
    return path


def _query_rows(db: Path) -> list[tuple[object, ...]]:
    """As 3 instruções `UPDATE` de `fc_a` (seus alvos de `update_set`) e a linha corrompida de
    `fc_bad` (JSON inválido em `tables`, para o teste de "função que não parseia")."""
    tables = json.dumps([TRIBOB], ensure_ascii=False)
    return [
        (FC_A, 0, "update", 10, 10, "update tribob set valx = valx + total where numpre = numpre", tables, "SQL_AST", 1.0),  # noqa: E501
        (FC_A, 1, "update", 11, 11, "update tribob set descx = descx + 1 where numpre = numpre", tables, "SQL_AST", 1.0),  # noqa: E501
        (FC_A, 2, "update", 12, 12, "update tribob set qtd = qtd + 1 where numpre = numpre", tables, "SQL_AST", 1.0),  # noqa: E501
        (FC_BAD, 0, "select", 1, 1, "x", "nao-e-json", None, None),
    ]  # fmt: skip


def _write_sources_and_rules(db: Path) -> None:
    functions = {FC_A: FC_A_DDL, FC_B: FC_B_DDL, FC_BAD: FC_BAD_DDL}
    rows = [
        routines.make_source(
            node_id, "public", node_id.partition("(")[0].rpartition(".")[2], "plpgsql", ddl, READ_AT
        )
        for node_id, ddl in functions.items()
    ]
    routines.write_routines(db, rows)
    routines.write_rules(db, [(node_id, *scan_routine(ddl)) for node_id, ddl in functions.items()])
    routines.write_fields(db, [])  # só cria o esquema de `routine_queries`/`routine_fields`
    with sqlite3.connect(db) as conn, conn:
        conn.executemany(
            "INSERT INTO routine_queries "
            "(routine_id, seq, kind, line, end_line, text, tables, source, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _query_rows(db),
        )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = _db(tmp_path)
    _write_sources_and_rules(path)
    return path


def test_has_formula_index_before_and_after(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        assert formula_index.has_formula_index(conn) is False
        assert formula_index.rule_topics_available(conn) is False
        stats = formula_index.build_formula_index(conn)
        assert formula_index.has_formula_index(conn) is True
        assert formula_index.rule_topics_available(conn) is True
    assert stats["functions"] == 3


def test_topic_by_call_dictionary_name_and_none(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        formula_index.build_formula_index(conn)
        by_target = {
            row[0]: row
            for row in conn.execute(
                "SELECT target, topic, topic_by, target_column, column_label "
                "FROM routine_formulas WHERE routine_id = ? ORDER BY line",
                (FC_A,),
            )
        }
    assert by_target["total"][1:3] == ("multa", "chamada")  # fc_multa(...)
    assert by_target["tribob.valx"][1:3] == ("desconto", "dicionario")
    assert by_target["tribob.valx"][3:] == ("public.tribob.valx", "Valor de desconto aplicado")
    assert by_target["tribob.descx"][1:3] == ("desconto", "nome")
    assert by_target["tribob.qtd"][1:3] == ("outros", "nenhum")


def test_rule_topic_by_message_then_condition(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        formula_index.build_formula_index(conn)
        rules_found = routines.read_rules(conn, FC_B, kinds=())
        assert rules_found is not None
        topics_by_seq = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT seq, topic, topic_by FROM routine_rule_topics WHERE routine_id = ?", (FC_B,)
            )
        }
    by_message = {item["seq"]: item for item in rules_found["items"] if item["message"]}
    warning_seq = next(s for s, i in by_message.items() if "negativo" in i["message"])
    validation_seq = next(s for s, i in by_message.items() if "multa" in i["message"])
    assert topics_by_seq[warning_seq] == ("juros", "condicao")
    assert topics_by_seq[validation_seq] == ("multa", "mensagem")


def test_function_that_fails_to_parse_counts_as_error(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        stats = formula_index.build_formula_index(conn)
        found = conn.execute(
            "SELECT COUNT(*) FROM routine_formulas WHERE routine_id = ?", (FC_BAD,)
        ).fetchone()[0]
    assert stats["errors"] == 1
    assert found == 0


def test_running_twice_gives_the_same_result(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        first = formula_index.build_formula_index(conn)
        formulas_first = conn.execute(
            "SELECT routine_id, line, target, topic FROM routine_formulas ORDER BY routine_id, line"
        ).fetchall()
        rules_first = conn.execute(
            "SELECT routine_id, seq, topic FROM routine_rule_topics ORDER BY routine_id, seq"
        ).fetchall()
        second = formula_index.build_formula_index(conn)
        formulas_second = conn.execute(
            "SELECT routine_id, line, target, topic FROM routine_formulas ORDER BY routine_id, line"
        ).fetchall()
        rules_second = conn.execute(
            "SELECT routine_id, seq, topic FROM routine_rule_topics ORDER BY routine_id, seq"
        ).fetchall()
    assert {k: v for k, v in first.items() if k != "seconds"} == {
        k: v for k, v in second.items() if k != "seconds"
    }
    assert formulas_first == formulas_second
    assert rules_first == rules_second


def test_build_without_routine_sources_raises(tmp_path: Path) -> None:
    db = _db(tmp_path, "no-sources")
    with sqlite3.connect(db) as conn, pytest.raises(QueryError, match="não tem o texto"):
        formula_index.build_formula_index(conn)


def test_search_formulas_without_the_index_is_none(tmp_path: Path) -> None:
    db = _db(tmp_path, "no-index")
    with sqlite3.connect(db) as conn:
        assert formula_index.search_formulas(conn, topic="juros") is None


def test_search_formulas_by_topic_group_by_routine(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        formula_index.build_formula_index(conn)
        found = formula_index.search_formulas(conn, topic="desconto")
    assert found is not None
    assert found["group_by"] == "routine"
    assert found["total"] == 1
    item = found["items"][0]
    assert item["routine_id"] == FC_A
    assert item["total"] == 2  # tribob.valx (dicionário) + tribob.descx (nome)


def test_search_formulas_by_label_and_group_by_formula(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        formula_index.build_formula_index(conn)
        by_label = formula_index.search_formulas(conn, topic="correção monetária")
        assert by_label is not None and by_label["total"] == 0  # nenhuma fórmula de correção
        found = formula_index.search_formulas(conn, topic="multa", group_by="formula")
    assert found["group_by"] == "formula"
    assert found["total"] == 1
    assert found["items"][0]["target"] == "total"
    assert found["items"][0]["calls"] == ["fc_multa"]


def test_search_formulas_unknown_topic_raises(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        formula_index.build_formula_index(conn)
        with pytest.raises(QueryError, match="assunto desconhecido"):
            formula_index.search_formulas(conn, topic="nao-existe")


def test_search_formulas_by_query_matches_name_expression_and_routine(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        formula_index.build_formula_index(conn)
        by_routine_name = formula_index.search_formulas(conn, query="fc_a", group_by="formula")
        by_target = formula_index.search_formulas(conn, query="descx", group_by="formula")
    assert by_routine_name["total"] == 4  # todas as fórmulas de fc_a
    assert by_target["total"] == 1 and by_target["items"][0]["target"] == "tribob.descx"


def test_search_formulas_module_filter_without_analytics_raises(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        formula_index.build_formula_index(conn)
        with pytest.raises(QueryError, match="analytics_routines"):
            formula_index.search_formulas(conn, topic="multa", module="Module:x")


def test_search_formulas_limits_to_one_routine(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        formula_index.build_formula_index(conn)
        found = formula_index.search_formulas(conn, topic="desconto", routine=FC_B)
    assert found["total"] == 0  # fc_b não tem fórmula nenhuma


def test_render_formula_search_markdown() -> None:
    result = {
        "group_by": "routine",
        "total": 1,
        "truncated": False,
        "next_offset": None,
        "items": [
            {
                "routine_id": FC_A,
                "total": 2,
                "module": None,
                "by_topic": [{"topic": "desconto", "label": "desconto", "count": 2}],
                "examples": ["5: tribob.valx = valx + total"],
            }
        ],
    }
    markdown = formula_index.render_formula_search(result, topic="desconto", query=None)
    assert "fc_a" in markdown and "desconto" in markdown.lower()
    assert formula_index.WARNING in markdown
