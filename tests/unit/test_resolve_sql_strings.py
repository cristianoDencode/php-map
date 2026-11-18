"""SQL embutido no PHP → tabelas e RPCs, com registros e DDL sintéticos."""

from pathlib import Path
from typing import Any

import pytest
from conftest import PhpRecords

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder, StoredEdge
from php_map.ingest.records import PhpFileRecord
from php_map.ingest.sql_schema import load_schema
from php_map.resolve import resolve_php
from php_map.resolve.sql_strings import SqlStats, analyze_embedded, render, resolve_sql
from php_map.resolve.unresolved import UnresolvedWriter

DDL = """
create table empempenho (e60_numemp int4, e60_vlranu float8);
create function fc_debug(text) returns text as $$ select 'a' $$ language sql;
create function fc_debug(text, bool) returns text as $$ select 'b' $$ language sql;
"""
DAO = "classes/db_empempenho_classe.php"


def _sql(text: str, complete: bool = False, scope: str = "", line: int = 10) -> dict[str, Any]:
    return {"text": text, "complete": complete, "via": "db_query", "line": line, "scope": scope}


def _run(
    tmp_path: Path,
    php: PhpRecords,
    *sql: dict[str, Any],
    schema: str | None = DDL,
    dao: bool = False,
) -> tuple[GraphBuilder, UnresolvedWriter, SqlStats]:
    builder, writer, settings = GraphBuilder(), UnresolvedWriter(), Settings()
    if schema is not None:
        path = tmp_path / "schema.sql"
        path.write_text(schema, encoding="utf-8")
        load_schema([path], builder, writer)
    records: list[PhpFileRecord] = [
        php.record(
            "tela001.php",
            sql_strings=list(sql),
            declares=[php.decl("function", "monta")],
        )
    ]
    if dao:
        records.append(php.record(DAO, declares=[php.decl("class", "cl_empempenho")]))
    resolve_php(records, builder, writer, settings)
    stats = resolve_sql(records, builder, writer, settings)
    writer.close()
    return builder, writer, stats


def _sql_edges(builder: GraphBuilder) -> dict[tuple[str, str, str], StoredEdge]:
    types = {"QUERIES", "INSERTS", "UPDATES", "DELETES", "CALLS_RPC"}
    return {(e.src, e.type.value, e.dst): e for e in builder.iter_edges() if e.type.value in types}


# --------------------------------------------------------------------------- placeholders


def test_render_quotes_inside_become_zero_and_outside_follow_attempt() -> None:
    text = "insert into t values ({{VAR}}, '{{VAR}}', 'it''s {{VAR}}', {{VAR}})"
    assert render(text, "__var__") == "insert into t values (__var__, '0', 'it''s 0', __var__)"
    assert render(text, "0") == "insert into t values (0, '0', 'it''s 0', 0)"
    assert render("select '{{VAR}}{{VAR}}'", "'__var__'") == "select '00'"


@pytest.mark.parametrize(
    ("text", "attempt"),
    [
        ("select 1 from empempenho", "direct"),
        ("insert into empempenho values ({{VAR}}, '{{VAR}}')", "number"),
        ("select now() - interval {{VAR}} from empempenho", "string"),
        ("select {{VAR}}{{VAR}} from empempenho {{VAR}} order by {{VAR}}", "identifier"),
        ("select e60_numemp from empempenho where {{VAR}} and", None),
    ],
)
def test_attempts_stop_at_the_first_that_parses(text: str, attempt: str | None) -> None:
    name, refs = analyze_embedded(text)
    assert name == attempt
    assert bool(refs) == (attempt is not None)


# --------------------------------------------------------------------------- arestas AST


def test_ast_edges_scope_infra_and_native_functions(tmp_path: Path, php: PhpRecords) -> None:
    builder, writer, stats = _run(
        tmp_path,
        php,
        _sql("update empempenho set e60_vlranu = {{VAR}}", scope="function:monta"),
        _sql("insert into db_acount values ({{VAR}}, '{{VAR}}')"),
        _sql("select nextval('db_acount_id_acount_seq'), fc_debug('{{VAR}}')", scope=""),
    )
    edges = _sql_edges(builder)
    update = edges[("Function:monta", "UPDATES", "Table:public.empempenho")]
    assert (update.source, update.confidence) == ("SQL_AST", 1.0)
    assert ("File:tela001.php", "INSERTS", "Table:public.db_acount") in edges
    assert builder.node_attrs("Table:public.db_acount")["is_infra"] is True
    # Sobrecarga decidida pela aridade (o schema grava min_args/max_args); `nextval` é nativa.
    rpc = edges[("File:tela001.php", "CALLS_RPC", "PgFunction:public.fc_debug(text)")]
    assert rpc.confidence == 1.0
    assert builder.node_attrs("PgFunction:public.fc_debug(text)")["is_infra"] is True
    assert not any("nextval" in dst for _s, _t, dst in edges)
    assert stats.parsed == {"number": 3}
    assert writer.counts["dynamic_sql"] == 0


def test_without_schema_fc_goes_to_rpccall(tmp_path: Path, php: PhpRecords) -> None:
    builder, _w, _s = _run(tmp_path, php, _sql("select fc_debug('{{VAR}}')"), schema=None)
    edge = _sql_edges(builder)[("File:tela001.php", "CALLS_RPC", "RpcCall:fc_debug")]
    assert (edge.source, edge.confidence) == ("SQL_AST", 1.0)
    assert builder.node_attrs("RpcCall:fc_debug")["is_infra"] is True


# --------------------------------------------------------------------------- fallback validado

FRAGMENT = "select e60_numemp from empempenho where {{VAR}} and"


def test_fragment_validated_by_schema(tmp_path: Path, php: PhpRecords) -> None:
    builder, writer, stats = _run(tmp_path, php, _sql(FRAGMENT))
    edge = _sql_edges(builder)[("File:tela001.php", "QUERIES", "Table:public.empempenho")]
    assert (edge.source, edge.confidence) == ("SQL_REGEX", 0.7)
    assert edge.evidence["validated"] == "schema"
    assert [(r.kind, r.path, r.line) for r in writer.records] == [
        ("dynamic_sql", "tela001.php", 10)
    ]
    assert (stats.regex_fallbacks, stats.validated_schema) == (1, 1)


def test_fragment_validated_by_dao_or_not_validated(tmp_path: Path, php: PhpRecords) -> None:
    fragments = (_sql(FRAGMENT), _sql(FRAGMENT.replace("empempenho", "tabela_inexistente")))
    builder, _w, stats = _run(tmp_path, php, *fragments, schema=None, dao=True)
    edges = _sql_edges(builder)
    dao = edges[("File:tela001.php", "QUERIES", "Table:public.empempenho")]
    assert (dao.confidence, dao.evidence["validated"]) == (0.7, "dao")
    unknown = edges[("File:tela001.php", "QUERIES", "Table:public.tabela_inexistente")]
    assert (unknown.confidence, unknown.evidence["validated"]) == (0.5, False)
    assert (stats.validated_dao, stats.not_validated) == (1, 1)


def test_dao_placeholder_does_not_validate(tmp_path: Path, php: PhpRecords) -> None:
    """`new cl_x` sem a classe declarada cria um placeholder: não prova que a tabela existe."""
    builder, writer, settings = GraphBuilder(), UnresolvedWriter(), Settings()
    builder.ensure_node("Class:cl_empempenho")
    record = php.record("t.php", sql_strings=[_sql(FRAGMENT)])
    resolve_sql([record], builder, writer, settings)
    edge = _sql_edges(builder)[("File:t.php", "QUERIES", "Table:public.empempenho")]
    assert edge.confidence == 0.5


def test_complete_fragment_has_no_pending_and_regex_rpc(tmp_path: Path, php: PhpRecords) -> None:
    builder, writer, _s = _run(
        tmp_path, php, _sql("select fc_calcula(1 from empempenho", complete=True), schema=None
    )
    edges = _sql_edges(builder)
    rpc = edges[("File:tela001.php", "CALLS_RPC", "RpcCall:fc_calcula")]
    assert (rpc.source, rpc.confidence) == ("SQL_REGEX", 0.7)
    assert writer.counts["dynamic_sql"] == 0


# --------------------------------------------------------------------------- nomes dinâmicos e seq


def test_dynamic_table_name_creates_no_node(tmp_path: Path, php: PhpRecords) -> None:
    builder, writer, stats = _run(
        tmp_path,
        php,
        _sql("select * from {{VAR}} where x = 1"),
        _sql("select * from emp{{VAR}} where"),
    )
    assert not any("__var__" in n.id or n.id == "Table:public.emp" for n in builder.iter_nodes())
    assert stats.dynamic_tables == 2
    assert writer.counts["dynamic_sql"] == 2


def test_sequences_are_not_tables(tmp_path: Path, php: PhpRecords) -> None:
    builder, _w, stats = _run(
        tmp_path,
        php,
        _sql("select last_value from empempenho_e60_numemp_seq", complete=True),
        _sql("select last_value from empempenho_e60_numemp_seq where {{VAR}} and"),
    )
    assert not any(n.id.endswith("_seq") for n in builder.iter_nodes())
    assert stats.sequences_dropped == 2
