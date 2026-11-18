"""`routine_sources` (texto das funções da base) e a ligação do texto ao nó."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from php_map.errors import StoreError
from php_map.graph import routines
from php_map.graph import store as graph_store
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Edge, EdgeSource, EdgeType
from php_map.ingest.schema_db import DbRoutine, SchemaSnapshot
from php_map.ingest.sql_schema import load_schema_snapshot
from php_map.resolve.unresolved import UnresolvedWriter

READ_AT = "2026-11-03T10:00:00-03:00"


def _src(
    node_id: str, schema: str, name: str, ddl: str = "CREATE FUNCTION x()\n"
) -> routines.RoutineSource:
    return routines.make_source(node_id, schema, name, "plpgsql", ddl, READ_AT)


def _db(tmp_path: Path, rows: list[routines.RoutineSource]) -> Path:
    db = tmp_path / "g.db"
    sqlite3.connect(db).close()
    routines.write_routines(db, rows)
    return db


def _rows() -> list[routines.RoutineSource]:
    return [
        _src("PgFunction:public.lpad(float8,int4)", "public", "lpad", "CREATE FUNCTION a\nb\n"),
        _src("PgFunction:public.lpad(varchar,int4,int4)", "public", "lpad", "CREATE FUNCTION c\n"),
        _src("PgFunction:public.fc_calcula(int4)", "public", "fc_calcula", "CREATE FUNCTION d\n"),
        _src("PgFunction:configuracoes.fc_executa_ddl(text)", "configuracoes", "fc_executa_ddl"),
        _src("PgFunction:public.fc_executa_ddl(text)", "public", "fc_executa_ddl"),
        _src(
            "PgFunction:almoxarifado.fc_auditoria_tabela_12746eb7()",
            "almoxarifado",
            "fc_auditoria_tabela_12746eb7",
        ),
    ]


def test_round_trip_keeps_the_original_text(tmp_path: Path) -> None:
    ddl = "CREATE OR REPLACE FUNCTION public.f()\n AS $f$ select 'a\\'b' $f$\n"
    db = _db(tmp_path, [_src("PgFunction:public.f()", "public", "f", ddl)])
    with closing(sqlite3.connect(db)) as conn:
        row = routines.read_routine(conn, "PgFunction:public.f()")
    assert row is not None
    assert row.ddl == ddl  # sem "consertar" escapes nem acentos
    assert row.line_count == 2 and row.language == "plpgsql" and row.read_at == READ_AT
    assert len(row.sha1) == 40 and row.generated is False


def test_generated_flag_and_sha1() -> None:
    audit = _src("PgFunction:x.fc_auditoria_tabela_ab()", "x", "fc_auditoria_tabela_ab")
    other = _src("PgFunction:x.fc_a()", "x", "fc_a", "outro texto")
    assert audit.generated and not other.generated
    assert audit.sha1 != other.sha1


def test_overloads_keep_different_texts(tmp_path: Path) -> None:
    with closing(sqlite3.connect(_db(tmp_path, _rows()))) as conn:
        found = routines.find_routines(conn, "lpad")
    assert [r.node_id for r in found] == [
        "PgFunction:public.lpad(float8,int4)",
        "PgFunction:public.lpad(varchar,int4,int4)",
    ]
    assert found[0].ddl != found[1].ddl


def test_find_by_id_schema_name_and_name(tmp_path: Path) -> None:
    with closing(sqlite3.connect(_db(tmp_path, _rows()))) as conn:
        by_id = routines.find_routines(conn, "PgFunction:public.fc_calcula(int4)")
        by_signature = routines.find_routines(conn, "public.fc_calcula(int4)")
        by_schema = routines.find_routines(conn, "public.fc_calcula")
        by_name = routines.find_routines(conn, "FC_CALCULA")
        ambiguous = routines.find_routines(conn, "fc_executa_ddl")
        qualified = routines.find_routines(conn, "configuracoes.fc_executa_ddl")
        missing = routines.find_routines(conn, "nao_existe")
    assert {r.node_id for r in (*by_id, *by_signature, *by_schema, *by_name)} == {
        "PgFunction:public.fc_calcula(int4)"
    }
    assert len(by_id) == len(by_signature) == len(by_schema) == len(by_name) == 1
    assert len(ambiguous) == 2 and len(qualified) == 1 and missing == []


def test_suggest_names_by_prefix(tmp_path: Path) -> None:
    with closing(sqlite3.connect(_db(tmp_path, _rows()))) as conn:
        assert routines.suggest_names(conn, "fc_exe") == [
            "configuracoes.fc_executa_ddl",
            "public.fc_executa_ddl",
        ]
        assert routines.suggest_names(conn, "fc_%") == []  # `%` não é curinga


def test_database_without_the_table_is_tolerated(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    sqlite3.connect(db).close()
    with closing(sqlite3.connect(db)) as conn:
        assert routines.has_routines(conn) is False
        assert routines.read_routine(conn, "PgFunction:public.f()") is None
        assert routines.find_routines(conn, "f") == []
        assert routines.suggest_names(conn, "f") == []
        assert routines.count_routines(conn) == 0


def test_write_replaces_the_previous_table_and_needs_the_database(tmp_path: Path) -> None:
    db = _db(tmp_path, _rows())
    routines.write_routines(db, _rows()[:2])
    with closing(sqlite3.connect(db)) as conn:
        assert routines.count_routines(conn) == 2
    with pytest.raises(StoreError, match="inexistente"):
        routines.write_routines(tmp_path / "nao.db", [])


# --------------------------------------------------------------------------- nó ↔ texto


def _snapshot(*functions: DbRoutine) -> SchemaSnapshot:
    return SchemaSnapshot(source="db:x/y", search_path="public", functions=list(functions))


def _fn(name: str, args: str, body: str = "select 1") -> DbRoutine:
    ddl = (
        f"CREATE OR REPLACE FUNCTION public.{name}({args})\n RETURNS integer\n"
        f" LANGUAGE sql\nAS $function$ {body} $function$\n"
    )
    return DbRoutine("public", name, ddl)


def test_loader_ties_each_text_to_its_overload() -> None:
    builder = GraphBuilder()
    captured: list[tuple[str, DbRoutine]] = []
    snapshot = _snapshot(
        _fn("lpad", "integer, integer", "select 1"),
        _fn("lpad", "character varying, integer, integer", "select 2"),
    )
    stats = load_schema_snapshot(snapshot, builder, UnresolvedWriter(None), None, captured)
    ids = [node_id for node_id, _ in captured]
    assert ids == ["PgFunction:public.lpad(int4,int4)", "PgFunction:public.lpad(varchar,int4,int4)"]
    assert "select 1" in captured[0][1].ddl and "select 2" in captured[1][1].ddl
    assert stats.routines_without_node == []


def test_function_whose_text_makes_no_node_is_counted() -> None:
    builder = GraphBuilder()
    captured: list[tuple[str, DbRoutine]] = []
    broken = DbRoutine("public", "quebrada", "CREATE FUNCTION public.quebrada( ((( \n")
    stats = load_schema_snapshot(
        _snapshot(_fn("ok", ""), broken), builder, UnresolvedWriter(None), None, captured
    )
    assert [i for i, _ in captured] == ["PgFunction:public.ok()"]
    assert stats.routines_without_node == ["public.quebrada"]


# --------------------------------------------------------------------------- busca


def test_function_body_is_what_is_between_the_dollar_tags() -> None:
    ddl = (
        "CREATE FUNCTION f()\n RETURNS int\n LANGUAGE sql\nAS $function$ select juros $function$\n"
    )
    assert routines.function_body(ddl).strip() == "select juros"
    assert routines.function_body("sem corpo marcado") == "sem corpo marcado"
    custom = "CREATE FUNCTION f() AS $x$ a $y$ b $x$"
    assert routines.function_body(custom).strip() == "a $y$ b"


def test_function_body_enters_the_search_index(tmp_path: Path) -> None:
    from php_map.config import Settings
    from php_map.graph import store
    from php_map.graph.model import Node, NodeLabel
    from php_map.search import query
    from php_map.search.index import build_documents, write_index

    builder = GraphBuilder()
    node_id = "PgFunction:public.fc_calcula(int4)"
    builder.add_node(
        Node(
            node_id,
            NodeLabel.PG_FUNCTION,
            {"name": "fc_calcula", "schema": "public", "language": "plpgsql"},
        )
    )
    builder.add_node(
        Node("PgFunction:public.sem_texto()", NodeLabel.PG_FUNCTION, {"name": "sem_texto"})
    )
    body = "begin update iptucalc set j23_vlrter = 1; end"
    ddl = f"CREATE FUNCTION public.fc_calcula(int)\n AS $f$ {body} $f$\n"
    db = tmp_path / "g.db"
    store.save(builder, db, {"source": "x", "scanned_at": READ_AT})
    docs, stats = build_documents(lambda: iter(()), builder, Settings(), {node_id: ddl})
    write_index(db, docs, stats)

    assert stats.by_label == {"PgFunction": 1}  # só a que tem texto vira documento
    hits = query.search(db, "iptucalc")  # palavra que só existe no corpo
    assert [h.node_id for h in hits] == [node_id]
    assert hits[0].field == "text" and hits[0].label == "PgFunction"
    assert [h.node_id for h in query.search(db, "fc_calcula", label="PgFunction")] == [node_id]


def test_function_messages_and_comments_enter_search_texts(tmp_path: Path) -> None:
    """`validation`/`error`/`warning` e `comment` da função entram em `search_texts`
    com `scope` = o próprio `PgFunction` (a ficha de regras de negócio lê por aí)."""
    import sqlite3

    from php_map.config import Settings
    from php_map.graph import store
    from php_map.graph.model import Node, NodeLabel
    from php_map.ingest.plpgsql_rules import scan_routine
    from php_map.search.index import build_documents, write_index

    node_id = "PgFunction:public.fc_calcula(int4)"
    builder = GraphBuilder()
    builder.add_node(
        Node(node_id, NodeLabel.PG_FUNCTION, {"name": "fc_calcula", "schema": "public"})
    )
    ddl = (
        "CREATE FUNCTION public.fc_calcula(a integer) RETURNS void LANGUAGE plpgsql AS $f$\n"
        "BEGIN\n"
        "  -- Verifica se o saldo esta correto\n"
        "  IF a < 0 THEN\n"
        "    RAISE EXCEPTION 'saldo invalido para calculo';\n"
        "  END IF;\n"
        "END;\n"
        "$f$;\n"
    )
    rules, _profile = scan_routine(ddl)
    db = tmp_path / "g.db"
    store.save(builder, db, {"source": "x", "scanned_at": READ_AT})
    docs, stats = build_documents(
        lambda: iter(()), builder, Settings(), {node_id: ddl}, {node_id: rules}
    )
    write_index(db, docs, stats)

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT kind, text, scope FROM search_texts WHERE file_id = ? ORDER BY line", (node_id,)
        ).fetchall()
    assert rows == [
        ("comment", "Verifica se o saldo esta correto", node_id),
        ("literal", "saldo invalido para calculo", node_id),
    ]


# --------------------------------------------------------------------------- sondagem da base


def test_check_reachable_masks_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    from php_map.errors import ConfigError
    from php_map.ingest import catalog_db, schema_db

    def refuse(dsn: str) -> None:
        raise psycopg.OperationalError("falha com password=segredo123 no host")

    monkeypatch.setattr(catalog_db, "_connect", refuse)
    dsn = "host=db.exemplo dbname=ecidade user=u password=segredo123"
    with pytest.raises(
        ConfigError, match=r"não foi possível conectar em db:db\.exemplo/ecidade"
    ) as raised:
        schema_db.check_reachable(dsn)
    assert "segredo123" not in str(raised.value) and "***" in str(raised.value)


# --------------------------------------------------------------------------- repasse

FAMILY_SCHEMA = "public"
FAMILY_NAME = "fc_fam"


def _member(args: str) -> str:
    return f"PgFunction:{FAMILY_SCHEMA}.{FAMILY_NAME}({args})"


def _family_db(
    tmp_path: Path, members: list[str], edges: list[tuple[str, str]], name: str = "fam"
) -> Path:
    """Um grafo mínimo: `members` (mesma família, `public.fc_fam`) e `edges` (`CALLS_RPC`
    internas, confiança 1.0, como o scan real grava)."""
    builder = GraphBuilder()
    for src, dst in edges:
        builder.add_edge(
            Edge(src, dst, EdgeType.CALLS_RPC, EdgeSource.SQL_AST, 1.0, {"path": "x", "line": 1})
        )
    db = tmp_path / name / "g.db"
    graph_store.save(builder, db, {"source": "x", "scanned_at": READ_AT})
    rows = [
        routines.make_source(
            m, FAMILY_SCHEMA, FAMILY_NAME, "plpgsql", f"CREATE FUNCTION {m}\n", READ_AT
        )
        for m in members
    ]
    routines.write_routines(db, rows)
    return db


def test_resolve_family_follows_a_chain_of_delegation(tmp_path: Path) -> None:
    m1, m2, m3 = _member("int4"), _member("int4,int4"), _member("int4,int4,int4")
    db = _family_db(tmp_path, [m1, m2, m3], [(m1, m2), (m2, m3)])
    with closing(sqlite3.connect(db)) as conn:
        resolution = routines.resolve_family(conn, FAMILY_NAME)
    assert resolution.resolved_by == "repasse"
    assert resolution.chosen is not None and resolution.chosen.node_id == m3
    assert resolution.chain == (m1, m2, m3)
    assert resolution.delegates == {m1: m2, m2: m3}
    assert {c.node_id for c in resolution.candidates} == {m1, m2, m3}


def test_resolve_family_follows_a_star_of_delegation(tmp_path: Path) -> None:
    m1, m2, m3 = _member("int4"), _member("int4,int4"), _member("int4,int4,int4")
    dest = _member("int4,int4,int4,int4")
    db = _family_db(tmp_path, [m1, m2, m3, dest], [(m1, dest), (m2, dest), (m3, dest)])
    with closing(sqlite3.connect(db)) as conn:
        resolution = routines.resolve_family(conn, FAMILY_NAME)
    assert resolution.resolved_by == "repasse"
    assert resolution.chosen is not None and resolution.chosen.node_id == dest
    assert resolution.chain == (m1, dest)  # começa no membro de menos argumentos
    assert resolution.delegates == {m1: dest, m2: dest, m3: dest}


def test_resolve_family_with_two_destinations_is_ambiguous_with_delegates_to(
    tmp_path: Path,
) -> None:
    m1, dest1 = _member("int4"), _member("int4,int4")
    m2, dest2 = _member("int4,int4,int4"), _member("int4,int4,int4,int4")
    db = _family_db(tmp_path, [m1, dest1, m2, dest2], [(m1, dest1)])
    with closing(sqlite3.connect(db)) as conn:
        resolution = routines.resolve_family(conn, FAMILY_NAME)
    assert resolution.resolved_by == "ambiguo"
    assert resolution.chosen is None
    assert resolution.delegates == {m1: dest1}  # só quem tem aresta interna ganha delegates_to
    assert {c.node_id for c in resolution.candidates} == {m1, dest1, m2, dest2}


def test_resolve_family_with_a_cycle_is_ambiguous(tmp_path: Path) -> None:
    m1, m2 = _member("int4"), _member("int4,int4")
    db = _family_db(tmp_path, [m1, m2], [(m1, m2), (m2, m1)])
    with closing(sqlite3.connect(db)) as conn:
        resolution = routines.resolve_family(conn, FAMILY_NAME)
    assert resolution.resolved_by == "ambiguo"
    assert resolution.chosen is None
    assert resolution.delegates == {m1: m2, m2: m1}


def test_resolve_family_with_the_full_id_skips_delegation(tmp_path: Path) -> None:
    """Pediu a assinatura exata: devolve ela mesma, mesmo sendo a do meio de uma cadeia."""
    m1, m2, m3 = _member("int4"), _member("int4,int4"), _member("int4,int4,int4")
    db = _family_db(tmp_path, [m1, m2, m3], [(m1, m2), (m2, m3)])
    with closing(sqlite3.connect(db)) as conn:
        resolution = routines.resolve_family(conn, m2)
    assert resolution.resolved_by == "unico"
    assert resolution.chosen is not None and resolution.chosen.node_id == m2
    assert resolution.chain == ()


def test_resolve_family_missing_name_is_none(tmp_path: Path) -> None:
    db = _family_db(tmp_path, [_member("int4")], [])
    with closing(sqlite3.connect(db)) as conn:
        resolution = routines.resolve_family(conn, "fc_nao_existe")
    assert resolution == routines.RoutineResolution(None, (), "nenhum")


def test_describe_repasse_text(tmp_path: Path) -> None:
    m1, m2, m3 = _member("int4"), _member("int4,int4"), _member("int4,int4,int4")
    db = _family_db(tmp_path, [m1, m2, m3], [(m1, m2), (m2, m3)])
    with closing(sqlite3.connect(db)) as conn:
        resolution = routines.resolve_family(conn, FAMILY_NAME)
    text = routines.describe_repasse(resolution)
    assert text.startswith("`fc_fam` tem 3 assinaturas")
    assert "1 e 2 argumentos só repassam para esta" in text
    assert m3.partition(":")[2] in text


def test_arg_count() -> None:
    assert routines.arg_count("PgFunction:public.f()") == 0
    assert routines.arg_count("PgFunction:public.f(int4)") == 1
    assert routines.arg_count("PgFunction:public.f(int4,varchar)") == 2
