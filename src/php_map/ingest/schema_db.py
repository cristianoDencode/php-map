"""Schema do e-cidade lido ao vivo do `pg_catalog`.

Só metadado, numa sessão somente leitura (a mesma do catálogo, `catalog_db._connect`): as
tabelas com as colunas, o DDL das funções (`pg_get_functiondef`) e dos triggers
(`pg_get_triggerdef`) e o `search_path` da sessão. Nenhuma tabela de negócio é lida, nada é
gravado em disco, e o log e o resumo têm só contagens e `db:<host>/<base>`.

O `pg_dump --schema-only` da base remota (28,7 mil tabelas) trava bloqueando tabela por tabela;
estas consultas respondem em menos de 1 s. Todas são paginadas pelo `oid`: um resultado grande
numa consulta só trava na rede (lição aprendida em bases reais).
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg
from psycopg.conninfo import conninfo_to_dict

from php_map.errors import ConfigError
from php_map.ingest import catalog_db
from php_map.ingest.table_directory import DbTable

logger = logging.getLogger(__name__)

# Linhas por página: tabelas (uma linha por tabela, com as colunas num array) e rotinas (o DDL
# das funções soma 8 MB na base remota; páginas pequenas evitam o travamento na rede).
TABLE_PAGE_SIZE = 5000
ROUTINE_PAGE_SIZE = 200
_OWN_SCHEMAS = "n.nspname !~ '^pg_' and n.nspname <> 'information_schema'"
_KINDS = {"r": "table", "p": "table", "v": "view", "m": "view", "f": "foreign"}

_SEARCH_PATH = "select current_setting('search_path') as search_path"
# Só os nomes das funções nativas (~2,9 mil): para não ligar `substr(...)` às sobrecargas do
# e-cidade no `public` (decisão de projeto).
_BUILTINS = """
select distinct p.proname as name
from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'pg_catalog'
"""
_TABLES = f"""
select c.oid as oid, n.nspname as schema, c.relname as name, c.relkind::text as kind,
       coalesce(array_agg(a.attname::text order by a.attnum)
                filter (where a.attnum > 0 and not a.attisdropped), '{{}}') as columns
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
left join pg_attribute a on a.attrelid = c.oid
where c.relkind in ('r', 'p', 'v', 'm', 'f') and {_OWN_SCHEMAS} and c.oid > %s::oid
group by c.oid, n.nspname, c.relname, c.relkind
order by c.oid
limit %s
"""
# Sem agregados (`pg_get_functiondef` falha neles) e sem as funções de extensão (pgcrypto,
# unaccent…), que não são do e-cidade.
_FUNCTIONS = f"""
select p.oid as oid, n.nspname as schema, p.proname as name, pg_get_functiondef(p.oid) as ddl
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
where p.prokind = 'f' and {_OWN_SCHEMAS} and p.oid > %s::oid
  and not exists (select 1 from pg_depend d
                  where d.classid = 'pg_proc'::regclass and d.objid = p.oid and d.deptype = 'e')
order by p.oid
limit %s
"""
_TRIGGERS = f"""
select t.oid as oid, n.nspname as schema, c.relname as name, pg_get_triggerdef(t.oid) as ddl
from pg_trigger t
join pg_class c on c.oid = t.tgrelid
join pg_namespace n on n.oid = c.relnamespace
where not t.tgisinternal and {_OWN_SCHEMAS} and t.oid > %s::oid
order by t.oid
limit %s
"""


@dataclass(frozen=True, slots=True)
class DbRoutine:
    """DDL de uma função ou de um trigger (`schema`/`name` do trigger são os da tabela)."""

    schema: str
    name: str
    ddl: str


@dataclass(slots=True)
class SchemaSnapshot:
    source: str  # `db:<host>/<base>` (`catalog_db.describe_dsn`), nunca o DSN
    search_path: str
    tables: list[DbTable] = field(default_factory=list)
    functions: list[DbRoutine] = field(default_factory=list)
    triggers: list[DbRoutine] = field(default_factory=list)
    builtin_functions: frozenset[str] = frozenset()  # nomes das funções do `pg_catalog`
    seconds: float = 0.0


class SchemaFetch(Protocol):
    """Leitura do banco; injetável para testar sem banco."""

    def search_path(self) -> str: ...
    def tables(self) -> list[DbTable]: ...
    def functions(self) -> list[DbRoutine]: ...
    def triggers(self) -> list[DbRoutine]: ...
    def builtin_functions(self) -> frozenset[str]: ...


def read_schema_db(dsn: str, fetch: SchemaFetch | None = None) -> SchemaSnapshot:
    """Tabelas, funções, triggers e `search_path` do banco em `dsn` (somente leitura).

    Erro de conexão ou de permissão → `ConfigError` com `db:<host>/<base>`, sem a senha.
    """
    started = time.perf_counter()
    source = catalog_db.describe_dsn(dsn)
    if fetch is not None:
        snapshot = _read(fetch, source)
    else:
        password = str(conninfo_to_dict(dsn).get("password") or "")
        try:
            with catalog_db._connect(dsn) as conn:
                snapshot = _read(_DbSchemaFetch(conn), source)
        except psycopg.Error as exc:
            reason = str(exc).strip()
            if password:
                reason = reason.replace(password, "***")
            raise ConfigError(f"não foi possível ler o schema em {source}: {reason}") from None
    snapshot.seconds = round(time.perf_counter() - started, 2)
    logger.info(
        "schema lido de %s: %d tabelas, %d funções, %d triggers em %.1f s",
        source,
        len(snapshot.tables),
        len(snapshot.functions),
        len(snapshot.triggers),
        snapshot.seconds,
    )
    return snapshot


def check_reachable(dsn: str) -> None:
    """Abre e fecha uma conexão somente leitura: o modo automático do scan confere a
    base antes de extrair e conferir 35 mil arquivos, em vez de falhar minutos depois.

    Erro de conexão → `ConfigError` com `db:<host>/<base>`, sem a senha.
    """
    source = catalog_db.describe_dsn(dsn)
    password = str(conninfo_to_dict(dsn).get("password") or "")
    try:
        with catalog_db._connect(dsn):
            return
    except psycopg.Error as exc:
        reason = str(exc).strip()
        if password:
            reason = reason.replace(password, "***")
        raise ConfigError(f"não foi possível conectar em {source}: {reason}") from None


def _read(fetch: SchemaFetch, source: str) -> SchemaSnapshot:
    return SchemaSnapshot(
        source=source,
        search_path=fetch.search_path(),
        tables=fetch.tables(),
        functions=fetch.functions(),
        triggers=fetch.triggers(),
        builtin_functions=fetch.builtin_functions(),
    )


class _DbSchemaFetch:
    """`SchemaFetch` sobre uma conexão somente leitura (conferida antes de ler)."""

    def __init__(self, conn: "psycopg.Connection[dict[str, Any]]") -> None:
        self.conn = conn
        catalog_db.check_read_only(conn, "do schema")

    def search_path(self) -> str:
        row = self.conn.execute(_SEARCH_PATH).fetchone()
        return str(row["search_path"]) if row else ""

    def tables(self) -> list[DbTable]:
        return [
            DbTable(
                str(row["schema"]).lower(),
                str(row["name"]).lower(),
                _KINDS.get(str(row["kind"]), "table"),
                tuple(str(c).lower() for c in row["columns"] or ()),
            )
            for row in self._pages(_TABLES, TABLE_PAGE_SIZE)
        ]

    def functions(self) -> list[DbRoutine]:
        return self._routines(_FUNCTIONS)

    def triggers(self) -> list[DbRoutine]:
        return self._routines(_TRIGGERS)

    def builtin_functions(self) -> frozenset[str]:
        rows = self.conn.execute(_BUILTINS).fetchall()
        return frozenset(str(row["name"]).lower() for row in rows)

    def _routines(self, query: str) -> list[DbRoutine]:
        return [
            DbRoutine(str(row["schema"]).lower(), str(row["name"]).lower(), str(row["ddl"]))
            for row in self._pages(query, ROUTINE_PAGE_SIZE)
            if row["ddl"]
        ]

    def _pages(self, query: str, size: int) -> list[dict[str, Any]]:
        """`… and oid > %s order by oid limit %s`, até a página curta (mesmo retrato: a
        transação é `REPEATABLE READ`)."""
        rows: list[dict[str, Any]] = []
        last = 0
        while True:
            page = [dict(row) for row in self.conn.execute(query, (last, size)).fetchall()]
            rows.extend(page)
            if len(page) < size:
                return rows
            last = int(page[-1]["oid"])
