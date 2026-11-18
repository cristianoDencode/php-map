"""`routine_rules` e `routine_profile` (storage), gravação e leitura."""

import sqlite3
from pathlib import Path

import pytest

from php_map.errors import QueryError
from php_map.graph import routines
from php_map.ingest.plpgsql_rules import RoutineProfile, Rule, scan_routine

NODE_A = "PgFunction:public.fc_a(int4)"
NODE_B = "PgFunction:public.fc_b()"

DDL_A = (
    "CREATE FUNCTION public.fc_a(a integer) RETURNS void LANGUAGE plpgsql AS $f$\n"
    "BEGIN\n"
    "  IF a < 0 THEN\n"
    "    RAISE EXCEPTION 'saldo invalido';\n"
    "  END IF;\n"
    "  -- Verifica saldo antes de prosseguir\n"
    "END;\n"
    "$f$;\n"
)
DDL_B = (
    "CREATE FUNCTION public.fc_b() RETURNS void LANGUAGE plpgsql AS $f$\n"
    "BEGIN\n"
    "  RAISE WARNING 'saldo zerado';\n"
    "END;\n"
    "$f$;\n"
)


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "g.db"
    sqlite3.connect(db).close()
    return db


def _results() -> list[tuple[str, list[Rule], RoutineProfile]]:
    rules_a, profile_a = scan_routine(DDL_A)
    rules_b, profile_b = scan_routine(DDL_B)
    return [(NODE_A, rules_a, profile_a), (NODE_B, rules_b, profile_b)]


def test_write_and_read_rules_round_trip(tmp_path: Path) -> None:
    db = _db(tmp_path)
    routines.write_rules(db, _results())
    with sqlite3.connect(db) as conn:
        assert routines.has_rules(conn)
        found = routines.read_rules(conn, NODE_A)
        assert found is not None
        kinds = [item["kind"] for item in found["items"]]
        assert "validation" in kinds
        assert "comment" in kinds
        validation = next(i for i in found["items"] if i["kind"] == "validation")
        assert validation["message"] == "saldo invalido"
        assert validation["condition"] == "a < 0"


def test_default_kinds_exclude_diagnostic(tmp_path: Path) -> None:
    db = _db(tmp_path)
    ddl = (
        "CREATE FUNCTION public.fc_c() RETURNS void LANGUAGE plpgsql AS $f$\n"
        "BEGIN\n"
        "  IF true THEN\n"
        "    RAISE NOTICE 'oi';\n"
        "  END IF;\n"
        "END;\n"
        "$f$;\n"
    )
    rules, profile = scan_routine(ddl)
    routines.write_rules(db, [("PgFunction:public.fc_c()", rules, profile)])
    with sqlite3.connect(db) as conn:
        found = routines.read_rules(conn, "PgFunction:public.fc_c()")
        assert found is not None
        assert {i["kind"] for i in found["items"]} == {"decision"}  # diagnostic fica de fora
        everything = routines.read_rules(conn, "PgFunction:public.fc_c()", kinds=())
        assert everything is not None
        assert {i["kind"] for i in everything["items"]} == {"diagnostic", "decision"}


def test_profile_round_trip(tmp_path: Path) -> None:
    db = _db(tmp_path)
    routines.write_rules(db, _results())
    with sqlite3.connect(db) as conn:
        profile = routines.read_profile(conn, NODE_A)
        assert profile is not None
        assert profile["params"] == [{"name": "a", "mode": "IN", "type": "int4", "default": None}]
        assert profile["language"] == "plpgsql"
        assert profile["counts"]["validation"] == 1


def test_search_rules_by_text_and_level(tmp_path: Path) -> None:
    db = _db(tmp_path)
    routines.write_rules(db, _results())
    with sqlite3.connect(db) as conn:
        found = routines.search_rules(conn, query="saldo")
        assert found is not None
        assert found["total"] == 3  # validação, comentário ("saldo") e o aviso de fc_b
        assert {i["routine_id"] for i in found["items"]} == {NODE_A, NODE_B}
        warnings_only = routines.search_rules(conn, level="WARNING")
        assert warnings_only is not None
        assert warnings_only["total"] == 1
        assert warnings_only["items"][0]["routine_id"] == NODE_B


def test_missing_tables_return_none(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        assert routines.has_rules(conn) is False
        assert routines.read_rules(conn, NODE_A) is None
        assert routines.read_profile(conn, NODE_A) is None
        assert routines.search_rules(conn, query="x") is None


# --------------------------------------------------------------------------- assunto


def _write_rule_topics(db: Path, rows: list[tuple[str, int, str, str, str]]) -> None:
    """A mesma tabela que `graph/formula_index.py: build_formula_index` grava (aqui criada à
    mão, sem o módulo inteiro, só para testar o filtro `topic` de `search_rules`)."""
    with sqlite3.connect(db) as conn, conn:
        conn.execute(
            "CREATE TABLE routine_rule_topics (routine_id TEXT NOT NULL, seq INTEGER NOT NULL, "
            "topic TEXT NOT NULL, topic_by TEXT NOT NULL, evidence TEXT NOT NULL, "
            "PRIMARY KEY (routine_id, seq))"
        )
        conn.executemany("INSERT INTO routine_rule_topics VALUES (?, ?, ?, ?, ?)", rows)


def test_search_rules_by_topic_joins_the_derived_table(tmp_path: Path) -> None:
    db = _db(tmp_path)
    routines.write_rules(db, _results())
    with sqlite3.connect(db) as conn:
        rules_a = routines.read_rules(conn, NODE_A, kinds=())
    assert rules_a is not None
    validation_seq = next(i["seq"] for i in rules_a["items"] if i["kind"] == "validation")
    comment_seq = next(i["seq"] for i in rules_a["items"] if i["kind"] == "comment")
    _write_rule_topics(
        db,
        [
            (NODE_A, validation_seq, "outros", "nenhum", ""),
            (NODE_A, comment_seq, "juros", "mensagem", "'juro' na mensagem"),
        ],
    )
    with sqlite3.connect(db) as conn:
        found = routines.search_rules(conn, topic="juros")
        assert found is not None and found["total"] == 1
        item = found["items"][0]
        assert item["routine_id"] == NODE_A and item["topic"] == "juros"
        assert item["kind"] == "comment"

        # "cada item ganha topic": inclusive sem filtrar por assunto, e mesmo sem linha própria
        # (LEFT JOIN: vira `None`, nunca quebra a busca por texto).
        by_text = routines.search_rules(conn, query="saldo")
    assert by_text is not None and by_text["total"] == 3  # validação, comentário e o aviso de B
    topics_found = {item["routine_id"]: item["topic"] for item in by_text["items"]}
    assert topics_found[NODE_B] is None  # fc_b não tem linha em routine_rule_topics


def test_search_rules_topic_without_the_table_raises(tmp_path: Path) -> None:
    db = _db(tmp_path)
    routines.write_rules(db, _results())
    with sqlite3.connect(db) as conn, pytest.raises(QueryError, match="php-map index-formulas"):
        routines.search_rules(conn, topic="juros")
