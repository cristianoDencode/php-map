"""Catálogo do e-cidade lido ao vivo do banco.

Um `SELECT` por tabela nas 8 tabelas de sistema do dicionário (só as colunas usadas), numa sessão
somente leitura, e o mesmo `CatalogData` do leitor de arquivos (`catalog_sql.rows_to_catalog`).
As linhas ficam só em memória: nada é gravado em disco, e o log e as estatísticas têm só
contagens.

O DSN nunca aparece no resumo nem nas mensagens: só `db:<host>/<base>` (`describe_dsn`).
"""

import logging
import os
import time
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from php_map.errors import ConfigError
from php_map.ingest.catalog_sql import (
    TABLES,
    USED_COLUMNS,
    CatalogData,
    CatalogReadStats,
    Evidence,
    rows_to_catalog,
)

logger = logging.getLogger(__name__)

# `fetch(tabela)` → linhas (as colunas de `USED_COLUMNS` que existirem) ou `None` se a tabela
# não existir no banco. Injetável para testar sem banco.
FetchFn = Callable[[str], list[dict[str, Any]] | None]

# Somente leitura na sessão inteira; o `statement_timeout` protege a base de 515 GB.
SESSION_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=300000"
CONNECT_TIMEOUT_S = 15
# Leitura em páginas pela chave: um resultado único de ~2 MB (`db_syscampo`) travava em metade
# das leituras no banco remoto (servidor parado em `ClientWrite`); em páginas de 5.000 linhas,
# 16 de 16 leituras passaram (lição aprendida em bases reais).
PAGE_SIZE = 5000
# Schema do dicionário no e-cidade, preferido quando a tabela está fora do `search_path`.
CATALOG_SCHEMA = "configuracoes"

_RELATION_BY_PATH = """
select n.nspname as schema, c.relname as name, c.oid as oid
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where c.oid = to_regclass(%s)::oid
"""
# Fora do `search_path`: procura pelo nome (índice de pg_class), sem varrer o catálogo.
_RELATION_BY_NAME = """
select n.nspname as schema, c.relname as name, c.oid as oid
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where c.relname = %s and c.relkind in ('r', 'p', 'v', 'm', 'f')
order by n.nspname = %s desc, n.nspname
limit 1
"""
_COLUMNS = """
select attname from pg_attribute
where attrelid = %s and attnum > 0 and not attisdropped
"""
_READ_ONLY = "select current_setting('transaction_read_only') as read_only"


def describe_dsn(dsn: str) -> str:
    """`db:<host>/<base>`, sem usuário nem senha. DSN inválido → `ConfigError` sem ecoá-lo."""
    try:
        params = conninfo_to_dict(dsn)
    except (psycopg.Error, ValueError, TypeError):
        raise ConfigError("DSN do catálogo inválido (confira --catalog-db)") from None
    host = str(params.get("host") or os.environ.get("PGHOST") or "localhost")
    base = str(params.get("dbname") or os.environ.get("PGDATABASE") or params.get("user") or "?")
    return f"db:{host}/{base}"


def load_catalog_db(dsn: str, fetch: FetchFn | None = None) -> CatalogData:
    """Estado atual das 8 tabelas do catálogo, lido do banco em `dsn`.

    Sem `fetch`, conecta com `psycopg` numa transação somente leitura (`REPEATABLE READ`, um
    retrato só das 8 tabelas). Erro de conexão ou de permissão → `ConfigError` sem a senha.
    """
    started = time.perf_counter()
    stats = CatalogReadStats(source=describe_dsn(dsn))
    if fetch is not None:
        rows = _read_all(fetch, stats)
    else:
        password = str(conninfo_to_dict(dsn).get("password") or "")
        try:
            with _connect(dsn) as conn:
                rows = _read_all(_DbFetch(conn), stats)
        except psycopg.Error as exc:
            reason = _safe(str(exc).strip(), password)
            raise ConfigError(
                f"não foi possível ler o catálogo em {stats.source}: {reason}"
            ) from None
    data = rows_to_catalog(rows, _evidence, stats)
    stats.seconds = round(time.perf_counter() - started, 2)
    total = sum(stats.rows.values())
    logger.info("catálogo lido de %s: %d linhas em %.1f s", stats.source, total, stats.seconds)
    return data


def _read_all(fetch: FetchFn, stats: CatalogReadStats) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    for table in TABLES:
        found = fetch(table)
        stats.statements += 1
        if found is None:
            stats.missing_tables.append(table)
            logger.warning("tabela do catálogo ausente no banco: %s", table)
            found = []
        rows[table] = found
        stats.read[table] = len(found)
    return rows


def _evidence(table: str, key: tuple[int, ...]) -> Evidence:
    pairs = ",".join(
        f"{column}={value}" for column, value in zip(TABLES[table].key, key, strict=True)
    )
    return {"path": f"db:{table}", "key": pairs}


def _safe(message: str, password: str) -> str:
    return message.replace(password, "***") if password else message


def _connect(dsn: str) -> "psycopg.Connection[dict[str, Any]]":
    conn = psycopg.connect(
        dsn,
        options=SESSION_OPTIONS,
        client_encoding="UTF8",  # o servidor converte (sem isso, os acentos se corrompem)
        application_name="php-map",
        connect_timeout=CONNECT_TIMEOUT_S,
        row_factory=dict_row,
    )
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    return conn


def check_read_only(conn: "psycopg.Connection[dict[str, Any]]", what: str) -> None:
    """Confere que a sessão ficou somente leitura antes de qualquer outra consulta."""
    checked = conn.execute(_READ_ONLY).fetchone()
    if checked is None or checked["read_only"] != "on":
        raise ConfigError(f"a sessão {what} não ficou somente leitura; nada foi lido")


class _DbFetch:
    """`FetchFn` sobre uma conexão: acha a tabela, as colunas que existem e faz o `SELECT`."""

    def __init__(self, conn: "psycopg.Connection[dict[str, Any]]") -> None:
        self.conn = conn
        check_read_only(conn, "do catálogo")

    def _one(self, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        return self.conn.execute(query, params).fetchone()

    def __call__(self, table: str) -> list[dict[str, Any]] | None:
        relation = self._one(_RELATION_BY_PATH, (table,)) or self._one(
            _RELATION_BY_NAME, (table, CATALOG_SCHEMA)
        )
        if relation is None:
            return None
        existing = {
            row["attname"] for row in self.conn.execute(_COLUMNS, (relation["oid"],)).fetchall()
        }
        # Só as colunas que o `CatalogData` usa, na ordem do dicionário, e que existem no banco.
        wanted = USED_COLUMNS[table]
        columns = [c for c in TABLES[table].columns if c in wanted and c in existing]
        select = sql.SQL("select {columns} from {relation}").format(
            columns=sql.SQL(", ").join(sql.Identifier(column) for column in columns),
            relation=sql.Identifier(relation["schema"], relation["name"]),
        )
        key = TABLES[table].key
        if not set(key) <= existing:  # sem a chave não há como paginar: uma consulta só
            return [dict(row) for row in self.conn.execute(select).fetchall()]
        return self._pages(select, key)

    def _pages(self, select: sql.Composed, key: tuple[str, ...]) -> list[dict[str, Any]]:
        """`… where (chave) > (última) order by chave limit PAGE_SIZE`, até a página curta.

        A transação é `REPEATABLE READ`: todas as páginas veem o mesmo retrato da tabela.
        """
        order = sql.SQL(", ").join(sql.Identifier(column) for column in key)
        first = sql.SQL("{select} order by {order} limit %s").format(select=select, order=order)
        after = sql.SQL("{select} where ({order}) > ({values}) order by {order} limit %s").format(
            select=select, order=order, values=sql.SQL(", ").join(sql.Placeholder() * len(key))
        )
        rows: list[dict[str, Any]] = []
        last: tuple[Any, ...] | None = None
        while True:
            query, params = (first, ()) if last is None else (after, last)
            page = [dict(row) for row in self.conn.execute(query, (*params, PAGE_SIZE)).fetchall()]
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return rows
            last = tuple(page[-1].get(column) for column in key)
            if None in last:  # chave nula ordena por último: o resto também não tem chave
                return rows
