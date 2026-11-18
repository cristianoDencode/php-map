"""Leitor de schema SQL com DDL sintético em `tmp_path`."""

from pathlib import Path

from php_map.graph.builder import GraphBuilder, StoredEdge
from php_map.graph.model import EdgeType
from php_map.ingest.sql_schema import (
    SchemaStats,
    expand_paths,
    load_schema,
    normalize_escapes,
    read_sql,
    regex_refs,
    split_statements,
)
from php_map.resolve.unresolved import UnresolvedWriter
from php_map.sqlutil import TableOp


def _load(
    tmp_path: Path, sql: str, name: str = "schema.sql"
) -> tuple[GraphBuilder, UnresolvedWriter, SchemaStats]:
    path = tmp_path / name
    path.write_text(sql, encoding="utf-8")
    builder, writer = GraphBuilder(), UnresolvedWriter()
    stats = load_schema([path], builder, writer)
    writer.close()
    return builder, writer, stats


def _edges(builder: GraphBuilder) -> dict[tuple[str, str, str], StoredEdge]:
    return {(e.src, e.type.value, e.dst): e for e in builder.iter_edges()}


def _touch(root: Path, *relative: str) -> None:
    for item in relative:
        path = root / item
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("select 1;", encoding="utf-8")


def test_incremental_order_old_and_new_layouts(tmp_path: Path) -> None:
    db = tmp_path / "db"
    _touch(
        db,
        "v2.3.9/ddl_v2.3.9.sql",
        "v2.3.9/pre_v2.3.9.sql",
        "v2.3.10/pos_v2.3.10.sql",
        "v2.3.10/up/ddl_v2.3.10.sql",
        "v2.3.10/up/pre_v2.3.10.sql",
        "v2.3.10/down/ddl_v2.3.10.sql",
        "DML/carga.sql",
    )
    assert [display for _path, display in expand_paths([db])] == [
        "v2.3.9/pre_v2.3.9.sql",
        "v2.3.9/ddl_v2.3.9.sql",
        "v2.3.10/up/pre_v2.3.10.sql",
        "v2.3.10/up/ddl_v2.3.10.sql",
        "v2.3.10/pos_v2.3.10.sql",
    ]


def test_plain_directory_is_alphabetical_and_file_uses_its_name(tmp_path: Path) -> None:
    _touch(tmp_path / "dump", "b.sql", "a/z.sql", "a.sql")
    assert [d for _p, d in expand_paths([tmp_path / "dump"])] == ["a.sql", "a/z.sql", "b.sql"]
    assert [d for _p, d in expand_paths([tmp_path / "dump/b.sql"])] == ["b.sql"]


def test_latin1_file_and_crlf_are_read(tmp_path: Path) -> None:
    path = tmp_path / "x.sql"
    path.write_bytes("create table ação (id int4);\r\n".encode("latin-1"))
    assert read_sql(path) == "create table ação (id int4);\n"


def test_tables_columns_schema_and_infra(tmp_path: Path) -> None:
    builder, _w, stats = _load(
        tmp_path,
        "CREATE TABLE EmpX (a int4, b text);\n"
        "create table fiscal.y (c int4);\n"
        "create table db_acountx (id int4);",
    )
    attrs = builder.node_attrs("Table:public.empx")
    assert (
        attrs["columns"] == ["a", "b"]
        and attrs["line"] == 1
        and attrs["source_file"] == "schema.sql"
    )
    assert "Table:fiscal.y" in builder
    assert builder.node_attrs("Table:public.db_acountx")["is_infra"] is True
    assert stats.tables == 3


def test_parse_error_is_pending_and_load_continues(tmp_path: Path) -> None:
    builder, writer, stats = _load(
        tmp_path,
        "create table ok1 (id int4);\n"
        "CREATE TABLE quebrada (id int4,, );\n"
        "create table ok2 (id int4);",
    )
    assert "Table:public.ok2" in builder and stats.parse_errors == 1
    (pending,) = writer.records
    assert (pending.kind, pending.path, pending.line, pending.hint) == (
        "sql_parse_error",
        "schema.sql",
        2,
        "quebrada",
    )


FUNCTION = """
create or replace function fc_a(integer, character varying) returns int4 as $$
begin
  insert into t_log values (1);
  perform fc_b();
  return (select count(*) from t_itens);
end;
$$ language plpgsql;
"""


def test_plpgsql_body_edges_and_signature(tmp_path: Path) -> None:
    builder, _w, _s = _load(tmp_path, FUNCTION)
    fid = "PgFunction:public.fc_a(int4,varchar)"
    attrs = builder.node_attrs(fid)
    assert (attrs["language"], attrs["returns"], attrs["args"]) == (
        "plpgsql",
        "int4",
        ["int4", "varchar"],
    )
    edges = _edges(builder)
    assert edges[fid, "INSERTS", "Table:public.t_log"].evidence["line"] == 4
    assert edges[fid, "QUERIES", "Table:public.t_itens"].evidence["line"] == 6
    assert (fid, "CALLS_RPC", "RpcCall:fc_b") in edges  # fc_* fora do schema
    assert builder.node_attrs("Table:public.t_log")["resolved"] is False  # placeholder


def test_create_or_replace_drops_old_edges(tmp_path: Path) -> None:
    second = (
        "create or replace function fc_a(integer, varchar) returns int4 as "
        "$$ begin update t_novo set x = 1; return 1; end; $$ language plpgsql;"
    )
    builder, _w, _s = _load(tmp_path, FUNCTION + second)
    targets = {dst for src, _t, dst in _edges(builder) if src.startswith("PgFunction:public.fc_a")}
    assert targets == {"Table:public.t_novo"}


def test_rpc_overloads_native_functions_and_triggers(tmp_path: Path) -> None:
    sql = """
create function fc_x(int4) returns int4 as $$ select 1 $$ language sql;
create function fc_x(text) returns int4 as $$ select 1 $$ language sql;
create function fc_trg() returns trigger as $$
begin update t set v = now(); return new; end;
$$ language plpgsql;
create function fc_trg(int4) returns int4 as $$ select 1 $$ language sql;
create function fc_chama() returns int4 as $$ select fc_x(1) + coalesce(null, 1) $$ language sql;
create trigger tg after insert on t for each row execute procedure fc_trg();
"""
    builder, _w, stats = _load(tmp_path, sql)
    edges = _edges(builder)
    caller = "PgFunction:public.fc_chama()"
    for overload in ("PgFunction:public.fc_x(int4)", "PgFunction:public.fc_x(text)"):
        edge = edges[caller, "CALLS_RPC", overload]
        assert edge.confidence == 0.5 and len(edge.evidence["candidates"]) == 2
    assert not any("coalesce" in dst for _s, _t, dst in edges)
    assert ("Table:public.t", "TRIGGERS", "PgFunction:public.fc_trg()") in edges
    # Função de trigger: reescrita como RETURNS record para o parse_plpgsql (sem fallback).
    assert stats.plpgsql_fallbacks == 0
    assert edges["PgFunction:public.fc_trg()", "UPDATES", "Table:public.t"].source == "SQL_AST"


def test_drop_table_and_function(tmp_path: Path) -> None:
    sql = """
create table morta (id int4);
create table lida (id int4);
create function fc_le() returns int4 as $$ select count(*)::int4 from lida $$ language sql;
drop table morta;
drop table lida;
drop table nunca_criada;
create function fc_tmp() returns int4 as $$ select 1 $$ language sql;
drop function fc_tmp();
"""
    builder, _w, stats = _load(tmp_path, sql)
    assert "Table:public.morta" not in builder
    assert builder.node_attrs("Table:public.lida")["resolved"] is False  # ainda lida por fc_le
    assert "PgFunction:public.fc_tmp()" not in builder
    assert stats.drops == 3


def test_dynamic_sql_is_pending(tmp_path: Path) -> None:
    sql = (
        "create function fc_d(text) returns void as "
        "$$ begin execute 'delete from ' || $1; end; $$ language plpgsql;"
    )
    _b, writer, stats = _load(tmp_path, sql)
    assert stats.dynamic_sql == 1
    assert [(r.kind, r.hint) for r in writer.records] == [
        ("dynamic_sql", "PgFunction:public.fc_d(text)")
    ]


def test_backslash_quotes_and_executed_ddl(tmp_path: Path) -> None:
    assert normalize_escapes(r"'a\'b', 'c\\', 'd\\\'e'") == r"'a''b', 'c\\', 'd\\''e'"
    sql = (
        "insert into db_syscampo values (1, 'C\\'digo');\n"
        "select fc_executa_ddl('CREATE TABLE via_string (id int4 default nextval(\\'s\\'))');\n"
        "create table depois (id int4);"
    )
    builder, writer, stats = _load(tmp_path, sql)
    assert writer.records == [] and stats.escaped_files == 1
    assert builder.node_attrs("Table:public.via_string")["line"] == 2
    assert "Table:public.depois" in builder


def test_manual_split_respects_dollar_quotes_and_strings() -> None:
    from php_map.ingest.sql_schema import _manual_split

    text = (
        "select ';'; create function f() returns int as $body$ select 1; $body$ language sql;"
        " -- ;\nselect 2"
    )
    parts = [text[a:b].strip() for a, b in _manual_split(text)]
    assert parts[0] == "select ';'"
    assert parts[1].endswith("language sql")
    assert parts[-1].endswith("select 2")
    assert len(split_statements("select 1; select 2")) == 2


def test_regex_refs_fallback() -> None:
    refs = regex_refs(
        "for r in select * from t1 join fiscal.t2 on true loop update t3 set x = 1; delete from t4"
    )
    assert set(refs) == {
        TableOp(EdgeType.QUERIES, "t1"),
        TableOp(EdgeType.QUERIES, "t2", "fiscal"),
        TableOp(EdgeType.UPDATES, "t3"),
        TableOp(EdgeType.DELETES, "t4"),
    }


def test_overloads_are_filtered_by_argument_count(tmp_path: Path) -> None:
    sql = """
create function fc_debug(text) returns text as $$ select 'a' $$ language sql;
create function fc_debug(text, bool) returns text as $$ select 'b' $$ language sql;
create function fc_debug(text, bool, bool, bool) returns text as $$ select 'c' $$ language sql;
create function fc_opt(int4, int4 default 0) returns int4 as $$ select 1 $$ language sql;
create function fc_opt(text) returns int4 as $$ select 1 $$ language sql;
create function fc_var(variadic int4[]) returns int4 as $$ select 1 $$ language sql;
create function fc_var(text, text) returns int4 as $$ select 1 $$ language sql;
create function fc_chama() returns int4 as $$
  select length(fc_debug('')) + fc_opt(1, 2) + fc_var(1, 2, 3)
$$ language sql;
"""
    builder, _w, _s = _load(tmp_path, sql)
    calls = {
        (e.dst, e.confidence)
        for e in builder.iter_edges()
        if e.src == "PgFunction:public.fc_chama()"
    }
    assert calls == {
        ("PgFunction:public.fc_debug(text)", 1.0),
        ("PgFunction:public.fc_opt(int4,int4)", 1.0),
        ("PgFunction:public.fc_var(int4[])", 1.0),
    }


def test_sequences_are_not_tables_and_arity_is_stored(tmp_path: Path) -> None:
    sql = """
create function fc_proximo() returns int8 as $$
  select last_value from empempenho_e60_numemp_seq
$$ language sql;
create function fc_opt(int4, int4 default 0) returns int4 as $$ select 1 $$ language sql;
create function fc_var(text, variadic int4[]) returns int4 as $$ select 1 $$ language sql;
"""
    builder, _w, stats = _load(tmp_path, sql)
    assert not any(node.id.endswith("_seq") for node in builder.iter_nodes())
    assert stats.sequences_dropped == 1
    opt = builder.node_attrs("PgFunction:public.fc_opt(int4,int4)")
    assert (opt["min_args"], opt["max_args"]) == (1, 2)
    var = builder.node_attrs("PgFunction:public.fc_var(text,int4[])")
    assert (var["min_args"], var["max_args"]) == (2, None)
