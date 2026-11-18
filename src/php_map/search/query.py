"""Consulta ao índice de busca textual.

O FTS5 não tem stemmer de português: os termos passam por uma redução de sufixos simples e
são buscados por prefixo (`anulação` → `"anula"*`). Nomes compostos sem separador
(`anularempenho001`) são achados pela tabela `trigram` (substring).
"""

import json
import re
import sqlite3
import unicodedata
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from php_map.errors import StoreError
from php_map.graph import store
from php_map.graph.model import key_of, normalize_module_name
from php_map.search.index import FIELDS

SUFFIXES = (
    "coes",
    "cao",
    "soes",
    "sao",
    "mentos",
    "mento",
    "idades",
    "idade",
    "ados",
    "adas",
    "ado",
    "ada",
    "ar",
    "er",
    "ir",
    "es",
    "s",
)
MIN_STEM = 4
MIN_TRIGRAM = 3
WEIGHTS = (10.0, 6.0, 4.0, 2.0, 1.0, 1.5)  # name, catalog, labels, symbols, text, uses
NAME_BONUS = 5.0  # por termo que só casou como substring do nome (tabela trigram)
SNIPPET_TOKENS = 12
USES_PREFIX = "usa: "
NO_INDEX = "banco sem índice de busca: rode o scan de novo, sem --no-search"

_WORD = re.compile(r"[a-z0-9]+")
_LETTER = re.compile(r"[a-z]")


@dataclass(frozen=True, slots=True)
class SearchHit:
    node_id: str
    label: str
    modules: list[str]
    score: float  # maior = melhor (−bm25 + bônus do nome)
    matched: int  # termos casados
    field: str  # coluna do trecho (`name`, `catalog`, `labels`, `symbols`, `text`, `uses`)
    snippet: str
    next_command: str


def normalize_terms(text: str) -> list[str]:
    """Termos da consulta: minúsculas, sem acento, ≥ 2 caracteres com letra, sufixo reduzido.

    Só sobram `[a-z0-9]`, então a sintaxe do FTS5 (`"`, `-`, `:`, `*`) nunca chega ao MATCH.
    """
    plain = "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    ).lower()
    terms: list[str] = []
    for word in _WORD.findall(plain):
        if len(word) < 2 or not _LETTER.search(word):
            continue
        terms.append(_stem(word))
    return list(dict.fromkeys(terms))


def _stem(word: str) -> str:
    for suffix in SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= MIN_STEM:
            return word[: -len(suffix)]
    return word


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = store.connect_readonly(db_path)
    found = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE name IN ('search_docs', 'search_fts', "
        "'search_names')"
    ).fetchone()[0]
    if found != 3:
        conn.close()
        raise StoreError(NO_INDEX)
    return conn


def _fields(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Colunas do `search_fts` deste banco, na ordem. Um índice antigo não tem
    `uses`; o `bm25` exige um peso por coluna, então os pesos acompanham o que existe."""
    present = {row[1] for row in conn.execute("PRAGMA table_info(search_fts)")}
    return tuple(name for name in FIELDS if name in present)


def _rows(conn: sqlite3.Connection, table: str, expression: str) -> set[int]:
    return {row[0] for row in conn.execute(f"SELECT rowid FROM {table}(?)", (expression,))}


def search(
    db_path: Path,
    text: str,
    *,
    label: str | None = None,
    module: str | None = None,
    limit: int = 20,
    any_term: bool = False,
) -> list[SearchHit]:
    terms = normalize_terms(text)
    if not terms:
        return []
    with closing(_connect(db_path)) as conn:
        fts_by_term: list[set[int]] = []
        names_by_term: list[set[int]] = []
        for term in terms:
            fts_by_term.append(_rows(conn, "search_fts", f'"{term}"*'))
            names = _rows(conn, "search_names", f'"{term}"') if len(term) >= MIN_TRIGRAM else set()
            names_by_term.append(names)
        per_term = [f | n for f, n in zip(fts_by_term, names_by_term, strict=True)]
        candidates = set.union(*per_term) if any_term else set.intersection(*per_term)
        if not candidates:
            return []

        docs = {
            rowid: (node_id, doc_label, json.loads(modules))
            for rowid, node_id, doc_label, modules in conn.execute(
                "SELECT rowid, node_id, label, modules FROM search_docs"
                f" WHERE rowid IN ({','.join(str(r) for r in sorted(candidates))})"
            )
        }
        # Pelo nome normalizado: "Dívida Ativa", "dividaativa" e "empenho" valem.
        module_key = normalize_module_name(module) if module else None
        docs = {
            rowid: doc
            for rowid, doc in docs.items()
            if (label is None or doc[1] == label)
            and (module_key is None or module_key in (normalize_module_name(m) for m in doc[2]))
        }
        if not docs:
            return []

        expression = " OR ".join(f'"{term}"*' for term in terms)
        fields = _fields(conn)
        weights = ", ".join(str(w) for w in WEIGHTS[: len(fields)])
        bm25 = dict(
            conn.execute(
                f"SELECT rowid, bm25(search_fts, {weights}) FROM search_fts(?)", (expression,)
            ).fetchall()
        )
        ranked = []
        for rowid, (node_id, doc_label, modules) in docs.items():
            matched = sum(1 for rows in per_term if rowid in rows)
            # Bônus só para o termo que casou **apenas** pelo trigram (`empanulado` ← `anul`):
            # quando o prefixo já casou no FTS5, o termo está no bm25 e contaria duas vezes.
            by_name = sum(
                1
                for fts, names in zip(fts_by_term, names_by_term, strict=True)
                if rowid in names and rowid not in fts
            )
            score = -bm25.get(rowid, 0.0) + NAME_BONUS * by_name
            ranked.append((-matched, -score, node_id, rowid, doc_label, modules, matched, score))
        ranked.sort()

        hits = []
        for *_key, node_id, rowid, doc_label, modules, matched, score in ranked[:limit]:
            field, snippet = _snippet(conn, rowid, expression, fields)
            hits.append(
                SearchHit(
                    node_id,
                    doc_label,
                    modules,
                    round(score, 3),
                    matched,
                    field,
                    snippet,
                    _next_command(conn, node_id, doc_label, rowid),
                )
            )
        return hits


def _snippet(
    conn: sqlite3.Connection, rowid: int, expression: str, fields: Sequence[str] = FIELDS
) -> tuple[str, str]:
    """Trecho da coluna de maior peso que casou; só pelo trigram → o nome, sem marcadores."""
    for column, name in enumerate(fields):
        row = conn.execute(
            f"SELECT snippet(search_fts, {column}, '[', ']', '…', {SNIPPET_TOKENS})"
            " FROM search_fts(?) WHERE rowid = ?",
            (expression, rowid),
        ).fetchone()
        if row is not None and "[" in (row[0] or ""):
            return name, (USES_PREFIX + str(row[0]) if name == "uses" else str(row[0]))
    row = conn.execute("SELECT name FROM search_names WHERE rowid = ?", (rowid,)).fetchone()
    return "name", str(row[0]).splitlines()[0] if row else ""


def _next_command(conn: sqlite3.Connection, node_id: str, label: str, rowid: int) -> str:
    key = key_of(node_id)
    if label == "File":
        return f"php-map trace --entry {key}"
    if label == "Table":
        schema, _sep, table = key.partition(".")
        return f"php-map impact --table {table} --schema {schema}"
    if label in ("Endpoint", "Route"):  # o `trace` parte de arquivo; sem comando próprio aqui
        return ""
    row = conn.execute("SELECT symbols FROM search_fts WHERE rowid = ?", (rowid,)).fetchone()
    files = (row[0] or "").split("\n") if row else []
    return f"php-map trace --entry {files[0]}" if files and files[0] else ""
