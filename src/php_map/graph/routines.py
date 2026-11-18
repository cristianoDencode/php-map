"""Texto das funções da base, guardado junto do grafo.

Uma tabela `routine_sources` no mesmo `.db`, gravada depois do `save` (como `analytics_*` e o
índice de busca): o texto é lido só sob demanda, não engorda `nodes.attrs` nem o carregamento do
grafo, e vai junto na publicação para o celular.

**Contrato com os leitores de regras, consultas e campos:** o `ddl` é guardado inteiro, como o
banco devolveu (`pg_get_functiondef`), e as linhas (`line`) de regras, consultas e campos são
linhas desse texto (1 = `CREATE`, como nas arestas `db:<schema>.<nome>`). Os leitores toleram a
ausência da tabela (grafos antigos), como já se faz com `search_texts`.
"""

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from php_map.errors import QueryError, StoreError

if TYPE_CHECKING:
    from php_map.ingest.plpgsql_rules import RoutineProfile, Rule
    from php_map.ingest.sql_fields import StatementFacts

TABLE = "routine_sources"
GENERATED_PREFIX = "fc_auditoria_tabela_"  # funções geradas pela auditoria do e-cidade

_SCHEMA = f"""
DROP TABLE IF EXISTS {TABLE};
CREATE TABLE {TABLE} (
    node_id TEXT PRIMARY KEY,
    schema TEXT NOT NULL,
    name TEXT NOT NULL,
    language TEXT NOT NULL,
    generated INTEGER NOT NULL,
    sha1 TEXT NOT NULL,
    line_count INTEGER NOT NULL,
    read_at TEXT NOT NULL,
    ddl TEXT NOT NULL
);
CREATE INDEX idx_{TABLE}_name ON {TABLE}(schema, name);
"""
_COLUMNS = "node_id, schema, name, language, generated, sha1, line_count, read_at, ddl"
_BODY_START = re.compile(r"\bAS\s+(\$[A-Za-z0-9_]*\$)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RoutineSource:
    node_id: str  # `PgFunction:<schema>.<nome>(<tipos>)`, o mesmo ID do nó
    schema: str
    name: str
    language: str
    generated: bool  # `fc_auditoria_tabela_*`
    ddl: str  # como veio de `pg_get_functiondef`
    sha1: str
    line_count: int
    read_at: str  # ISO com fuso: quando o scan leu da base


@dataclass(frozen=True, slots=True)
class RoutineStats:
    stored: int
    bytes: int
    source: str  # `db` (lido da base) ou `none`
    read_at: str | None
    without_node: int  # funções lidas cujo texto não gerou nó (parse falhou por inteiro)
    warning: str | None = None


def split_lines(ddl: str) -> list[str]:
    """As linhas do texto, sem a linha vazia que a quebra de linha final deixaria."""
    lines = ddl.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return lines


def make_source(
    node_id: str, schema: str, name: str, language: str, ddl: str, read_at: str
) -> RoutineSource:
    """O registro de uma função, com `sha1` do texto original e a contagem de linhas."""
    return RoutineSource(
        node_id=node_id,
        schema=schema,
        name=name,
        language=language,
        generated=name.startswith(GENERATED_PREFIX),
        ddl=ddl,
        sha1=hashlib.sha1(ddl.encode("utf-8")).hexdigest(),
        line_count=len(split_lines(ddl)),
        read_at=read_at,
    )


def function_body(ddl: str) -> str:
    """O corpo da função (entre os `$tag$` do `AS`), para a busca; sem eles, o texto inteiro."""
    match = _BODY_START.search(ddl)
    if match is None:
        return ddl
    tag = match.group(1)
    start = match.end()
    end = ddl.rfind(tag)
    return ddl[start:end] if end >= start else ddl[start:]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_routines(db_path: Path, rows: Sequence[RoutineSource]) -> float:
    """Recria `routine_sources` numa transação. Devolve os segundos gastos."""
    if not db_path.is_file():
        raise StoreError(f"banco do grafo inexistente: {db_path}. Rode o scan primeiro.")
    started = time.perf_counter()
    try:
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executescript("BEGIN;" + _SCHEMA)
            conn.executemany(
                f"INSERT INTO {TABLE} ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        r.node_id, r.schema, r.name, r.language, int(r.generated),
                        r.sha1, r.line_count, r.read_at, r.ddl,
                    )
                    for r in rows
                ),
            )  # fmt: skip
    except sqlite3.Error as exc:
        raise StoreError(f"texto das funções não gravado em {db_path}: {exc}") from exc
    return round(time.perf_counter() - started, 2)


def has_routines(conn: sqlite3.Connection) -> bool:
    """O banco tem a tabela (grafos antigos não têm)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    return row is not None


def _of(row: Sequence[object]) -> RoutineSource:
    return RoutineSource(
        node_id=str(row[0]),
        schema=str(row[1]),
        name=str(row[2]),
        language=str(row[3]),
        generated=bool(row[4]),
        sha1=str(row[5]),
        line_count=int(str(row[6])),
        read_at=str(row[7]),
        ddl=str(row[8]),
    )


def read_routine(conn: sqlite3.Connection, node_id: str) -> RoutineSource | None:
    """O texto da função de ID `node_id`; `None` se não há (ou se a tabela não existe)."""
    if not has_routines(conn):
        return None
    row = conn.execute(f"SELECT {_COLUMNS} FROM {TABLE} WHERE node_id = ?", (node_id,)).fetchone()
    return _of(row) if row else None


def read_at_of(conn: sqlite3.Connection, node_id: str) -> str | None:
    """Quando o scan leu o texto da função (sem trazer o texto); `None` se não há."""
    if not has_routines(conn):
        return None
    row = conn.execute(f"SELECT read_at FROM {TABLE} WHERE node_id = ?", (node_id,)).fetchone()
    return str(row[0]) if row else None


def find_routines(conn: sqlite3.Connection, name: str) -> list[RoutineSource]:
    """Funções por ID completo, `schema.nome` ou só o nome (sem diferenciar maiúsculas)."""
    if not has_routines(conn):
        return []
    text = name.strip()
    exact = read_routine(conn, text)
    if exact is not None:
        return [exact]
    lowered = text.lower()
    if "(" in lowered:  # `public.lpad(int4,int4)` sem o prefixo do nó
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE lower(node_id) = ?",
            (f"pgfunction:{lowered}",),
        ).fetchone()
        return [_of(row)] if row else []
    schema, dot, bare = lowered.rpartition(".")
    if dot:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE schema = ? AND name = ? ORDER BY node_id",
            (schema, bare),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE name = ? ORDER BY node_id", (bare,)
        ).fetchall()
    return [_of(r) for r in rows]


def suggest_names(conn: sqlite3.Connection, name: str, limit: int = 10) -> list[str]:
    """Nomes de função que começam com `name` (sem o schema), para a mensagem de erro."""
    if not has_routines(conn):
        return []
    bare = name.strip().lower().rpartition(".")[2]
    rows = conn.execute(
        f"SELECT DISTINCT schema || '.' || name FROM {TABLE} "
        "WHERE name LIKE ? ESCAPE '\\' ORDER BY 1 LIMIT ?",
        (bare.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", limit),
    ).fetchall()
    return [str(r[0]) for r in rows]


def count_routines(conn: sqlite3.Connection) -> int:
    if not has_routines(conn):
        return 0
    return int(conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])


# --------------------------------------------------------------------------- regras
#
# `routine_rules` e `routine_profile`: o que o leitor léxico (`ingest/plpgsql_rules.py`) achou no
# texto de cada função — validações, avisos, decisões, tratamento de exceção e comentários, mais
# o perfil (parâmetros, retorno, contagens, variáveis declaradas). Gravadas na mesma transação de
# `routine_sources`, pela mesma convenção de linha (1 = `CREATE`). Os leitores toleram a ausência
# das tabelas, como as de cima.

RULES_TABLE = "routine_rules"
PROFILE_TABLE = "routine_profile"
_RULES_SCHEMA = f"""
DROP TABLE IF EXISTS {RULES_TABLE};
CREATE TABLE {RULES_TABLE} (
    routine_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    level TEXT,
    line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    condition TEXT,
    message TEXT,
    params TEXT,
    sqlstate TEXT,
    dynamic INTEGER NOT NULL,
    truncated INTEGER NOT NULL,
    PRIMARY KEY (routine_id, seq)
);
CREATE INDEX idx_{RULES_TABLE}_routine_kind ON {RULES_TABLE}(routine_id, kind);
CREATE INDEX idx_{RULES_TABLE}_kind_level ON {RULES_TABLE}(kind, level);
"""
_PROFILE_SCHEMA = f"""
DROP TABLE IF EXISTS {PROFILE_TABLE};
CREATE TABLE {PROFILE_TABLE} (
    routine_id TEXT PRIMARY KEY,
    params TEXT NOT NULL,
    returns TEXT NOT NULL,
    language TEXT NOT NULL,
    volatility TEXT NOT NULL,
    security_definer INTEGER NOT NULL,
    counts TEXT NOT NULL,
    declared TEXT NOT NULL
);
"""
_RULES_COLUMNS = (
    "routine_id, seq, kind, level, line, end_line, condition, message, params, sqlstate, "
    "dynamic, truncated"
)
_PROFILE_COLUMNS = (
    "routine_id, params, returns, language, volatility, security_definer, counts, declared"  # noqa: E501
)


def write_rules(
    db_path: Path, results: Sequence[tuple[str, Sequence["Rule"], "RoutineProfile"]]
) -> float:
    """Recria `routine_rules` e `routine_profile` numa transação. Devolve os segundos gastos.

    `results`: `(ID do PgFunction, regras, perfil)`, como `ingest.plpgsql_rules.scan_routine`
    devolve para cada função de `routine_sources`.
    """
    if not db_path.is_file():
        raise StoreError(f"banco do grafo inexistente: {db_path}. Rode o scan primeiro.")
    started = time.perf_counter()
    rule_rows = [
        (
            node_id,
            seq,
            rule.kind,
            rule.level,
            rule.line,
            rule.end_line,
            rule.condition,
            rule.message,
            json.dumps(list(rule.params), ensure_ascii=False),
            rule.sqlstate,
            int(rule.dynamic),
            int(rule.truncated),
        )  # fmt: skip
        for node_id, rules, _profile in results
        for seq, rule in enumerate(rules)
    ]
    profile_rows = [
        (
            node_id,
            json.dumps([list(p) for p in profile.params], ensure_ascii=False),
            profile.returns,
            profile.language,
            profile.volatility,
            int(profile.security_definer),
            json.dumps(profile.counts, ensure_ascii=False, sort_keys=True),
            json.dumps(list(profile.declared), ensure_ascii=False),
        )
        for node_id, _rules, profile in results
    ]
    try:
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executescript("BEGIN;" + _RULES_SCHEMA + _PROFILE_SCHEMA)
            conn.executemany(
                f"INSERT INTO {RULES_TABLE} ({_RULES_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rule_rows,
            )
            conn.executemany(
                f"INSERT INTO {PROFILE_TABLE} ({_PROFILE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                profile_rows,
            )
    except sqlite3.Error as exc:
        raise StoreError(f"regras das funções não gravadas em {db_path}: {exc}") from exc
    return round(time.perf_counter() - started, 2)


def has_rules(conn: sqlite3.Connection) -> bool:
    """O banco tem as tabelas de regras (grafos antigos não têm)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (RULES_TABLE,)
    ).fetchone()
    return row is not None


def _rule_of(row: Sequence[object]) -> dict[str, Any]:
    (
        _routine_id, seq, kind, level, line, end_line, condition, message, params, sqlstate,
        dynamic, truncated,
    ) = row  # fmt: skip
    return {
        "seq": int(str(seq)),
        "kind": str(kind),
        "level": str(level) if level is not None else None,
        "line": int(str(line)),
        "end_line": int(str(end_line)),
        "condition": str(condition) if condition is not None else None,
        "message": str(message) if message is not None else None,
        "params": json.loads(str(params)) if params else [],
        "sqlstate": str(sqlstate) if sqlstate is not None else None,
        "dynamic": bool(dynamic),
        "truncated": bool(truncated),
    }


_DEFAULT_RULE_KINDS = ("validation", "error", "warning", "handler", "decision", "comment")
# Tabela derivada (`graph/formula_index.py` grava e é a única dona do schema desta tabela derivada;
# `routines.py` só lê, pelo nome, para não criar um import circular).
_RULE_TOPICS_TABLE = "routine_rule_topics"


def has_rule_topics(conn: sqlite3.Connection) -> bool:
    """A tabela de assunto das regras existe (rode `php-map index-formulas` ou o
    scan novo para gravá-la)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_RULE_TOPICS_TABLE,)
    ).fetchone()
    return row is not None


def read_rules(
    conn: sqlite3.Connection,
    routine_id: str,
    *,
    kinds: Sequence[str] | None = None,
    level: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any] | None:
    """As regras de uma função, paginadas. `None` se o banco não tem a tabela.

    `kinds=None` usa o padrão (sem `diagnostic`, como a ficha de regras de negócio e a
    interface); passe `kinds=()` para não filtrar por tipo."""
    if not has_rules(conn):
        return None
    wanted = _DEFAULT_RULE_KINDS if kinds is None else tuple(kinds)
    clauses = ["routine_id = ?"]
    params: list[object] = [routine_id]
    if wanted:
        clauses.append(f"kind IN ({', '.join('?' * len(wanted))})")
        params.extend(wanted)
    if level is not None:
        clauses.append("level = ?")
        params.append(level)
    where = " AND ".join(clauses)
    total = int(
        conn.execute(f"SELECT count(*) FROM {RULES_TABLE} WHERE {where}", params).fetchone()[0]
    )
    rows = conn.execute(
        f"SELECT {_RULES_COLUMNS} FROM {RULES_TABLE} WHERE {where} ORDER BY seq LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return {
        "total": total,
        "items": [_rule_of(r) for r in rows],
        "truncated": offset + len(rows) < total,
    }


def read_profile(conn: sqlite3.Connection, routine_id: str) -> dict[str, Any] | None:
    """O perfil de uma função; `None` se não há (ou se a tabela não existe)."""
    if not has_rules(conn):
        return None
    row = conn.execute(
        f"SELECT {_PROFILE_COLUMNS} FROM {PROFILE_TABLE} WHERE routine_id = ?", (routine_id,)
    ).fetchone()
    if row is None:
        return None
    _routine_id, params, returns, language, volatility, security_definer, counts, declared = row
    return {
        "params": [
            {"name": p[0], "mode": p[1], "type": p[2], "default": p[3]} for p in json.loads(params)
        ],
        "returns": returns,
        "language": language,
        "volatility": volatility,
        "security_definer": bool(security_definer),
        "counts": json.loads(counts),
        "declared": json.loads(declared),
    }


def search_rules(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    kind: str | None = None,
    level: str | None = None,
    topic: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any] | None:
    """Regras em todas as funções, por texto da mensagem/condição, nível, tipo e assunto. `None`
    sem a tabela de regras. Responde por exemplo "quais funções validam saldo?" sem ler o texto
    de cada uma.

    `topic` é a **chave** do assunto, já resolvida por quem chama (por exemplo
    `analysis.formula_topics.resolve_topic_key`, que também aceita o rótulo em português);
    exige `routine_rule_topics` (`php-map index-formulas --db <banco>`) e levanta
    `QueryError` com o comando, sem essa tabela. Cada item da resposta ganha `topic` (a chave, ou
    `None` sem `routine_rule_topics`), filtrado ou não."""
    if not has_rules(conn):
        return None
    has_topics = has_rule_topics(conn)
    if topic is not None and not has_topics:
        raise QueryError(
            "grafo sem o assunto das regras: rode `php-map index-formulas --db <banco>`"
        )
    join = (
        f" LEFT JOIN {_RULE_TOPICS_TABLE} t ON t.routine_id = r.routine_id AND t.seq = r.seq"
        if has_topics
        else ""
    )
    clauses: list[str] = []
    params: list[object] = []
    if query:
        clauses.append("(r.message LIKE ? OR r.condition LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like])
    if kind is not None:
        clauses.append("r.kind = ?")
        params.append(kind)
    if level is not None:
        clauses.append("r.level = ?")
        params.append(level)
    if topic is not None:
        clauses.append("t.topic = ?")
        params.append(topic)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    from_ = f"{RULES_TABLE} r{join}"
    total = int(conn.execute(f"SELECT count(*) FROM {from_} {where}", params).fetchone()[0])  # noqa: S608
    cols = ", ".join(f"r.{name.strip()}" for name in _RULES_COLUMNS.split(","))
    topic_col = "t.topic" if has_topics else "NULL"
    rows = conn.execute(
        f"SELECT {cols}, {topic_col} FROM {from_} {where} "  # noqa: S608
        "ORDER BY r.routine_id, r.seq LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    items = []
    for row in rows:
        *rule_row, topic_value = row
        items.append({"routine_id": rule_row[0], **_rule_of(rule_row), "topic": topic_value})
    return {"total": total, "items": items, "truncated": offset + len(rows) < total}


# ------------------------------------------------------------------ campos e consultas
#
# `routine_queries` (uma linha por instrução SQL) e `routine_fields` (uma linha por consulta ×
# tabela × coluna × papel): o que `ingest/sql_fields.py` acha no texto de `routine_sources`.
# Mesma convenção de linha (1 = CREATE). Leitores toleram a ausência das tabelas.

QUERIES_TABLE = "routine_queries"
FIELDS_TABLE = "routine_fields"
_QUERIES_SCHEMA = f"""
DROP TABLE IF EXISTS {QUERIES_TABLE};
CREATE TABLE {QUERIES_TABLE} (
    routine_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    text TEXT NOT NULL,
    tables TEXT NOT NULL,
    source TEXT,
    confidence REAL,
    PRIMARY KEY (routine_id, seq)
);
CREATE INDEX idx_{QUERIES_TABLE}_routine ON {QUERIES_TABLE}(routine_id);
"""
_FIELDS_SCHEMA = f"""
DROP TABLE IF EXISTS {FIELDS_TABLE};
CREATE TABLE {FIELDS_TABLE} (
    routine_id TEXT NOT NULL,
    query_seq INTEGER NOT NULL,
    table_id TEXT NOT NULL,
    column_name TEXT NOT NULL,
    role TEXT NOT NULL,
    line INTEGER NOT NULL,
    confidence REAL NOT NULL,
    source TEXT NOT NULL
);
CREATE INDEX idx_{FIELDS_TABLE}_table_column ON {FIELDS_TABLE}(table_id, column_name);
CREATE INDEX idx_{FIELDS_TABLE}_routine ON {FIELDS_TABLE}(routine_id);
"""
_QUERIES_COLUMNS = "routine_id, seq, kind, line, end_line, text, tables, source, confidence"
_FIELDS_COLUMNS = "routine_id, query_seq, table_id, column_name, role, line, confidence, source"


@dataclass(frozen=True, slots=True)
class QueryRow:
    """Uma instrução SQL de uma função, pronta para gravar (`seq`/`line`/`end_line` vêm de quem
    separou as instruções; `facts`, de `sql_fields.analyze_statement`)."""

    routine_id: str
    seq: int
    line: int
    end_line: int
    facts: "StatementFacts"


def write_fields(db_path: Path, rows: Sequence[QueryRow]) -> float:
    """Recria `routine_queries` e `routine_fields` numa transação. Devolve os segundos gastos."""
    if not db_path.is_file():
        raise StoreError(f"banco do grafo inexistente: {db_path}. Rode o scan primeiro.")
    started = time.perf_counter()
    query_rows = [
        (
            r.routine_id,
            r.seq,
            r.facts.kind,
            r.line,
            r.end_line,
            r.facts.text,
            json.dumps(list(r.facts.tables), ensure_ascii=False),
            r.facts.source,
            r.facts.confidence,
        )
        for r in rows
    ]
    field_rows = [
        (r.routine_id, r.seq, f.table, f.column, f.role, r.line, f.confidence, f.source)
        for r in rows
        for f in r.facts.fields
    ]
    try:
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executescript("BEGIN;" + _QUERIES_SCHEMA + _FIELDS_SCHEMA)
            conn.executemany(
                f"INSERT INTO {QUERIES_TABLE} ({_QUERIES_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                query_rows,
            )
            conn.executemany(
                f"INSERT INTO {FIELDS_TABLE} ({_FIELDS_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                field_rows,
            )
    except sqlite3.Error as exc:
        raise StoreError(f"campos e consultas não gravados em {db_path}: {exc}") from exc
    return round(time.perf_counter() - started, 2)


def has_fields(conn: sqlite3.Connection) -> bool:
    """O banco tem as tabelas de campos (grafos antigos não têm)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (QUERIES_TABLE,)
    ).fetchone()
    return row is not None


def _query_of(row: Sequence[object]) -> dict[str, Any]:
    routine_id, seq, kind, line, end_line, text, tables, source, confidence = row
    return {
        "seq": int(str(seq)),
        "kind": str(kind),
        "line": int(str(line)),
        "end_line": int(str(end_line)),
        "text": str(text),
        "tables": json.loads(str(tables)),
        "source": str(source) if source is not None else None,
        "confidence": float(str(confidence)) if confidence is not None else None,
    }


def read_queries(
    conn: sqlite3.Connection, routine_id: str, *, limit: int = 200, offset: int = 0
) -> dict[str, Any] | None:
    """As consultas de uma função, paginadas, com as colunas por papel de cada uma. `None` se o
    banco não tem as tabelas."""
    if not has_fields(conn):
        return None
    total = int(
        conn.execute(
            f"SELECT count(*) FROM {QUERIES_TABLE} WHERE routine_id = ?", (routine_id,)
        ).fetchone()[0]
    )
    rows = conn.execute(
        f"SELECT {_QUERIES_COLUMNS} FROM {QUERIES_TABLE} WHERE routine_id = ? "
        "ORDER BY seq LIMIT ? OFFSET ?",
        (routine_id, limit, offset),
    ).fetchall()
    items = []
    for row in rows:
        query = _query_of(row)
        field_rows = conn.execute(
            f"SELECT {_FIELDS_COLUMNS} FROM {FIELDS_TABLE} "
            "WHERE routine_id = ? AND query_seq = ? ORDER BY table_id, column_name, role",
            (routine_id, query["seq"]),
        ).fetchall()
        query["fields"] = [
            {
                "table": f[2],
                "column": f[3],
                "role": f[4],
                "line": int(str(f[5])),
                "confidence": float(str(f[6])),
                "source": str(f[7]),
            }
            for f in field_rows
        ]
        items.append(query)
    return {"total": total, "items": items, "truncated": offset + len(rows) < total}


def column_usage(
    conn: sqlite3.Connection,
    table_id_: str,
    column: str,
    *,
    role: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any] | None:
    """As funções que usam uma coluna de uma tabela (lê, grava, filtra…), com a linha e o texto
    da consulta. `None` se o banco não tem as tabelas."""
    if not has_fields(conn):
        return None
    clauses = ["table_id = ?", "column_name = ?"]
    params: list[object] = [table_id_, column.lower()]
    if role is not None:
        clauses.append("role = ?")
        params.append(role)
    where = " AND ".join(clauses)
    total = int(
        conn.execute(f"SELECT count(*) FROM {FIELDS_TABLE} WHERE {where}", params).fetchone()[0]
    )
    rows = conn.execute(
        f"SELECT routine_id, query_seq, role, line, confidence, source FROM {FIELDS_TABLE} "
        f"WHERE {where} ORDER BY routine_id, query_seq LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    items = []
    for routine_id, query_seq, use_role, line, confidence, source in rows:
        query_row = conn.execute(
            f"SELECT text FROM {QUERIES_TABLE} WHERE routine_id = ? AND seq = ?",
            (routine_id, query_seq),
        ).fetchone()
        items.append(
            {
                "routine_id": routine_id,
                "role": use_role,
                "line": int(str(line)),
                "confidence": float(str(confidence)),
                "source": source,
                "text": str(query_row[0]) if query_row else "",
            }
        )
    return {"total": total, "items": items, "truncated": offset + len(rows) < total}


# ------------------------------------------------------------ nome ambíguo por repasse
#
# Nome que casa com várias assinaturas da mesma família (mesmo `schema`/`name`) pode resolver
# sozinho quando as sobrecargas só **repassam** para uma delas (`fc_issqn(8 args)` → `(9 args)` →
# `(11 args)`, que faz o trabalho de verdade). A evidência é a aresta `CALLS_RPC` que já existe
# entre as sobrecargas (`SQL_AST`, confiança 1.0): nenhuma heurística nova, só ler o grafo.


@dataclass(frozen=True, slots=True)
class RoutineResolution:
    """O resultado de tentar resolver um nome ambíguo pela cadeia de repasse entre sobrecargas.

    `resolved_by`: `"unico"` (o nome já casou uma assinatura só, sem precisar de família),
    `"repasse"` (família com um destino único que todos os outros alcançam), `"ambiguo"` (mais de
    uma assinatura e o repasse não decide) ou `"nenhum"` (nome não encontrado).
    """

    chosen: RoutineSource | None
    candidates: tuple[RoutineSource, ...]
    resolved_by: str
    chain: tuple[str, ...] = ()
    delegates: dict[str, str] = field(default_factory=dict)


def arg_count(node_id: str) -> int:
    """Número de argumentos a partir do ID `schema.nome(tipos)` (vazio = zero)."""
    args = node_id.partition("(")[2].rpartition(")")[0].strip()
    return 0 if not args else args.count(",") + 1


def _join_pt(items: Sequence[str]) -> str:
    """`"a"`, `"a e b"` ou `"a, b e c"` — lista em português, sem vírgula antes do último `e`."""
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " e " + items[-1]


def resolve_family(conn: sqlite3.Connection, name: str) -> RoutineResolution:
    """Resolve um nome possivelmente ambíguo pela cadeia de repasse entre sobrecargas da mesma
    família (mesmo `schema`/`name` em `routine_sources`), por `CALLS_RPC` **internas** à família
    (um membro chamando outro membro).

    Regra: o destino é o **único** membro sem aresta
    interna de saída; resolve só se existir exatamente um destino **e** todos os outros membros
    chegarem a ele (sem ciclo). Em ramificação (um membro com mais de uma aresta interna), usa a
    de menor id. `chain` vai do membro de menos argumentos até o destino.

    Id completo ou `schema.nome(args)` que já casa uma única assinatura devolve `"unico"` direto:
    `find_routines` cuida disso, sem olhar a família (o usuário pediu aquela assinatura)."""
    candidates = find_routines(conn, name)
    if not candidates:
        return RoutineResolution(None, (), "nenhum")
    if len(candidates) == 1:
        return RoutineResolution(candidates[0], tuple(candidates), "unico")
    ids = [c.node_id for c in candidates]
    by_id = {c.node_id: c for c in candidates}
    marks = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT src, dst FROM edges WHERE type = 'CALLS_RPC' "  # noqa: S608
        f"AND src IN ({marks}) AND dst IN ({marks}) ORDER BY src, dst",
        (*ids, *ids),
    ).fetchall()
    out_edges: dict[str, list[str]] = {}
    for src, dst in rows:
        if src != dst:
            out_edges.setdefault(str(src), []).append(str(dst))
    delegates = {src: dsts[0] for src, dsts in out_edges.items()}
    ambiguous = RoutineResolution(None, tuple(candidates), "ambiguo", (), dict(delegates))
    sinks = [i for i in ids if i not in out_edges]
    if len(sinks) != 1:
        return ambiguous
    dest = sinks[0]
    chain_from: dict[str, tuple[str, ...]] = {dest: (dest,)}

    def reach(node: str, visiting: frozenset[str]) -> tuple[str, ...] | None:
        cached = chain_from.get(node)
        if cached is not None:
            return cached
        if node in visiting:
            return None  # ciclo: nunca chega ao destino por este caminho
        nxt = delegates.get(node)
        if nxt is None:
            return None  # outro membro sem saída: não é o destino único já achado
        sub = reach(nxt, visiting | {node})
        if sub is None:
            return None
        result = (node, *sub)
        chain_from[node] = result
        return result

    for node_id in ids:
        if reach(node_id, frozenset()) is None:
            return ambiguous
    start = min(ids, key=arg_count)
    return RoutineResolution(
        by_id[dest], tuple(candidates), "repasse", chain_from[start], delegates
    )


def describe_repasse_text(name: str, chosen_id: str, other_ids: Sequence[str]) -> str:
    """A mesma frase de `describe_repasse`, a partir só dos ids (para quem guarda a resolução
    como strings, ex. `CalcDossier`, em vez da `RoutineResolution` inteira)."""
    counts = sorted({arg_count(i) for i in other_ids})
    counts_text = _join_pt([str(n) for n in counts])
    singular = len(other_ids) == 1
    verb = "repassa" if singular else "repassam"
    article = "a" if singular else "as"
    noun = "argumento" if counts == [1] else "argumentos"
    signature = chosen_id.partition(":")[2] or chosen_id
    return (
        f"`{name}` tem {len(other_ids) + 1} assinaturas; {article} de "
        f"{counts_text} {noun} só {verb} para esta (`{signature}`)."
    )


def describe_repasse(resolution: RoutineResolution) -> str:
    """Frase em português para abrir o dossiê/ficha quando `resolved_by == "repasse"`: diz quantas
    assinaturas há e que as outras só repassam para a escolhida."""
    chosen = resolution.chosen
    if chosen is None or resolution.resolved_by != "repasse":
        raise ValueError("describe_repasse exige resolved_by == 'repasse'")
    others = [c for c in resolution.candidates if c.node_id != chosen.node_id]
    return describe_repasse_text(chosen.name, chosen.node_id, [c.node_id for c in others])
