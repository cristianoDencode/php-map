"""Schema lido do banco, sem banco: snapshot escrito à mão e conexão falsa.

O DDL imita o formato de `pg_get_functiondef` (sem `;` no fim) e `pg_get_triggerdef`.
"""

from pathlib import Path
from typing import Any

import psycopg
import pytest

from php_map.errors import ConfigError
from php_map.graph.builder import GraphBuilder
from php_map.ingest import catalog_db, schema_db
from php_map.ingest.schema_db import DbRoutine, SchemaSnapshot, read_schema_db
from php_map.ingest.sql_schema import load_schema, load_schema_snapshot
from php_map.ingest.table_directory import DbTable, TableDirectory
from php_map.resolve.unresolved import UnresolvedWriter

DSN = "host=db.exemplo dbname=ecidade user=leitor password=senha-secreta"
SEARCH_PATH = '"$user", public, empenho, contabilidade'

FC_ANULA = """CREATE OR REPLACE FUNCTION public.fc_anula(integer, date)
 RETURNS text
 LANGUAGE plpgsql
AS $function$
begin
  insert into empanulado (e94_numemp) values ($1);
  update empempenho set e60_vlranu = 0 where e60_numemp = $1;
  perform fc_lanca($1);
  return 'ok';
end;
$function$
"""
FC_LANCA = """CREATE OR REPLACE FUNCTION contabilidade.fc_lanca(integer)
 RETURNS void
 LANGUAGE sql
AS $function$ insert into conlancamval (c69_codlan) values ($1) $function$
"""
FC_AUDITA = """CREATE OR REPLACE FUNCTION public.fc_audita()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
  insert into w_log (id) values (new.e60_numemp);
  return new;
end;
$function$
"""
TG_EMPENHO = (
    "CREATE TRIGGER tg_audita AFTER INSERT ON empenho.empempenho "
    "FOR EACH ROW EXECUTE PROCEDURE fc_audita()"
)
TABLES = [
    DbTable("empenho", "empempenho", "table", ("e60_numemp", "e60_vlranu")),
    DbTable("empenho", "empanulado", "table", ("e94_codanu", "e94_numemp")),
    DbTable("contabilidade", "conlancamval", "table", ("c69_sequen", "c69_codlan")),
    DbTable("public", "w_nunca_usada", "table", ("id",)),
    DbTable("public", "vw_empenhos", "view", ("e60_numemp",)),
]


def _snapshot() -> SchemaSnapshot:
    return SchemaSnapshot(
        source="db:db.exemplo/ecidade",
        search_path=SEARCH_PATH,
        tables=list(TABLES),
        functions=[
            DbRoutine("public", "fc_anula", FC_ANULA),
            DbRoutine("contabilidade", "fc_lanca", FC_LANCA),
            DbRoutine("public", "fc_audita", FC_AUDITA),
        ],
        triggers=[DbRoutine("empenho", "empempenho", TG_EMPENHO)],
    )


def _load(snapshot: SchemaSnapshot) -> tuple[GraphBuilder, Any, TableDirectory]:
    builder, directory = GraphBuilder(), TableDirectory()
    stats = load_schema_snapshot(snapshot, builder, UnresolvedWriter(), directory)
    return builder, stats, directory


def _edges(builder: GraphBuilder) -> set[tuple[str, str, str]]:
    return {(e.src, e.dst, e.type.value) for e in builder.iter_edges()}


# --------------------------------------------------------------------------- carga


def test_functions_and_triggers_go_through_the_ddl_reader() -> None:
    builder, stats, directory = _load(_snapshot())

    anula = "PgFunction:public.fc_anula(int4,date)"
    lanca = "PgFunction:contabilidade.fc_lanca(int4)"
    assert builder.node_attrs(anula)["source_file"] == "db:public.fc_anula"
    assert builder.node_attrs(anula)["min_args"] == 2
    edges = _edges(builder)
    # sem schema no corpo: resolvido pelo search_path da sessão
    assert (anula, "Table:empenho.empanulado", "INSERTS") in edges
    assert (anula, "Table:empenho.empempenho", "UPDATES") in edges
    assert (anula, lanca, "CALLS_RPC") in edges
    assert (lanca, "Table:contabilidade.conlancamval", "INSERTS") in edges
    # trigger ligado à função; tabela de trabalho inexistente cai em `public`
    assert ("Table:empenho.empempenho", "PgFunction:public.fc_audita()", "TRIGGERS") in edges
    assert ("PgFunction:public.fc_audita()", "Table:public.w_log", "INSERTS") in edges

    assert (stats.source, stats.functions, stats.triggers) == ("db:db.exemplo/ecidade", 3, 1)
    assert stats.search_path == ["public", "empenho", "contabilidade"]
    assert stats.directory_tables == len(directory) == 5
    # 3 do corpo pelo search_path, `w_log` não achada e a tabela do trigger, qualificada
    assert stats.table_refs == {"explicit": 1, "search_path": 3, "unique": 0, "not_found": 1}
    assert stats.parse_errors == 0


def test_tables_stay_in_the_directory_without_nodes() -> None:
    builder, stats, directory = _load(_snapshot())

    tables = {node.id: node.attrs for node in builder.iter_nodes() if node.label.value == "Table"}
    # só as referenciadas aparecem, e como placeholder (o pipeline materializa)
    assert "Table:public.w_nunca_usada" not in tables
    assert "Table:public.vw_empenhos" not in tables
    assert tables["Table:empenho.empanulado"]["resolved"] is False
    assert directory.info("public", "vw_empenhos") == TABLES[4]
    assert stats.tables == 0


def test_file_mode_qualifies_a_reference_to_a_table_created_later(tmp_path: Path) -> None:
    first, second = tmp_path / "a.sql", tmp_path / "b.sql"
    first.write_text(
        "create function fc_a() returns int as $$ begin perform 1 from exemplo; return 1; "
        "end $$ language plpgsql;\n",
        encoding="utf-8",
    )
    second.write_text("create table issqn.exemplo (id int4);\n", encoding="utf-8")
    builder = GraphBuilder()
    directory = TableDirectory(("public", "issqn"))

    stats = load_schema([first, second], builder, UnresolvedWriter(), directory)

    assert ("PgFunction:public.fc_a()", "Table:issqn.exemplo", "QUERIES") in _edges(builder)
    assert builder.node_attrs("Table:issqn.exemplo")["resolved"] is True
    assert stats.table_refs["search_path"] == 1


def test_redefined_function_keeps_only_the_last_body(tmp_path: Path) -> None:
    ddl = tmp_path / "x.sql"
    ddl.write_text(
        "create table t1 (id int4); create table t2 (id int4);\n"
        "create or replace function fc_x() returns int as $$ begin perform 1 from t1; "
        "return 1; end $$ language plpgsql;\n"
        "create or replace function fc_x() returns int as $$ begin perform 1 from t2; "
        "return 1; end $$ language plpgsql;\n",
        encoding="utf-8",
    )
    builder = GraphBuilder()
    load_schema([ddl], builder, UnresolvedWriter())

    targets = {dst for src, dst, _t in _edges(builder) if src == "PgFunction:public.fc_x()"}
    assert targets == {"Table:public.t2"}


def test_call_to_a_builtin_name_is_native_even_with_an_ecidade_overload() -> None:
    """O e-cidade tem `public.substr` (4 sobrecargas): `substr(...)` sem schema é a nativa."""
    shim = """CREATE OR REPLACE FUNCTION public.substr(integer, integer, integer)
 RETURNS text
 LANGUAGE sql
AS $function$ select substr($1::text, $2, $3) $function$
"""
    body = """CREATE OR REPLACE FUNCTION public.fc_usa()
 RETURNS text
 LANGUAGE plpgsql
AS $function$
begin
  return substr('abc', 1, 2) || public.substr(1, 1, 1);
end;
$function$
"""
    snapshot = SchemaSnapshot(
        source="db:x/y",
        search_path="public",
        functions=[DbRoutine("public", "substr", shim), DbRoutine("public", "fc_usa", body)],
        builtin_functions=frozenset({"substr"}),
    )
    builder, _stats, _directory = _load(snapshot)

    calls = {dst for src, dst, t in _edges(builder) if t == "CALLS_RPC" and "fc_usa" in src}
    # só a chamada qualificada chega à sobrecarga do e-cidade
    assert calls == {"PgFunction:public.substr(int4,int4,int4)"}


# --------------------------------------------------------------------------- leitura do banco


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class FakeSchemaConnection:
    """Responde às consultas de `schema_db._DbSchemaFetch`, paginando pelo `oid`."""

    def __init__(self, read_only: str = "on") -> None:
        self.read_only_setting = read_only
        self.tables = [
            {"oid": 100 + i, "schema": t.schema, "name": t.name, "kind": k, "columns": t.columns}
            for i, (t, k) in enumerate(zip(TABLES, ["r", "r", "p", "r", "v"], strict=True))
        ]
        self.functions = [
            {"oid": 10, "schema": "public", "name": "fc_anula", "ddl": FC_ANULA},
            {"oid": 20, "schema": "contabilidade", "name": "fc_lanca", "ddl": FC_LANCA},
            {"oid": 30, "schema": "public", "name": "fc_audita", "ddl": FC_AUDITA},
        ]
        self.triggers = [{"oid": 5, "schema": "empenho", "name": "empempenho", "ddl": TG_EMPENHO}]
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self.isolation_level: Any = None
        self.read_only = False

    def __enter__(self) -> "FakeSchemaConnection":
        return self

    def __exit__(self, *_exc: object) -> None:
        pass

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> _Cursor:
        self.queries.append((query, params))
        if query == catalog_db._READ_ONLY:
            return _Cursor([{"read_only": self.read_only_setting}])
        if query == schema_db._SEARCH_PATH:
            return _Cursor([{"search_path": SEARCH_PATH}])
        if query == schema_db._BUILTINS:
            return _Cursor([{"name": "substr"}, {"name": "LPAD"}])
        rows = {
            schema_db._TABLES: self.tables,
            schema_db._FUNCTIONS: self.functions,
            schema_db._TRIGGERS: self.triggers,
        }[query]
        last, size = params
        return _Cursor([row for row in rows if row["oid"] > last][:size])


def _fake(monkeypatch: pytest.MonkeyPatch, conn: FakeSchemaConnection) -> FakeSchemaConnection:
    monkeypatch.setattr(catalog_db.psycopg, "connect", lambda dsn, **kw: conn)
    return conn


def test_reads_read_only_and_pages_by_oid(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _fake(monkeypatch, FakeSchemaConnection())
    monkeypatch.setattr(schema_db, "TABLE_PAGE_SIZE", 2)
    monkeypatch.setattr(schema_db, "ROUTINE_PAGE_SIZE", 2)

    snapshot = read_schema_db(DSN)

    assert snapshot.source == "db:db.exemplo/ecidade" and snapshot.search_path == SEARCH_PATH
    assert snapshot.tables == TABLES
    assert [f.name for f in snapshot.functions] == ["fc_anula", "fc_lanca", "fc_audita"]
    assert snapshot.triggers == [DbRoutine("empenho", "empempenho", TG_EMPENHO)]
    assert snapshot.builtin_functions == frozenset({"substr", "lpad"})
    assert conn.read_only is True and conn.queries[0][0] == catalog_db._READ_ONLY
    table_pages = [params for query, params in conn.queries if query == schema_db._TABLES]
    assert table_pages == [(0, 2), (101, 2), (103, 2)]  # 5 tabelas, páginas de 2
    # nenhuma consulta fora do pg_catalog e nenhum comando que não seja leitura
    assert all(query.lstrip().lower().startswith("select") for query, _p in conn.queries)
    assert all("pg_" in query or "current_setting" in query for query, _p in conn.queries)


def test_session_that_is_not_read_only_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _fake(monkeypatch, FakeSchemaConnection(read_only="off"))
    with pytest.raises(ConfigError, match="somente leitura"):
        read_schema_db(DSN)
    assert len(conn.queries) == 1


def test_connection_error_hides_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(dsn: str, **kwargs: Any) -> Any:
        raise psycopg.OperationalError(f"falhou para {dsn}")

    monkeypatch.setattr(catalog_db.psycopg, "connect", refuse)
    with pytest.raises(ConfigError) as error:
        read_schema_db(DSN)
    assert "senha-secreta" not in str(error.value)
    assert "db:db.exemplo/ecidade" in str(error.value)


def test_injected_fetch() -> None:
    class Fetch:
        def search_path(self) -> str:
            return "public"

        def tables(self) -> list[DbTable]:
            return TABLES[:1]

        def functions(self) -> list[DbRoutine]:
            return []

        def triggers(self) -> list[DbRoutine]:
            return []

        def builtin_functions(self) -> frozenset[str]:
            return frozenset()

    snapshot = read_schema_db(DSN, fetch=Fetch())
    assert snapshot.tables == TABLES[:1] and snapshot.seconds >= 0


@pytest.mark.remote_db
def test_remote_schema_has_the_pilot_tables(remote_dsn: str) -> None:
    """Banco remoto: só metadado, nada gravado."""
    snapshot = read_schema_db(remote_dsn)
    by_name = {(t.schema, t.name): t for t in snapshot.tables}
    assert "e60_numemp" in by_name[("empenho", "empempenho")].columns
    assert ("protocolo", "cgm") in by_name
    assert "empenho" in snapshot.search_path
    assert any(f.name == "fc_verifica_lancamento" for f in snapshot.functions)
