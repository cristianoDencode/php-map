"""`routine_queries`/`routine_fields` (storage), gravação e leitura."""

import sqlite3
from pathlib import Path

from php_map.graph import routines
from php_map.ingest.sql_fields import analyze_statement
from php_map.ingest.table_directory import TableDirectory

NODE_A = "PgFunction:public.fc_a(int4)"
T1 = "Table:public.empempenho"
T2 = "Table:public.empanulado"


def _directory() -> TableDirectory:
    d = TableDirectory()
    d.add("public", "empempenho", columns=("e60_numemp", "e60_vlranu"))
    d.add("public", "empanulado", columns=("e94_codanu", "e94_numemp"))
    return d


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "g.db"
    sqlite3.connect(db).close()
    return db


def _rows() -> list[routines.QueryRow]:
    directory = _directory()
    facts_a = analyze_statement(
        "select e60_numemp from empempenho where e60_vlranu > 1", directory=directory
    )
    facts_b = analyze_statement(
        "update empanulado set e94_numemp = 1 where e94_codanu = 2", directory=directory
    )
    return [
        routines.QueryRow(NODE_A, 0, 10, 10, facts_a),
        routines.QueryRow(NODE_A, 1, 20, 20, facts_b),
    ]


def test_write_and_read_queries_round_trip(tmp_path: Path) -> None:
    db = _db(tmp_path)
    routines.write_fields(db, _rows())
    with sqlite3.connect(db) as conn:
        assert routines.has_fields(conn)
        found = routines.read_queries(conn, NODE_A)
        assert found is not None
        assert found["total"] == 2
        first = found["items"][0]
        assert first["kind"] == "select" and first["tables"] == [T1]
        field_cols = {(f["table"], f["column"], f["role"]) for f in first["fields"]}
        assert field_cols == {(T1, "e60_numemp", "read"), (T1, "e60_vlranu", "filter")}
        second = found["items"][1]
        assert second["kind"] == "update"
        field_cols2 = {(f["table"], f["column"], f["role"]) for f in second["fields"]}
        assert field_cols2 == {(T2, "e94_numemp", "write"), (T2, "e94_codanu", "filter")}


def test_column_usage_finds_the_right_function_and_role(tmp_path: Path) -> None:
    db = _db(tmp_path)
    routines.write_fields(db, _rows())
    with sqlite3.connect(db) as conn:
        found = routines.column_usage(conn, T2, "e94_numemp", role="write")
        assert found is not None
        assert found["total"] == 1
        assert found["items"][0]["routine_id"] == NODE_A
        assert "update empanulado" in found["items"][0]["text"]
        none_found = routines.column_usage(conn, T2, "e94_numemp", role="read")
        assert none_found is not None and none_found["total"] == 0


def test_column_usage_unknown_column_is_empty_not_error(tmp_path: Path) -> None:
    db = _db(tmp_path)
    routines.write_fields(db, _rows())
    with sqlite3.connect(db) as conn:
        found = routines.column_usage(conn, T1, "nao_existe")
        assert found is not None and found["total"] == 0


def test_missing_tables_return_none(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        assert routines.has_fields(conn) is False
        assert routines.read_queries(conn, NODE_A) is None
        assert routines.column_usage(conn, T1, "e60_numemp") is None
