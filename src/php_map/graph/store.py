"""Persistência do grafo em SQLite."""

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from php_map import fsperm
from php_map.errors import StoreError
from php_map.graph.builder import GraphBuilder, StoredEdge, add_nx_edge, add_nx_node, new_graph
from php_map.graph.model import EdgeSource, EdgeType

if TYPE_CHECKING:
    from php_map.graph.builder import KnowledgeGraph

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE meta  (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE nodes (id TEXT PRIMARY KEY, label TEXT NOT NULL, attrs TEXT NOT NULL);
CREATE TABLE edges (id INTEGER PRIMARY KEY, src TEXT NOT NULL, dst TEXT NOT NULL,
                    type TEXT NOT NULL, source TEXT NOT NULL, confidence REAL NOT NULL,
                    evidence TEXT NOT NULL, attrs TEXT NOT NULL);
CREATE INDEX ix_edges_src ON edges(src);
CREATE INDEX ix_edges_dst ON edges(dst);
CREATE INDEX ix_nodes_label ON nodes(label);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _meta_value(value: Any) -> str:
    return value if isinstance(value, str) else _dumps(value)


def save(builder: GraphBuilder, db_path: Path, meta: Mapping[str, Any]) -> None:
    """Recria o banco em `db_path`: grava em `<db>.tmp` e depois troca pelo definitivo.

    `meta` aceita qualquer valor; o que não for `str` é gravado como JSON. `schema_version`,
    `node_count` e `edge_count` são sempre definidos aqui.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)
    stats = builder.stats()
    rows_meta = {
        **{key: _meta_value(value) for key, value in meta.items()},
        "schema_version": SCHEMA_VERSION,
        "node_count": str(stats.nodes),
        "edge_count": str(stats.edges),
    }
    try:
        with closing(sqlite3.connect(tmp_path)) as conn:
            conn.executescript(_SCHEMA)
            with conn:  # uma transação
                conn.executemany("INSERT INTO meta VALUES (?, ?)", rows_meta.items())
                conn.executemany(
                    "INSERT INTO nodes VALUES (?, ?, ?)",
                    ((n.id, str(n.label), _dumps(n.attrs)) for n in builder.iter_nodes()),
                )
                conn.executemany(
                    "INSERT INTO edges (src, dst, type, source, confidence, evidence, attrs)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            e.src,
                            e.dst,
                            str(e.type),
                            str(e.source),
                            e.confidence,
                            _dumps(e.evidence),
                            _dumps(e.attrs),
                        )
                        for e in builder.iter_edges()
                    ),
                )
        # Conexão fechada antes do replace: no Windows, arquivo aberto não pode ser substituído.
        os.replace(tmp_path, db_path)
        fsperm.secure_file(db_path)
        fsperm.secure_dir(db_path.parent)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise StoreError(f"banco do grafo inexistente: {db_path}. Rode o scan primeiro.")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise StoreError(f"não foi possível abrir {db_path}: {exc}") from exc


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Conexão somente leitura ao banco do grafo (usada pela busca)."""
    return _connect(db_path)


def _read_meta(conn: sqlite3.Connection, db_path: Path) -> dict[str, str]:
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.Error as exc:
        raise StoreError(f"{db_path} não é um banco do php-map ({exc}).") from exc
    version = meta.get("schema_version")
    if version != SCHEMA_VERSION:
        raise StoreError(
            f"{db_path} tem schema_version {version!r}, esperado {SCHEMA_VERSION!r}:"
            " rode o scan novamente."
        )
    return meta


def load_meta(db_path: Path) -> dict[str, str]:
    """Metadados do scan como texto (valores não-string foram gravados em JSON)."""
    with closing(_connect(db_path)) as conn:
        return _read_meta(conn, db_path)


def _iter_edges(conn: sqlite3.Connection) -> Iterator[StoredEdge]:
    rows = conn.execute(
        "SELECT src, dst, type, source, confidence, evidence, attrs FROM edges ORDER BY id"
    )
    for src, dst, type_, source, confidence, evidence, attrs in rows:
        yield StoredEdge(
            src,
            dst,
            EdgeType(type_),
            EdgeSource(source),
            confidence,
            json.loads(evidence),
            json.loads(attrs),
        )


def load(db_path: Path) -> "KnowledgeGraph":
    """Carrega o grafo inteiro em `networkx.MultiDiGraph` (mesmo formato de `to_networkx`)."""
    with closing(_connect(db_path)) as conn:
        _read_meta(conn, db_path)
        graph = new_graph()
        try:
            for node_id, label, attrs in conn.execute(
                "SELECT id, label, attrs FROM nodes ORDER BY rowid"
            ):
                add_nx_node(graph, node_id, label, json.loads(attrs))
            for edge in _iter_edges(conn):
                add_nx_edge(graph, edge)
        except (sqlite3.Error, ValueError) as exc:
            raise StoreError(f"{db_path} corrompido: {exc}") from exc
        return graph
