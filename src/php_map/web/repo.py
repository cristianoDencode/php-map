"""Consultas SQL da API. Sem ORM e sem carregar o grafo inteiro.

`trace` e `impact` reproduzem `analysis.trace`/`analysis.impact` sobre a BFS em SQL
(`web.subgraph.walk_sql`), com os mesmos números de resumo que a CLI imprime.
"""

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast

from php_map.analysis import LEAF_LABELS, TABLE_OPS, Step, require_node
from php_map.analysis.aggregate import NO_MODULE
from php_map.analysis.impact import (
    DaoTouch,
    group_dao_users,
    is_dao_method,
    table_target,
)
from php_map.errors import QueryError, StoreError
from php_map.graph import routines
from php_map.graph.model import file_id, table_id
from php_map.report.render import mermaid as render_mermaid
from php_map.web.subgraph import SqlWalk, batches, placeholders, walk_sql

MAX_LIMIT = 500
MAX_NODES = 2000
MAX_DEPTH = 10
NO_ANALYTICS = "banco sem agregados: rode o scan de novo, sem --no-analytics"
_RPC_FILE = re.compile(r"\.RPC\.php$", re.IGNORECASE)
_OPS = tuple(TABLE_OPS)


# --------------------------------------------------------------------------- linhas


@dataclass(frozen=True, slots=True)
class NodeRow:
    id: str
    label: str
    attrs: dict[str, Any]
    modules: list[str]


@dataclass(frozen=True, slots=True)
class EdgeRow:
    id: int
    src: str
    dst: str
    type: str
    source: str
    confidence: float
    evidence: dict[str, Any]
    attrs: dict[str, Any]


@dataclass(slots=True)
class SubgraphData:
    root: str
    nodes: list[NodeRow]
    edges: list[EdgeRow]
    depth_of: dict[str, int]
    truncated: bool
    summary: dict[str, Any]
    walk: SqlWalk
    priority: list[str] = field(default_factory=list)  # folhas que o Mermaid mostra primeiro
    dao_users: list[DaoTouch] = field(default_factory=list)  # só no `impact`


class Clamp:
    """Corta os parâmetros acima dos limites e anota o que cortou."""

    def __init__(self) -> None:
        self.clamped: list[str] = []

    def __call__(self, name: str, value: int, maximum: int, minimum: int = 0) -> int:
        if value > maximum:
            self.clamped.append(f"{name}: {value} → {maximum}")
            return maximum
        return max(minimum, value)


def _edge(row: Sequence[Any]) -> EdgeRow:
    edge_id, src, dst, type_, source, confidence, evidence, attrs = row
    return EdgeRow(
        edge_id, src, dst, type_, source, confidence, json.loads(evidence), json.loads(attrs)
    )


_EDGE_COLUMNS = "id, src, dst, type, source, confidence, evidence, attrs"


# --------------------------------------------------------------------------- nós


def nodes(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, NodeRow]:
    """Nós existentes, com os módulos (`BELONGS_TO`), em lotes."""
    wanted = list(dict.fromkeys(ids))
    rows: dict[str, tuple[str, dict[str, Any]]] = {}
    modules: dict[str, list[str]] = {}
    for chunk in batches(wanted):
        marks = placeholders(len(chunk))
        for node_id, label, attrs in conn.execute(
            f"SELECT id, label, attrs FROM nodes WHERE id IN ({marks})", tuple(chunk)
        ):
            rows[node_id] = (label, json.loads(attrs))
        for src, dst in conn.execute(
            f"SELECT src, dst FROM edges WHERE type = 'BELONGS_TO' AND src IN ({marks})"
            " ORDER BY src, dst",
            tuple(chunk),
        ):
            modules.setdefault(src, []).append(dst)
    result: dict[str, NodeRow] = {}
    for node_id in wanted:
        if node_id in rows:
            label, attrs = rows[node_id]
            result[node_id] = NodeRow(node_id, label, attrs, sorted(set(modules.get(node_id, []))))
    return result


def node(conn: sqlite3.Connection, node_id: str) -> NodeRow | None:
    return nodes(conn, [node_id]).get(node_id)


MAX_ROUTINE_LINES = 500


@dataclass(frozen=True, slots=True)
class RoutinePage:
    """Uma página de linhas do texto de uma função da base."""

    node_id: str
    schema: str
    name: str
    language: str
    generated: bool
    read_at: str
    sha1: str
    total_lines: int
    start_line: int
    end_line: int
    lines: list[str]
    truncated: bool


def has_routine_texts(conn: sqlite3.Connection) -> bool:
    """O banco foi escaneado com o texto das funções (`routine_sources` existe)."""
    return routines.has_routines(conn)


def routine_page(
    conn: sqlite3.Connection, node_id: str, offset: int = 1, limit: int = 200
) -> RoutinePage | None:
    """O texto da função em linhas (sem a linha vazia final); `None` se não tem texto."""
    row = routines.read_routine(conn, node_id)
    if row is None:
        return None
    lines = routines.split_lines(row.ddl)
    start = min(max(1, offset), len(lines))
    window = lines[start - 1 : start - 1 + max(1, min(limit, MAX_ROUTINE_LINES))]
    end = start + len(window) - 1
    return RoutinePage(
        node_id=row.node_id,
        schema=row.schema,
        name=row.name,
        language=row.language,
        generated=row.generated,
        read_at=row.read_at,
        sha1=row.sha1,
        total_lines=len(lines),
        start_line=start,
        end_line=end,
        lines=window,
        truncated=start > 1 or end < len(lines),
    )


def memberships(conn: sqlite3.Connection, node_id: str) -> list[dict[str, Any]]:
    """Módulos do nó com a fonte e a confiança da aresta, e o nome do módulo."""
    rows = conn.execute(
        "SELECT e.dst, e.source, e.confidence, json_extract(n.attrs, '$.name') FROM edges e"
        " LEFT JOIN nodes n ON n.id = e.dst WHERE e.type = 'BELONGS_TO' AND e.src = ?"
        " ORDER BY e.confidence DESC, e.dst",
        (node_id,),
    )
    return [
        {"module": dst, "name": name or dst.partition(":")[2], "source": src, "confidence": conf}
        for dst, src, conf, name in rows
    ]


def neighbors(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    incoming: bool,
    types: Sequence[str] | None,
    min_confidence: float,
    include_infra: bool,
    limit: int,
    offset: int,
) -> tuple[int, list[EdgeRow], dict[str, NodeRow]]:
    """Página de arestas (saindo ou chegando), por tipo, vizinho e confiança."""
    key, other = ("dst", "src") if incoming else ("src", "dst")
    where = [f"e.{key} = ?", "e.confidence >= ?"]
    params: list[Any] = [node_id, min_confidence]
    if types:
        where.append(f"e.type IN ({placeholders(len(types))})")
        params.extend(types)
    if not include_infra:
        where.append("coalesce(json_extract(n.attrs, '$.is_infra'), 0) = 0")
    base = f"FROM edges e JOIN nodes n ON n.id = e.{other} WHERE {' AND '.join(where)}"
    total = conn.execute(f"SELECT count(*) {base}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT e.id, e.src, e.dst, e.type, e.source, e.confidence, e.evidence, e.attrs {base}"
        f" ORDER BY e.type, e.{other}, e.confidence DESC, e.id LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    edges = [_edge(row) for row in rows]
    found = nodes(conn, [e.src if incoming else e.dst for e in edges])
    return total, edges, found


def edges_by_id(conn: sqlite3.Connection, ids: Iterable[int]) -> list[EdgeRow]:
    wanted = list(dict.fromkeys(ids))
    found: dict[int, EdgeRow] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        for row in conn.execute(
            f"SELECT {_EDGE_COLUMNS} FROM edges WHERE id IN ({placeholders(len(chunk))})",
            tuple(chunk),
        ):
            found[row[0]] = _edge(row)
    return [found[i] for i in wanted if i in found]


def require(conn: sqlite3.Connection, node_id: str, label: str, what: str) -> None:
    """`QueryError` com sugestões (`analysis.require_node`) se o nó não existe."""
    if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone():
        return
    candidates = [r[0] for r in conn.execute("SELECT id FROM nodes WHERE label = ?", (label,))]
    require_node(cast(Any, set(candidates)), node_id, candidates, what)


# --------------------------------------------------------------------------- trace e impacto


def trace(
    conn: sqlite3.Connection,
    entry: str,
    *,
    depth: int = 8,
    min_confidence: float = 0.0,
    include_infra: bool = False,
    expand_pgfunctions: bool = False,
    max_nodes: int = 500,
) -> SubgraphData:
    clamp = Clamp()
    depth = clamp("depth", depth, MAX_DEPTH, 1)
    max_nodes = clamp("maxNodes", max_nodes, MAX_NODES, 1)
    root = file_id(entry)
    require(conn, root, "File", "arquivo de entrada")

    def expand(label: str) -> bool:
        if label == "PgFunction":
            return expand_pgfunctions
        return label not in LEAF_LABELS

    walked = walk_sql(
        conn,
        root,
        depth=depth,
        min_confidence=min_confidence,
        include_infra=include_infra,
        expand=expand,
        max_nodes=max_nodes,
    )
    steps, labels = walked.walk.steps, walked.labels
    origins = [n for n in steps if n == root or expand(labels[n])]
    touches: list[EdgeRow] = []
    for chunk in batches(origins):
        for row in conn.execute(
            f"SELECT {_EDGE_COLUMNS} FROM edges WHERE src IN ({placeholders(len(chunk))})"
            f" AND type IN ({placeholders(len(_OPS))}) AND confidence >= ?",
            (*chunk, *_OPS, min_confidence),
        ):
            edge = _edge(row)
            if edge.dst in steps:
                touches.append(edge)
    per_op: dict[str, set[str]] = {}
    for edge in touches:
        per_op.setdefault(TABLE_OPS[edge.type], set()).add(edge.dst)
    node_rows = nodes(conn, steps)
    counts = Counter(labels.values())
    summary = {
        "nodes": len(steps),
        "files": counts["File"],
        "screens": sum(
            1 for n in steps if labels[n] == "File" and node_rows[n].attrs.get("module_hint")
        ),
        "tables": {
            op: len(per_op[op]) for op in ("INSERT", "UPDATE", "DELETE", "SELECT") if op in per_op
        },
        "rpc": counts["PgFunction"] + counts["RpcCall"],
        "truncated": walked.truncated,
        "clamped": clamp.clamped,
    }
    tree = edges_by_id(conn, walked.edge_ids.values())
    seen = {e.id for e in tree}
    edges = tree + [e for e in touches if e.id not in seen]
    priority = [n for n, label in labels.items() if label in ("Table", "PgFunction", "RpcCall")]
    return SubgraphData(
        root,
        [node_rows[n] for n in steps if n in node_rows],
        edges,
        {n: s.depth for n, s in steps.items()},
        walked.truncated,
        summary,
        walked,
        priority,
    )


def impact(
    conn: sqlite3.Connection,
    table: str,
    schema: str | None = None,
    *,
    depth: int = 10,
    min_confidence: float = 0.0,
    include_infra: bool = False,
    max_nodes: int = 500,
) -> SubgraphData:
    clamp = Clamp()
    depth = clamp("depth", depth, MAX_DEPTH, 1)
    max_nodes = clamp("maxNodes", max_nodes, MAX_NODES, 1)
    # Sem schema: o nó `Table` único com esse nome, como o `impact` da CLI.
    if schema is None:
        tables = [row[0] for row in conn.execute("SELECT id FROM nodes WHERE label = 'Table'")]
        target = table_target(tables, table, None)
    else:
        target = table_id(table, schema)
    require(conn, target, "Table", "tabela")
    walked = walk_sql(
        conn,
        target,
        depth=depth,
        min_confidence=min_confidence,
        include_infra=include_infra,
        reverse=True,
        max_nodes=max_nodes,
    )
    steps, labels = walked.walk.steps, walked.labels
    direct = [
        _edge(row)
        for row in conn.execute(
            f"SELECT {_EDGE_COLUMNS} FROM edges WHERE dst = ?"
            f" AND type IN ({placeholders(len(_OPS))}) AND confidence >= ?",
            (target, *_OPS, min_confidence),
        )
    ]
    direct = [e for e in direct if e.src in steps]
    node_rows = nodes(conn, steps)
    dao_users = _dao_users(conn, direct, steps, labels, min_confidence)  # antes de mexer em `steps`
    reached = {
        "nodes": len(steps),
        "code": sum(1 for n in steps if labels[n] in ("Method", "Function")),
        "pgfunctions": sum(1 for e in direct if labels.get(e.src) == "PgFunction"),
    }
    files = [n for n in steps if labels[n] == "File" and n in node_rows]
    by_method = _method_files(conn, walked, node_rows, include_infra, max_nodes)
    files += list(by_method.owners)
    screens = [n for n in files if _node_attrs(node_rows, by_method, n).get("module_hint")]
    rpcs = [n for n in files if _RPC_FILE.search(n)]
    summary = {
        "nodes": reached["nodes"],
        "direct": len(direct),
        "dao_users": len(dao_users),
        "files": len(files),
        "files_via_method": len(by_method.owners),
        "screens": len(screens),
        "rpc_files": len(rpcs),
        "code": reached["code"],
        "pgfunctions": reached["pgfunctions"],
        "truncated": walked.truncated or by_method.truncated,
        "clamped": clamp.clamped,
    }
    node_rows.update(by_method.rows)
    tree = edges_by_id(conn, walked.edge_ids.values()) + by_method.edges
    seen = {e.id for e in tree}
    return SubgraphData(
        target,
        [node_rows[n] for n in steps if n in node_rows],
        tree + [e for e in direct if e.id not in seen],
        {n: s.depth for n, s in steps.items()},
        walked.truncated or by_method.truncated,
        summary,
        walked,
        sorted((set(screens) | set(rpcs)) & set(steps)),
        dao_users,
    )


def _node_attrs(
    node_rows: dict[str, NodeRow], by_method: "_MethodFiles", node: str
) -> dict[str, Any]:
    row = node_rows.get(node) or by_method.owners.get(node)
    return row.attrs if row else {}


@dataclass(slots=True)
class _MethodFiles:
    """Arquivos dos métodos alcançados pelo `impact`, que o `walk` não traz porque
    não percorre `DECLARES`."""

    owners: dict[str, NodeRow] = field(default_factory=dict)  # arquivo → linha (todos contam)
    rows: dict[str, NodeRow] = field(default_factory=dict)  # nós acrescentados ao desenho
    edges: list[EdgeRow] = field(default_factory=list)
    truncated: bool = False


def _method_files(
    conn: sqlite3.Connection,
    walked: SqlWalk,
    node_rows: dict[str, NodeRow],
    include_infra: bool,
    max_nodes: int,
) -> _MethodFiles:
    """Para cada `Method`/`Function` alcançado, o arquivo onde foi declarado (atributo `path`),
    na mesma profundidade. No desenho entram o arquivo e a classe do caminho (as arestas
    `DECLARES` que já existem), como passos da busca; acima de `max_nodes` o arquivo só conta
    no resumo (`truncated`). Muda `walked.walk.steps` e `walked.labels`."""
    steps, labels = walked.walk.steps, walked.labels
    found = _MethodFiles()
    first: dict[str, str] = {}  # arquivo → 1º método (o mais raso: `steps` está na ordem da BFS)
    for node in steps:
        if labels[node] not in ("Method", "Function") or node not in node_rows:
            continue
        path = node_rows[node].attrs.get("path")
        owner = file_id(str(path)) if path else None
        if owner and owner not in steps:
            first.setdefault(owner, node)
    found.owners = {
        owner: row
        for owner, row in nodes(conn, first).items()
        if include_infra or not row.attrs.get("is_infra")
    }
    if not found.owners:
        return found
    methods = sorted({first[owner] for owner in found.owners})
    class_of: dict[str, tuple[str, int]] = {}  # método → (Class que o declara, id da aresta)
    for chunk in batches(methods):
        for edge_id, src, dst in conn.execute(
            "SELECT id, src, dst FROM edges WHERE type = 'DECLARES' AND src LIKE 'Class:%'"
            f" AND dst IN ({placeholders(len(chunk))}) ORDER BY id",
            tuple(chunk),
        ):
            class_of.setdefault(dst, (src, edge_id))
    classes = sorted({cls for cls, _edge in class_of.values()})
    file_edges: dict[tuple[str, str], int] = {}  # (arquivo, classe ou método) → id da aresta
    for chunk in batches([*classes, *methods]):
        for edge_id, src, dst in conn.execute(
            "SELECT id, src, dst FROM edges WHERE type = 'DECLARES' AND src LIKE 'File:%'"
            f" AND dst IN ({placeholders(len(chunk))}) ORDER BY id",
            tuple(chunk),
        ):
            file_edges.setdefault((src, dst), edge_id)
    class_rows = nodes(conn, classes)
    budget = max_nodes - len(steps)
    ids: list[int] = []
    for owner in sorted(found.owners):
        method = first[owner]
        cls, cls_edge = class_of.get(method, (None, 0))
        if cls is not None and (owner, cls) in file_edges:
            parent, link = cls, file_edges[(owner, cls)]
        elif (owner, method) in file_edges:  # função solta: o arquivo declara direto
            cls, parent, link = None, method, file_edges[(owner, method)]
        else:
            continue  # sem `DECLARES` até o arquivo: conta no resumo, não entra no desenho
        added_class = cls is not None and cls not in steps and cls in class_rows
        if 1 + added_class > budget:
            found.truncated = True
            continue
        budget -= 1 + added_class
        # No desenho, o pai fica um nível acima (como em toda a busca): classe e arquivo logo
        # depois do método. O `AffectedFile` da CLI usa a profundidade do próprio método.
        depth = steps[method].depth + 1
        if cls is not None and cls in class_rows:
            if cls not in steps:
                steps[cls] = Step(cls, method, "DECLARES", 1.0, depth, {"via": "declares"})
                labels[cls] = "Class"
                found.rows[cls] = class_rows[cls]
            ids.append(cls_edge)
        steps[owner] = Step(
            owner, parent, "DECLARES", 1.0, steps[parent].depth + 1, {"via": "declares"}
        )
        labels[owner] = "File"
        found.rows[owner] = found.owners[owner]
        ids.append(link)
    for edge in edges_by_id(conn, ids):
        found.edges.append(replace(edge, evidence={**edge.evidence, "via": "declares"}))
    return found


def _dao_users(
    conn: sqlite3.Connection,
    direct: Sequence["EdgeRow"],
    steps: dict[str, Step],
    labels: dict[str, str],
    min_confidence: float,
) -> list[DaoTouch]:
    """Mesma regra de `analysis.impact`: quem chama (`CALLS`) um método `cl_*` que toca a tabela."""
    touches = {e.src: e for e in direct if is_dao_method(e.src)}
    direct_origins = {e.src for e in direct}
    calls: list[tuple[str, str, str, str, float, dict[str, Any]]] = []
    for chunk in batches(sorted(touches)):
        rows = conn.execute(
            f"SELECT src, dst, confidence, evidence FROM edges WHERE type = 'CALLS'"
            f" AND dst IN ({placeholders(len(chunk))}) AND confidence >= ?"
            " ORDER BY src, dst, id",
            (*chunk, min_confidence),
        )
        for caller, dao_node, confidence, evidence in rows:
            if caller in direct_origins or caller not in steps:
                continue
            touch = touches[dao_node]
            calls.append(
                (
                    caller,
                    labels[caller],
                    TABLE_OPS[touch.type],
                    dao_node,
                    min(confidence, touch.confidence),
                    json.loads(evidence),
                )
            )
    return group_dao_users(calls)


def mermaid(data: SubgraphData, max_nodes: int, *, reverse: bool) -> str:
    text, _cut = render_mermaid(
        data.walk.walk, data.walk.labels, max_nodes, reverse=reverse, priority=data.priority
    )
    return text


# --------------------------------------------------------------------------- estatísticas


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Meta do scan e as contagens do resumo gravado no `meta` (sem varrer as tabelas)."""
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    summary = json.loads(meta.pop("summary", "{}") or "{}")
    catalog = summary.get("catalog") or {}
    return {
        "meta": meta,
        "nodes": summary.get("nodes", {}),
        "edges": summary.get("edges", {}),
        "files": summary.get("files", {}),
        "catalog": {
            "read": catalog.get("read"),
            "graph": catalog.get("graph"),
            "modules": catalog.get("modules"),
        }
        if catalog
        else None,
        # O `scan` grava o resumo no `meta` antes da busca e dos agregados: sem eles no resumo,
        # conta direto das tabelas `analytics_*` (rápido: 12 mil telas no e-cidade).
        "analytics": summary.get("analytics") or _analytics_counts(conn),
        "search": summary.get("search"),
    }


def _analytics_counts(conn: sqlite3.Connection) -> dict[str, Any] | None:
    try:
        screens, in_menu = conn.execute(
            "SELECT count(*), coalesce(sum(in_menu), 0) FROM analytics_screens"
        ).fetchone()
        shared = conn.execute("SELECT count(*) FROM analytics_tables WHERE shared = 1").fetchone()
        modules = conn.execute("SELECT count(*) FROM analytics_modules").fetchone()
    except sqlite3.OperationalError:
        return None  # banco sem agregados (`--no-analytics`)
    return {
        "screens": screens,
        "screens_in_menu": in_menu,
        "tables_shared": shared[0],
        "modules": modules[0],
    }


def graph_source(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'source'").fetchone()
    return str(row[0]) if row and row[0] else None


# --------------------------------------------------------------------------- agregados


def _analytics(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    try:
        return conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            raise StoreError(NO_ANALYTICS) from exc
        raise


_MODULE_SQL = (
    "SELECT m.module, m.name, m.files, m.screens, m.tables, m.ca, m.ce, m.instability,"
    " m.weight_in, m.weight_out, m.shared_tables,"
    " coalesce(o.owned, 0), coalesce(o.owned_shared, 0),"
    " CASE WHEN c.total > 0 THEN round(c.known / c.total, 4) END"
    " FROM analytics_modules m"
    # Tabelas de que o módulo é dono, pela mesma fonte das compartilhadas.
    " LEFT JOIN (SELECT j.value AS module, count(*) AS owned, sum(t.shared) AS owned_shared"
    "   FROM analytics_tables t, json_each(t.owner_modules) j GROUP BY j.value) o"
    " ON o.module = m.module"
    # Cobertura: parte do peso das dependências que vai para um módulo conhecido.
    " LEFT JOIN (SELECT src, sum(CASE WHEN dst != ? THEN weight ELSE 0 END) AS known,"
    "   sum(weight) AS total FROM analytics_module_edges GROUP BY src) c ON c.src = m.module"
)


def module_rows(conn: sqlite3.Connection, module: str | None = None) -> list[tuple[Any, ...]]:
    """Linhas de `analytics_modules` mais `owned_tables`, `owned_shared` e `coverage`."""
    if module is not None:
        return _analytics(conn, _MODULE_SQL + " WHERE m.module = ?", (NO_MODULE, module))
    return _analytics(conn, _MODULE_SQL + " ORDER BY m.files DESC, m.module", (NO_MODULE,))


def foreign_written_rows(conn: sqlite3.Connection, module: str) -> list[tuple[Any, ...]]:
    """Tabelas com dono que não é `module` e que `module` grava (sinal forte de acoplamento)."""
    return _analytics(
        conn,
        "SELECT table_id, owner_modules, reader_modules, writer_modules, readers, writers, shared"
        " FROM analytics_tables t"
        " WHERE EXISTS (SELECT 1 FROM json_each(t.writer_modules) w WHERE w.key = ?)"
        " AND json_array_length(t.owner_modules) > 0"
        " AND NOT EXISTS (SELECT 1 FROM json_each(t.owner_modules) o WHERE o.value = ?)"
        " ORDER BY (SELECT w.value FROM json_each(t.writer_modules) w WHERE w.key = ?) DESC,"
        " table_id",
        (module, module, module),
    )


def table_count(conn: sqlite3.Connection, *, shared: bool | None, module: str | None) -> int:
    where: list[str] = ["1 = 1"]
    params: list[Any] = []
    if shared is not None:
        where.append("shared = ?")
        params.append(int(shared))
    if module is not None:
        where.append("EXISTS (SELECT 1 FROM json_each(owner_modules) WHERE value = ?)")
        params.append(module)
    rows = _analytics(
        conn, f"SELECT count(*) FROM analytics_tables WHERE {' AND '.join(where)}", params
    )
    return int(rows[0][0])


def module_edge_rows(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    min_weight: float = 0.0,
    src: str | None = None,
    dst: str | None = None,
) -> list[tuple[Any, ...]]:
    where: list[str] = ["weight >= ?"]
    params: list[Any] = [min_weight]
    for column, value in (("kind", kind), ("src", src), ("dst", dst)):
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
    return _analytics(
        conn,
        "SELECT src, dst, kind, weight, edges, samples FROM analytics_module_edges"
        f" WHERE {' AND '.join(where)} ORDER BY weight DESC, src, dst, kind",
        params,
    )


def table_rows(
    conn: sqlite3.Connection,
    *,
    shared: bool | None = None,
    module: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[tuple[Any, ...]]:
    """`module` filtra as tabelas de que o módulo é dono (dicionário)."""
    where: list[str] = ["1 = 1"]
    params: list[Any] = []
    if shared is not None:
        where.append("shared = ?")
        params.append(int(shared))
    if module is not None:
        where.append("EXISTS (SELECT 1 FROM json_each(owner_modules) WHERE value = ?)")
        params.append(module)
    return _analytics(
        conn,
        "SELECT table_id, owner_modules, reader_modules, writer_modules, readers, writers, shared"
        f" FROM analytics_tables WHERE {' AND '.join(where)}"
        " ORDER BY readers + writers DESC, table_id LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )


def _module_filter(column: str, module: str | None) -> tuple[str, list[Any]]:
    if module is None:
        return "", []
    # Coluna com os módulos separados por vírgula (`analytics_hotspots`/`analytics_screens`).
    return f" AND (',' || {column} || ',') LIKE ?", [f"%,{module},%"]


def hotspot_rows(
    conn: sqlite3.Connection, *, label: str | None, module: str | None, limit: int
) -> list[tuple[Any, ...]]:
    extra, params = _module_filter("module", module)
    if label is not None:
        extra += " AND label = ?"
        params.append(label)
    return _analytics(
        conn,
        "SELECT node_id, label, module, in_degree, out_degree, fan_in_modules, rank"
        f" FROM analytics_hotspots WHERE 1 = 1{extra}"
        " ORDER BY fan_in_modules DESC, in_degree DESC, node_id LIMIT ?",
        (*params, limit),
    )


def screen_rows(
    conn: sqlite3.Connection,
    *,
    in_menu: bool | None,
    module: str | None,
    limit: int,
    offset: int,
) -> list[tuple[Any, ...]]:
    extra, params = _module_filter("module", module)
    if in_menu is not None:
        extra += " AND in_menu = ?"
        params.append(int(in_menu))
    return _analytics(
        conn,
        "SELECT file_id, module, in_menu, menu_items, tables_written, tables_read, reached_by"
        f" FROM analytics_screens WHERE 1 = 1{extra}"
        " ORDER BY reached_by DESC, file_id LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )


def ensure_node_exists(conn: sqlite3.Connection, node_id: str) -> None:
    if not conn.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone():
        raise QueryError(f"nó não encontrado no grafo: {node_id}")
