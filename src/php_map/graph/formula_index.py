"""Índice de fórmulas e regras por assunto, em todas as funções da base.

A IA responde «quais funções calculam juros?» ou «quais regras falam de multa?» numa chamada só,
sem ler função por função. Duas tabelas derivadas (sem nó e sem aresta nova, como `routine_rules`
e `php_field_writes`):

- `routine_formulas`: uma linha por `Formula` de `analysis.formulas.extract_formulas` (reusa a
  extração, não duplica), com o assunto (`analysis.formula_topics`) que cada fórmula já traz;
- `routine_rule_topics`: uma linha por `routine_rules`, com o assunto achado na mensagem ou,
  senão, na condição.

`build_formula_index` roda no scan (`pipeline.py`, passo `formula_index`) e no comando
`php-map index-formulas --db <banco>`, para mapas antigos. A busca não pode depender só do
assunto (79% das fórmulas reais são «outros»): `search_formulas` também casa termo livre com o
destino, a expressão legível, o rótulo da coluna gravada e o nome da função.
"""

import json
import sqlite3
import time
import unicodedata
from collections.abc import Sequence
from typing import Any

from php_map.analysis import formula_topics as topics
from php_map.analysis import formulas as formulas_module
from php_map.errors import QueryError, StoreError
from php_map.graph import routines as routine_store

FORMULAS_TABLE = "routine_formulas"
RULE_TOPICS_TABLE = "routine_rule_topics"
MAX_EXPRESSION = 300  # corte de `expression`/`readable` (a fórmula inteira já está no dossiê)
MAX_CALLS = 20
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_EXAMPLES = 3  # fórmulas de exemplo por função em `group_by="routine"`

_SCHEMA = f"""
DROP TABLE IF EXISTS {FORMULAS_TABLE};
CREATE TABLE {FORMULAS_TABLE} (
    routine_id TEXT NOT NULL,
    line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    target_column TEXT,
    column_label TEXT,
    expression TEXT NOT NULL,
    readable TEXT NOT NULL,
    topic TEXT NOT NULL,
    topic_by TEXT NOT NULL,
    topic_evidence TEXT NOT NULL,
    calls TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'SQL_AST',
    confidence REAL NOT NULL
);
CREATE INDEX idx_{FORMULAS_TABLE}_topic ON {FORMULAS_TABLE}(topic);
CREATE INDEX idx_{FORMULAS_TABLE}_routine ON {FORMULAS_TABLE}(routine_id);

DROP TABLE IF EXISTS {RULE_TOPICS_TABLE};
CREATE TABLE {RULE_TOPICS_TABLE} (
    routine_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    topic TEXT NOT NULL,
    topic_by TEXT NOT NULL,
    evidence TEXT NOT NULL,
    PRIMARY KEY (routine_id, seq)
);
CREATE INDEX idx_{RULE_TOPICS_TABLE}_topic ON {RULE_TOPICS_TABLE}(topic);
"""
_FORMULA_COLUMNS = (
    "routine_id, line, end_line, kind, target, target_column, column_label, expression, "
    "readable, topic, topic_by, topic_evidence, calls, source, confidence"
)
_RULE_TOPIC_COLUMNS = "routine_id, seq, topic, topic_by, evidence"

NO_FORMULA_INDEX = (
    "grafo sem o índice de fórmulas por assunto: rode `php-map index-formulas --db <banco>`"
)


# --------------------------------------------------------------------------- montagem (scan)


def has_formula_index(conn: sqlite3.Connection) -> bool:
    """O banco tem a tabela (mapas antigos não têm)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (FORMULAS_TABLE,)
    ).fetchone()
    return row is not None


def rule_topics_available(conn: sqlite3.Connection) -> bool:
    """A tabela de assunto das regras existe (gravada junto de `routine_formulas`)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (RULE_TOPICS_TABLE,)
    ).fetchone()
    return row is not None


def _target_column(
    res: formulas_module.Resolver, formula: formulas_module.Formula
) -> tuple[str | None, str | None]:
    """`(schema.tabela.coluna, rótulo do dicionário)` quando o destino da fórmula é uma coluna
    gravada (`UPDATE SET`/`INSERT`); `(None, None)` nos demais casos (mesma regra de
    `_column_label` do dossiê, sem os hints de escopo — pista, não garantia)."""
    if formula.kind not in ("update_set", "insert_value") or "." not in formula.target:
        return None, None
    rel, _sep, column = formula.target.partition(".")
    table_id = res.table_for(rel, None, ())
    if table_id is None:
        return None, None
    label = res.column_info(table_id, column)[0]
    return f"{table_id.partition(':')[2]}.{column}", label


def _calls_of(formula: formulas_module.Formula) -> list[str]:
    """Nomes das funções chamadas na expressão (entradas do tipo `function`)."""
    seen: dict[str, None] = {}
    for item in formula.inputs:
        if item.kind == "function":
            seen.setdefault(item.name, None)
    return list(seen)[:MAX_CALLS]


def _cut(text: str, limit: int = MAX_EXPRESSION) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _formula_row(
    res: formulas_module.Resolver, formula: formulas_module.Formula
) -> tuple[Any, ...]:
    topic = formula.topic or topics.topic_of(formula)
    target_column, column_label = _target_column(res, formula)
    return (
        formula.routine_id,
        formula.line,
        formula.end_line,
        formula.kind,
        formula.target,
        target_column,
        column_label,
        _cut(formula.expression),
        _cut(formula.readable),
        topic.key,
        topic.by,
        topic.evidence,
        json.dumps(_calls_of(formula), ensure_ascii=False),
        "SQL_AST",
        1.0,
    )


def _rule_topic(message: str | None, condition: str | None) -> tuple[str, str, str]:
    """`(topic, topic_by, evidence)` de uma regra: `_match_name` na mensagem e, senão, na
    condição (mesma regra de nome; sem acento, minúsculo); senão «outros»."""
    for text, by in ((message, "mensagem"), (condition, "condicao")):
        if not text:
            continue
        hit = topics._match_name(text)  # noqa: SLF001 (mesma regra do assunto das fórmulas, não duplicada)
        if hit is not None:
            key, piece = hit
            return key, by, f"'{piece}' na {by}"
    return topics.OTHERS, "nenhum", ""


def build_formula_index(conn: sqlite3.Connection) -> dict[str, Any]:
    """Grava `routine_formulas` e `routine_rule_topics` numa transação e devolve as contagens
    do resumo (scan e `php-map index-formulas`).

    Lê `routine_sources` e roda `analysis.formulas.extract_formulas` (por um único `Resolver`
    reaproveitado no lote inteiro — reabrir um por função é lento demais, ver a lição da tarefa)
    com o assunto que a própria extração já calcula (`analysis.formula_topics.topic_of`, com o
    rótulo do dicionário da coluna gravada). Classifica as regras (`routine_rules`, se houver) e
    grava as duas tabelas juntas. Função cujo texto não gera fórmula alguma por erro de
    parsing/leitura: conta em `errors` e segue (nunca aborta o lote)."""
    if not routine_store.has_routines(conn):
        raise QueryError(
            "este mapa não tem o texto das funções da base: rode o scan com --schema-db (ou "
            "--schema) para gravar routine_sources"
        )
    started = time.perf_counter()
    res = formulas_module.Resolver(conn)
    routine_ids = [
        str(row[0])
        for row in conn.execute(f"SELECT node_id FROM {routine_store.TABLE} ORDER BY node_id")
    ]
    formula_rows: list[tuple[Any, ...]] = []
    by_topic: dict[str, int] = {}
    functions_with_formula = 0
    errors = 0
    for routine_id in routine_ids:
        try:
            model = res.model(routine_id)
            found = list(model.formulas()) if model is not None else None
        except Exception:  # nunca aborta o lote por uma função ruim
            errors += 1
            continue
        if found is None:
            errors += 1
            continue
        if found:
            functions_with_formula += 1
        for formula in found:
            row = _formula_row(res, formula)
            formula_rows.append(row)
            by_topic[row[9]] = by_topic.get(row[9], 0) + 1

    rule_rows: list[tuple[Any, ...]] = []
    rules_with_topic = 0
    if routine_store.has_rules(conn):
        for routine_id, seq, message, condition in conn.execute(
            f"SELECT routine_id, seq, message, condition FROM {routine_store.RULES_TABLE}"  # noqa: S608
        ):
            key, by, evidence = _rule_topic(message, condition)
            if key != topics.OTHERS:
                rules_with_topic += 1
            rule_rows.append((routine_id, seq, key, by, evidence))

    try:
        with conn:
            conn.executescript("BEGIN;" + _SCHEMA)
            conn.executemany(
                f"INSERT INTO {FORMULAS_TABLE} ({_FORMULA_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                formula_rows,
            )
            conn.executemany(
                f"INSERT INTO {RULE_TOPICS_TABLE} ({_RULE_TOPIC_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
                rule_rows,
            )
    except sqlite3.Error as exc:
        raise StoreError(f"índice de fórmulas por assunto não gravado: {exc}") from exc
    return {
        "functions": len(routine_ids),
        "formulas": len(formula_rows),
        "functions_with_formula": functions_with_formula,
        "by_topic": dict(sorted(by_topic.items())),
        "rules": len(rule_rows),
        "rules_with_topic": rules_with_topic,
        "errors": errors,
        "seconds": round(time.perf_counter() - started, 2),
    }


# --------------------------------------------------------------------------- busca (MCP/CLI)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def _topic_keys_matching(query: str) -> list[str]:
    """Chaves de assunto cujo rótulo (ou chave) contém o termo livre — «termo livre casa com
    assunto (chave e rótulo)»."""
    norm = _normalize(query.strip())
    if not norm:
        return []
    found = []
    for key, label in topics.LABELS.items():
        if norm in key or norm in _normalize(label):
            found.append(key)
    return found


def _resolve_topic(topic: str) -> str:
    key = topics.resolve_topic_key(topic)
    if key is None:
        options = ", ".join(sorted(topics.LABELS))
        raise QueryError(f"assunto desconhecido: {topic!r}. Use uma das chaves: {options}")
    return key


def _has_analytics_routines(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'analytics_routines'"
    ).fetchone()
    return row is not None


def _where(
    conn: sqlite3.Connection,
    *,
    query: str | None,
    topic: str | None,
    module: str | None,
    routine: str | None,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if routine:
        clauses.append("routine_id = ?")
        params.append(routine)
    if topic:
        clauses.append("topic = ?")
        params.append(_resolve_topic(topic))
    if query:
        like = f"%{_escape_like(query)}%"
        ors = [
            "target LIKE ? ESCAPE '\\'",
            "expression LIKE ? ESCAPE '\\'",
            "readable LIKE ? ESCAPE '\\'",
            "column_label LIKE ? ESCAPE '\\'",
            "routine_id LIKE ? ESCAPE '\\'",
        ]
        query_params: list[object] = [like, like, like, like, like]
        matching_topics = _topic_keys_matching(query)
        if matching_topics:
            ors.append(f"topic IN ({', '.join('?' for _ in matching_topics)})")
            query_params.extend(matching_topics)
        clauses.append("(" + " OR ".join(ors) + ")")
        params.extend(query_params)
    if module:
        if not _has_analytics_routines(conn):
            raise QueryError(
                "grafo sem o módulo das funções da base (analytics_routines): rode o scan com "
                "os agregados (sem --no-analytics)"
            )
        clauses.append("routine_id IN (SELECT routine_id FROM analytics_routines WHERE module = ?)")
        params.append(module)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _module_of(conn: sqlite3.Connection, routine_id: str) -> str | None:
    if not _has_analytics_routines(conn):
        return None
    row = conn.execute(
        "SELECT module FROM analytics_routines WHERE routine_id = ?", (routine_id,)
    ).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def _formula_item(row: Sequence[object]) -> dict[str, Any]:
    (
        routine_id, line, end_line, kind, target, target_column, column_label, expression,
        readable, topic, topic_by, topic_evidence, calls, _source, confidence,
    ) = row  # fmt: skip
    return {
        "routine_id": str(routine_id),
        "line": int(str(line)),
        "end_line": int(str(end_line)),
        "kind": str(kind),
        "target": str(target),
        "target_column": str(target_column) if target_column is not None else None,
        "column_label": str(column_label) if column_label is not None else None,
        "expression": str(expression),
        "readable": str(readable),
        "topic": str(topic),
        "topic_label": topics.LABELS.get(str(topic), str(topic)),
        "topic_by": str(topic_by),
        "topic_evidence": str(topic_evidence),
        "calls": json.loads(str(calls)),
        "confidence": float(str(confidence)),
    }


def search_formulas(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    topic: str | None = None,
    module: str | None = None,
    routine: str | None = None,
    group_by: str = "routine",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any] | None:
    """Fórmulas de todas as funções da base, por assunto e/ou termo livre, com o módulo e uma
    função para limitar. `None` sem a tabela (quem chama decide a mensagem). `topic` aceita a
    chave ou o rótulo em português.

    `group_by="routine"` (padrão): por função, a contagem das fórmulas que casam, até
    `MAX_EXAMPLES` de exemplo (`linha, destino, readable`) e o módulo — ordenado pela contagem,
    maior primeiro (como os exemplos reais da tarefa). `group_by="formula"`: as fórmulas em si,
    paginadas por linha."""
    if not has_formula_index(conn):
        return None
    where, params = _where(conn, query=query, topic=topic, module=module, routine=routine)
    if group_by == "formula":
        total = int(
            conn.execute(f"SELECT count(*) FROM {FORMULAS_TABLE} {where}", params).fetchone()[0]
        )
        rows = conn.execute(
            f"SELECT {_FORMULA_COLUMNS} FROM {FORMULAS_TABLE} {where} "
            "ORDER BY routine_id, line LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        items: list[dict[str, Any]] = [_formula_item(r) for r in rows]
        truncated = offset + len(rows) < total
        return {
            "group_by": "formula",
            "total": total,
            "items": items,
            "truncated": truncated,
            "next_offset": offset + len(rows) if truncated else None,
        }
    counts = conn.execute(
        f"SELECT routine_id, COUNT(*) c FROM {FORMULAS_TABLE} {where} "  # noqa: S608
        "GROUP BY routine_id ORDER BY c DESC, routine_id",
        params,
    ).fetchall()
    total_routines = len(counts)
    page = counts[offset : offset + limit]
    scoped_where = f"{where} AND routine_id = ?" if where else "WHERE routine_id = ?"
    routine_items: list[dict[str, Any]] = []
    for routine_id, count in page:
        scoped_params = (*params, routine_id)
        example_rows = conn.execute(
            f"SELECT line, target, readable FROM {FORMULAS_TABLE} {scoped_where} "
            "ORDER BY line LIMIT ?",
            (*scoped_params, MAX_EXAMPLES),
        ).fetchall()
        topic_rows = conn.execute(
            f"SELECT topic, COUNT(*) FROM {FORMULAS_TABLE} {scoped_where} "
            "GROUP BY topic ORDER BY 2 DESC",
            scoped_params,
        ).fetchall()
        routine_items.append(
            {
                "routine_id": str(routine_id),
                "total": int(count),
                "module": _module_of(conn, str(routine_id)),
                "by_topic": [
                    {"topic": str(k), "label": topics.LABELS.get(str(k), str(k)), "count": int(n)}
                    for k, n in topic_rows
                ],
                "examples": [
                    f"{line}: {target} = {readable}" for line, target, readable in example_rows
                ],
            }
        )
    truncated = offset + len(page) < total_routines
    return {
        "group_by": "routine",
        "total": total_routines,
        "items": routine_items,
        "truncated": truncated,
        "next_offset": offset + len(page) if truncated else None,
    }


WARNING = (
    "assunto é pista (pelo nome, pela chamada ou pelo rótulo); confira na fórmula com "
    "`formulas(routine=…)`"
)


def render_formula_search(result: dict[str, Any], *, topic: str | None, query: str | None) -> str:
    """Markdown curto da resposta de `search_formulas` (usado por `formula_search` no MCP)."""
    title = []
    if topic:
        title.append(f"assunto «{topic}»")
    if query:
        title.append(f"termo «{query}»")
    header = "## Fórmulas: " + (" e ".join(title) if title else "todas")
    if not result["items"]:
        return f"{header}\n\nNenhuma fórmula achada.\n"
    lines = [header, ""]
    if result["group_by"] == "routine":
        lines += ["| Função | Total | Módulo | Exemplos |", "|---|---|---|---|"]
        for item in result["items"]:
            name = item["routine_id"].partition(":")[2].partition("(")[0]
            examples = "; ".join(item["examples"]) or "—"
            lines.append(f"| {name} | {item['total']} | {item['module'] or '—'} | {examples} |")
    else:
        lines += ["| Função | Linha | Destino | Assunto | Fórmula |", "|---|---|---|---|---|"]
        for item in result["items"]:
            name = item["routine_id"].partition(":")[2].partition("(")[0]
            lines.append(
                f"| {name} | {item['line']} | {item['target']} | {item['topic_label']} | "
                f"{item['readable']} |"
            )
    unit = "funções" if result["group_by"] == "routine" else "fórmulas"
    lines += ["", f"Total: {result['total']} {unit}."]
    if result["truncated"]:
        lines.append(f"Mais resultados: `offset={result['next_offset']}`.")
    lines += ["", WARNING]
    return "\n".join(lines) + "\n"


__all__ = [
    "FORMULAS_TABLE",
    "NO_FORMULA_INDEX",
    "RULE_TOPICS_TABLE",
    "WARNING",
    "build_formula_index",
    "has_formula_index",
    "render_formula_search",
    "rule_topics_available",
    "search_formulas",
]
