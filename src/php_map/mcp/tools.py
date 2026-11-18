"""Ferramentas do servidor MCP, como funções puras sobre o `ProjectCatalog`.

Sem importar o SDK do MCP: cada função recebe parâmetros simples e devolve um `dict`
serializável em JSON, compacto e com limites (a resposta vai para o contexto de uma IA). As
consultas são as da interface (`web/repo.py`, `web/communication.py`), da busca e do `cross`,
todas em SQL sob demanda. Proibido carregar o grafo inteiro (`store.load`) aqui.
"""

import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from php_map.analysis import boundary, routine_origin
from php_map.analysis import field_search as field_search_module
from php_map.analysis import formula_topics as topics_module
from php_map.analysis import formulas as formulas_module
from php_map.analysis import layers as layers_module
from php_map.analysis import menu_dossier as menu_dossier_module
from php_map.analysis import table_access as table_access_module
from php_map.analysis import value_origin as value_origin_module
from php_map.analysis.cross import STATUSES, CrossEndpoint
from php_map.errors import QueryError, UsageError
from php_map.graph import formula_index as formula_index_module
from php_map.graph import routines
from php_map.mcp.catalog import CatalogProject, ProjectCatalog
from php_map.report import cards, paging
from php_map.search.query import search as search_query
from php_map.web import communication as comm
from php_map.web import repo, rules
from php_map.web.source import Workspace, read_source
from php_map.web.subgraph import batches, placeholders

MAX_SEARCH = 100
MAX_NEIGHBORS = 200
MAX_SUBGRAPH = 500
MAX_COMMUNICATION = 200
MAX_CROSS = 500
MAX_CONTEXT = 200
MAX_ROUTINE_LINES = 500  # linhas do texto de uma função por chamada (padrão 200)
MAX_ORIGINS = 200  # funções por página do relatório de origem
MAX_RULES = 200  # regras por página de `routine`/`routine_rules`
MAX_QUERIES = 200  # consultas/usos de coluna por página de `routine`/`column_usage`
MAX_TEXT = 300  # caracteres de um texto em atributo ou evidência
MAX_LIST = 30  # itens de uma lista em atributo ou evidência
WRITE_TYPES = ("INSERTS", "UPDATES", "DELETES")

# Arquivos que podem ter configuração ou segredo: nunca saem pelo `source`.
_BLOCKED_NAMES = ("*.ini", "*.ini.dist", ".env*", "*.pem", "*.key", "*.pfx", "*.p12", "*.crt")
_BLOCKED_PATHS = ("libs/db_conn.php", "config/*.php")
_BLOCKED_DIRS = frozenset({"configs", "certificado", ".git", "vendor", "node_modules"})
_LABEL_PREFIX = re.compile(r"^[A-Z][A-Za-z]+:")  # `Method:x`, não `C:\x`
BLOCKED = "arquivo bloqueado por conter configuração ou segredo"

T = TypeVar("T")


# --------------------------------------------------------------------------- utilitários


def _with_conn(catalog: ProjectCatalog, project: str, fn: Callable[[sqlite3.Connection], T]) -> T:
    with closing(catalog.connect(project)) as conn:
        return fn(conn)


def _cap(value: int, maximum: int, minimum: int = 1) -> int:
    return max(minimum, min(value, maximum))


def compact(value: Any, depth: int = 0) -> Any:
    """Corta textos longos e listas grandes, recursivamente (a IA não precisa do SQL inteiro)."""
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "…"
    if isinstance(value, dict):
        if depth > 4:
            return "…"
        return {str(k): compact(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        items = [compact(v, depth + 1) for v in list(value)[:MAX_LIST]]
        if len(value) > MAX_LIST:
            items.append(f"… +{len(value) - MAX_LIST}")
        return items
    return value


def _name(node_id: str, attrs: dict[str, Any] | None = None) -> str:
    if attrs and isinstance(attrs.get("name"), str):
        return str(attrs["name"])
    return node_id.partition(":")[2]


def _node(row: repo.NodeRow, depth: int | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"id": row.id, "label": row.label, "name": _name(row.id, row.attrs)}
    if row.modules:
        item["modules"] = row.modules
    if depth is not None:
        item["depth"] = depth
    return item


def _edge(edge: repo.EdgeRow) -> dict[str, Any]:
    return {
        "src": edge.src,
        "dst": edge.dst,
        "type": edge.type,
        "source": edge.source,
        "confidence": edge.confidence,
    }


def _evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    keep = ("path", "line", "via", "args", "sql", "name", "literal")
    return {k: compact(v) for k, v in evidence.items() if k in keep}


# --------------------------------------------------------------------------- projetos


def _formula_index_present(catalog: ProjectCatalog, slug: str) -> bool:
    try:
        with closing(catalog.connect(slug)) as conn:
            return formula_index_module.has_formula_index(conn)
    except Exception:  # banco ausente/ilegível: "sem índice" (list_projects nunca falha por isso)
        return False


def list_projects(catalog: ProjectCatalog) -> dict[str, Any]:
    items = [
        {
            "slug": p.slug,
            "name": p.name,
            "profile": p.profile,
            "nodes": int(p.meta.get("node_count", 0) or 0),
            "edges": int(p.meta.get("edge_count", 0) or 0),
            "scanned_at": p.meta.get("scanned_at"),
            "has_source": p.source is not None,
            # Caminho do arquivo escolhido e se `db` era uma pasta no `projects.json`.
            "db": str(p.db),
            "db_from_folder": p.db_from_folder,
            # Se `formula_search`/`routine_rules(topic=...)` respondem neste projeto.
            "formula_index": _formula_index_present(catalog, p.slug),
        }
        for p in catalog.projects
    ]
    return {"projects": items}


# --------------------------------------------------------------------------- busca e nó


def search(
    catalog: ProjectCatalog,
    query: str,
    project: str,
    label: str | None = None,
    module: str | None = None,
    limit: int = 20,
    any_term: bool = False,
) -> dict[str, Any]:
    target = catalog.get(project)
    hits = search_query(
        target.db,
        query,
        label=label,
        module=module,
        limit=_cap(limit, MAX_SEARCH),
        any_term=any_term,
    )
    return {
        "query": query,
        "total": len(hits),
        "hits": [
            {
                "node_id": h.node_id,
                "label": h.label,
                "name": h.node_id.partition(":")[2],
                "modules": h.modules,
                "field": h.field,
                "snippet": compact(h.snippet),
                "score": round(h.score, 2),
                "next_command": h.next_command,
            }
            for h in hits
        ],
    }


def node(catalog: ProjectCatalog, node_id: str, project: str) -> dict[str, Any]:
    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        found = repo.node(conn, node_id)
        if found is None:
            raise QueryError(f"nó inexistente no projeto {project}: {node_id}")
        out_counts = conn.execute(
            "SELECT type, count(*) FROM edges WHERE src = ? GROUP BY type ORDER BY type", (node_id,)
        ).fetchall()
        in_counts = conn.execute(
            "SELECT type, count(*) FROM edges WHERE dst = ? GROUP BY type ORDER BY type", (node_id,)
        ).fetchall()
        out: dict[str, Any] = {
            "id": found.id,
            "label": found.label,
            "name": _name(found.id, found.attrs),
            "attrs": compact(found.attrs),
            "modules": repo.memberships(conn, node_id),
            "edges_out": dict(out_counts),
            "edges_in": dict(in_counts),
        }
        if found.label == "PgFunction":  # A definição está no grafo? (ferramenta `routine`)
            read_at = routines.read_at_of(conn, node_id)
            out["has_source"] = read_at is not None
            out["read_at"] = read_at
        return out

    return _with_conn(catalog, project, run)


def neighbors(
    catalog: ProjectCatalog,
    node_id: str,
    project: str,
    direction: str = "out",
    edge_types: list[str] | None = None,
    min_confidence: float = 0.0,
    limit: int = 50,
) -> dict[str, Any]:
    if direction not in ("out", "in"):
        raise QueryError("direction aceita out ou in")
    limit = _cap(limit, MAX_NEIGHBORS)

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        if repo.node(conn, node_id) is None:
            raise QueryError(f"nó inexistente no projeto {project}: {node_id}")
        incoming = direction == "in"
        total, edges, found = repo.neighbors(
            conn,
            node_id,
            incoming=incoming,
            types=edge_types or None,
            min_confidence=min_confidence,
            include_infra=True,
            limit=limit,
            offset=0,
        )
        rows = []
        for edge in edges:
            other = edge.src if incoming else edge.dst
            row = found.get(other)
            rows.append(
                {
                    **_edge(edge),
                    "neighbor": _node(row) if row else {"id": other},
                    "evidence": _evidence(edge.evidence),
                }
            )
        return {
            "node": node_id,
            "direction": direction,
            "total": total,
            "edges": rows,
            "truncated": total > len(rows),
        }

    return _with_conn(catalog, project, run)


# --------------------------------------------------------------------------- trace e impacto


def _subgraph(data: repo.SubgraphData, *, reverse: bool, mermaid: bool, cap: int) -> dict[str, Any]:
    nodes = [_node(row, data.depth_of.get(row.id)) for row in data.nodes]
    tables = sorted(n["id"] for n in nodes if n["label"] == "Table")
    out: dict[str, Any] = {
        "root": data.root,
        "summary": data.summary,
        "nodes": nodes,
        "edges": [_edge(e) for e in data.edges],
        "tables": tables,
        "truncated": data.truncated,
        "limits": {"max_nodes": cap, "clamped": data.summary.get("clamped", [])},
    }
    if mermaid:
        out["mermaid"] = repo.mermaid(data, min(cap, 60), reverse=reverse)
    return out


def trace(
    catalog: ProjectCatalog,
    entry: str,
    project: str,
    depth: int = 8,
    min_confidence: float = 0.0,
    include_infra: bool = False,
    max_nodes: int = 150,
    mermaid: bool = False,
) -> dict[str, Any]:
    if entry.startswith("File:"):
        entry = entry.removeprefix("File:")
    elif _LABEL_PREFIX.match(entry):
        raise QueryError(
            "o trace parte de um arquivo (caminho ou File:…); para outros nós use neighbors"
        )
    cap = _cap(max_nodes, MAX_SUBGRAPH)

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        data = repo.trace(
            conn,
            entry,
            depth=depth,
            min_confidence=min_confidence,
            include_infra=include_infra,
            max_nodes=cap,
        )
        out = _subgraph(data, reverse=False, mermaid=mermaid, cap=cap)
        out["remote_calls"] = _remote_calls(conn, [row.id for row in data.nodes])
        return out

    return _with_conn(catalog, project, run)


def _remote_calls(conn: sqlite3.Connection, ids: list[str]) -> list[dict[str, Any]]:
    """`CALLS_REMOTE` saindo dos nós alcançados: o fluxo chama outro sistema (o trace comum não
    atravessa; o outro lado vem da ferramenta `endpoint`)."""
    calls: list[dict[str, Any]] = []
    for chunk in batches(ids):
        for src, dst, confidence, evidence in conn.execute(
            f"SELECT src, dst, confidence, evidence FROM edges WHERE type = 'CALLS_REMOTE'"
            f" AND src IN ({placeholders(len(chunk))}) ORDER BY dst, src",
            tuple(chunk),
        ):
            ev = json.loads(evidence)
            calls.append(
                {
                    "from": src,
                    "endpoint": dst,
                    "confidence": confidence,
                    "path": ev.get("path"),
                    "line": ev.get("line"),
                }
            )
    return calls[:MAX_NEIGHBORS]


def impact(
    catalog: ProjectCatalog,
    table: str,
    project: str,
    schema: str | None = None,
    depth: int = 10,
    max_nodes: int = 150,
    mermaid: bool = False,
) -> dict[str, Any]:
    if table.startswith("Table:"):
        schema_part, _, name = table.removeprefix("Table:").rpartition(".")
        table, schema = name, schema or (schema_part or None)
    cap = _cap(max_nodes, MAX_SUBGRAPH)

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        data = repo.impact(conn, table, schema, depth=depth, max_nodes=cap)
        out = _subgraph(data, reverse=True, mermaid=mermaid, cap=cap)
        writers: set[str] = set()
        readers: set[str] = set()
        for src, type_ in conn.execute(  # direto no SQL: o corte da BFS não esconde ninguém
            "SELECT DISTINCT src, type FROM edges WHERE dst = ?"
            " AND type IN ('INSERTS', 'UPDATES', 'DELETES', 'QUERIES')",
            (data.root,),
        ):
            (writers if type_ in WRITE_TYPES else readers).add(src)
        out["writers_total"], out["readers_total"] = len(writers), len(readers)
        out["writers"] = sorted(writers)[:MAX_NEIGHBORS]
        out["readers"] = sorted(readers)[:MAX_NEIGHBORS]
        out["dao_users_total"] = len(data.dao_users)  # quem chama o DAO da tabela
        out["dao_users"] = [
            {
                "origin": d.origin,
                "label": d.label,
                "operation": d.operation,
                "dao_method": d.dao_method,
                "confidence": d.confidence,
                "path": d.evidence.get("path"),
                "line": d.evidence.get("line"),
            }
            for d in data.dao_users[:MAX_NEIGHBORS]
        ]
        return out

    return _with_conn(catalog, project, run)


# --------------------------------------------------------------------------- comunicação


def _cross_or_none(catalog: ProjectCatalog) -> tuple[Any, str | None]:
    if len(catalog.projects) < 2:
        return None, "só um projeto carregado: sem o outro lado da costura"
    try:
        return catalog.cross(3), None
    except QueryError as exc:
        return None, str(exc)


def _impl(items: Iterable[comm.Impl]) -> list[dict[str, Any]]:
    return [{"node": i.node, "path": i.path, "line": i.line} for i in items]


def _caller(c: comm.Caller) -> dict[str, Any]:
    return {
        "project": c.project,
        "node": c.node,
        "path": c.path,
        "line": c.line,
        "module": c.module,
        "args": compact(c.args),
        "occurrences": c.occurrences,
    }


def _reach(r: comm.ReachSummary | None) -> dict[str, Any] | None:
    if r is None:
        return None
    return {"tables": list(r.tables), "modules": list(r.modules), "nodes": r.nodes}


def _row(row: comm.Row) -> dict[str, Any]:
    return {
        "endpoint_id": row.endpoint_id,
        "label": row.node_label,
        "system": row.system,
        "protocol": row.protocol,
        "kind": row.kind,
        "name": row.name,
        "direction": row.direction,
        "status": row.status,
        "params": compact(list(row.params)),
        "implemented_by": _impl(row.implemented_by),
        "calls": row.callers,
        "caller_sample": [_caller(c) for c in row.caller_sample],
        "module": row.module,
        "reaches": _reach(row.reaches),
    }


def communication(
    catalog: ProjectCatalog,
    project: str,
    system: str | None = None,
    protocol: str | None = None,
    direction: str | None = None,
    module: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    catalog.get(project)
    report, note = _cross_or_none(catalog)

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        page = comm.list_communication(
            conn,
            system=system,
            protocol=protocol,
            direction=direction,
            module=module,
            search=search,
            limit=_cap(limit, MAX_COMMUNICATION),
            offset=max(0, offset),
            cross=report,
        )
        return {
            "total": page.total,
            "offset": page.offset,
            "limit": page.limit,
            "counters": asdict(page.counters),
            "systems": [asdict(f) for f in page.systems],
            "protocols": [asdict(f) for f in page.protocols],
            "items": [_row(r) for r in page.items],
            "truncated": page.offset + len(page.items) < page.total,
            "cross_note": note,
        }

    return _with_conn(catalog, project, run)


def endpoint(catalog: ProjectCatalog, endpoint_id: str, project: str) -> dict[str, Any]:
    target = catalog.get(project)
    report, note = _cross_or_none(catalog)

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        found = comm.endpoint_detail(
            conn, endpoint_id, local_project=target.graph_project, cross=report
        )
        other = found.counterpart
        counterpart = None
        if other is not None:
            counterpart = {
                "available": other.available,
                "message": other.message,
                "status": other.status,
                "served_by": other.served_by,
                "system": other.system,
                "servers": _impl(other.servers),
                "params": compact(list(other.params)),
                "reaches": _reach(other.reaches),
                "callers": [_caller(c) for c in other.callers[:MAX_LIST]],
            }
        return {
            **_row(found.row),
            "callers": [_caller(c) for c in found.callers[:MAX_LIST]],
            "callers_total": len(found.callers),
            "attrs": compact(found.attrs),
            "counterpart": counterpart,
            "cross_note": note,
        }

    return _with_conn(catalog, project, run)


def _cross_endpoint(e: CrossEndpoint) -> dict[str, Any]:
    return {
        "endpoint_id": e.endpoint_id,
        "status": e.status,
        "protocol": e.protocol,
        "kind": e.kind,
        "served_by": e.served_by,
        "calls": e.calls,
        "callers": len(e.callers),
        "caller_projects": sorted({c.project for c in e.callers}),
        "caller_modules": sorted({c.module for c in e.callers if c.module}),
        "params": [p.get("name") for p in e.params],
        "tables": list(e.reaches.tables[:MAX_LIST]),
        "tables_total": len(e.reaches.tables),
    }


def cross_summary(
    catalog: ProjectCatalog,
    status: str | None = None,
    protocol: str | None = None,
    limit: int = 50,
    depth: int = 3,
) -> dict[str, Any]:
    if status is not None and status not in STATUSES:
        raise QueryError(f"status aceita: {', '.join(STATUSES)}")
    report = catalog.cross(_cap(depth, 10))
    shown = report.filtered(only=status, protocol=protocol)
    cap = _cap(limit, MAX_CROSS)
    return {
        "depth": report.depth,
        "projects": [asdict(p) for p in report.projects],
        "counts": report.counts,
        "filtered_counts": shown.counts,
        "dependencies": [
            {
                "caller": d.caller,
                "server": d.server,
                "endpoints": d.endpoints,
                "calls": d.calls,
                "modules": [asdict(m) for m in d.modules],
                "tables": len(d.tables),
                "tables_sample": list(d.tables[:MAX_LIST]),
            }
            for d in shown.dependencies
        ],
        "dynamic_calls": [asdict(c) for c in report.dynamic_calls],
        "external_calls": [asdict(c) for c in report.external_calls],
        "endpoints": [_cross_endpoint(e) for e in shown.endpoints[:cap]],
        "endpoints_total": len(shown.endpoints),
        "truncated": len(shown.endpoints) > cap,
        "warnings": list(report.warnings),
    }


# --------------------------------------------------------------------------- código-fonte


def is_blocked(relative: str) -> bool:
    """Arquivo de configuração, chave ou pasta de terceiros (lista de arquivos sensíveis)."""
    path = PurePosixPath(relative.replace("\\", "/").lower())
    if any(part in _BLOCKED_DIRS for part in path.parts[:-1]):
        return True
    if any(path.match(pattern) for pattern in _BLOCKED_NAMES):
        return True
    return any(path.match(pattern) for pattern in _BLOCKED_PATHS)


def _resolved_relative(root: Path, relative: str) -> str | None:
    """O caminho real (seguindo links) relativo à raiz, ou None se sair dela ou não existir."""
    try:
        target = (root / relative.replace("\\", "/")).resolve()
        return target.relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _decode(line: str, profile: str) -> str:
    """O `read_source` decodifica em latin-1; num projeto zend1 o fonte é UTF-8."""
    if profile != "zend1":
        return line
    try:
        return line.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return line


def source(
    catalog: ProjectCatalog, project: str, path: str, line: int | None = None, context: int = 30
) -> dict[str, Any]:
    target: CatalogProject = catalog.get(project)
    if target.source is None:
        raise QueryError("fonte não disponível neste servidor (o projeto não tem pasta de código)")
    if is_blocked(path):
        raise QueryError(BLOCKED)
    resolved = _resolved_relative(target.source, path)
    if resolved is not None and is_blocked(resolved):
        raise QueryError(BLOCKED)
    workspace = Workspace(str(target.source), "projeto", True, True, None)
    found = read_source(workspace, path, line)
    context = _cap(context, MAX_CONTEXT)
    first = found.start_line
    lines = found.lines
    if line is not None and found.highlight is not None:
        start = max(found.highlight - context, first)
        end = min(found.highlight + context, first + len(lines) - 1)
    else:
        start, end = first, min(first + 2 * context - 1, first + len(lines) - 1)
    window = lines[start - first : end - first + 1]
    return {
        "project": project,
        "path": found.path,
        "total_lines": found.total_lines,
        "start_line": start,
        "end_line": start + len(window) - 1,
        "highlight": found.highlight,
        "lines": [f"{start + i}: {_decode(text, target.profile)}" for i, text in enumerate(window)],
        "truncated": start > 1 or start + len(window) - 1 < found.total_lines,
    }


# --------------------------------------------------------------------------- funções

NO_ROUTINES = "grafo sem o texto das funções da base: rode o scan novo, com o banco"
_PHP_LABELS = ("File", "Function", "Method")


def _routine_users(conn: sqlite3.Connection, node_id: str) -> dict[str, int]:
    """Quantos nós chamam a função: PHP (arquivo, função ou método), outras funções da base e
    tabelas cujo trigger a dispara."""
    counts = {"php": 0, "functions": 0, "triggers": 0}
    rows = conn.execute(
        "SELECT substr(src, 1, instr(src, ':') - 1), count(DISTINCT src) FROM edges "
        "WHERE dst = ? AND type IN ('CALLS_RPC', 'CALLS', 'TRIGGERS') GROUP BY 1",
        (node_id,),
    ).fetchall()
    for label, total in rows:
        if label in _PHP_LABELS:
            counts["php"] += total
        elif label == "PgFunction":
            counts["functions"] += total
        elif label == "Table":
            counts["triggers"] += total
    return counts


def _routine_tables(conn: sqlite3.Connection, node_id: str, limit: int) -> dict[str, Any]:
    """Tabelas que a função lê e grava, com a confiança de cada aresta."""
    rows = conn.execute(
        "SELECT dst, type, source, confidence FROM edges "
        "WHERE src = ? AND type IN ('QUERIES', 'INSERTS', 'UPDATES', 'DELETES') "
        "ORDER BY dst, type LIMIT ?",
        (node_id, limit + 1),
    ).fetchall()
    items = [
        {"table": dst, "type": kind, "source": source, "confidence": confidence}
        for dst, kind, source, confidence in rows[:limit]
    ]
    return {"items": items, "truncated": len(rows) > limit}


def _routine_candidate(
    conn: sqlite3.Connection, row: routines.RoutineSource, delegates_to: str | None = None
) -> dict[str, Any]:
    found = repo.node(conn, row.node_id)
    attrs = found.attrs if found else {}
    candidate: dict[str, Any] = {
        "id": row.node_id,
        "schema": row.schema,
        "args": attrs.get("args", []),
        "returns": attrs.get("returns"),
        "language": row.language,
        "generated": row.generated,
    }
    if delegates_to is not None:
        # Repassa para outra sobrecarga da família, mas sem destino único (ambíguo).
        candidate["delegates_to"] = delegates_to
    return candidate


def _resolved_dict(resolution: routines.RoutineResolution) -> dict[str, Any]:
    """A evidência do repasse, para a resposta MCP (`resolved_by == "repasse"`)."""
    chosen_id = resolution.chosen.node_id if resolution.chosen else None
    return {
        "by": "repasse",
        "chain": list(resolution.chain),
        "others": [c.node_id for c in resolution.candidates if c.node_id != chosen_id],
    }


def routine(
    catalog: ProjectCatalog,
    project: str,
    name: str,
    offset: int = 1,
    limit: int = 200,
    rules_kind: str | None = None,
    rules_level: str | None = None,
) -> dict[str, Any]:
    """Texto de uma função da base (`pg_get_functiondef`, como o scan leu), em páginas de linhas.

    `name`: ID do nó, `schema.nome` ou só o nome. Várias assinaturas → `candidates` (sem texto),
    salvo quando as outras só repassam para uma: nesse caso resolve sozinho e a
    resposta traz `resolved`.

    `rules` (validações, avisos, decisões, tratamento de exceção e comentários, com a
    condição e a linha) e `profile` (parâmetros, retorno, contagens e variáveis declaradas).
    `rules_kind`/`rules_level` filtram `rules`; sem eles, o padrão esconde `diagnostic`.
    """
    limit = _cap(limit, MAX_ROUTINE_LINES)
    offset = max(1, offset)
    rule_kinds = (rules_kind,) if rules_kind else None

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        if not routines.has_routines(conn):
            raise QueryError(NO_ROUTINES)
        found = routines.find_routines(conn, name)
        if not found:
            hints = routines.suggest_names(conn, name)
            more = f" Parecidas: {', '.join(hints)}." if hints else ""
            raise QueryError(f"função inexistente no projeto {project}: {name}.{more}")
        resolution: routines.RoutineResolution | None = None
        if len(found) > 1:
            resolution = routines.resolve_family(conn, name)
            if resolution.resolved_by != "repasse" or resolution.chosen is None:
                return {
                    "project": project,
                    "name": name,
                    "ambiguous": True,
                    "message": (
                        "mais de uma assinatura ou schema: chame de novo com o id de uma delas"
                    ),
                    "candidates": [
                        _routine_candidate(conn, r, resolution.delegates.get(r.node_id))
                        for r in found
                    ],
                }
            row = resolution.chosen
        else:
            row = found[0]
        node_row = repo.node(conn, row.node_id)
        attrs = node_row.attrs if node_row else {}
        lines = routines.split_lines(row.ddl)
        start = min(offset, len(lines))
        window = lines[start - 1 : start - 1 + limit]
        end = start + len(window) - 1
        result: dict[str, Any] = {
            "project": project,
            "id": row.node_id,
            "schema": row.schema,
            "name": row.name,
            "args": attrs.get("args", []),
            "returns": attrs.get("returns"),
            "language": row.language,
            "generated": row.generated,
            "read_at": row.read_at,
            "sha1": row.sha1,
            "total_lines": len(lines),
            "start_line": start,
            "end_line": end,
            "lines": [f"{start + i}: {text}" for i, text in enumerate(window)],
            "truncated": start > 1 or end < len(lines),
            "used_by": _routine_users(conn, row.node_id),
            "tables": _routine_tables(conn, row.node_id, MAX_LIST),
            # Por onde a função é usada; nulo em grafo anterior (rode o scan novo).
            "origin": routine_origin.read_origin(conn, row.node_id),
            # Validações, avisos, decisões, exceção e comentários; nulo sem a tabela.
            "rules": routines.read_rules(
                conn, row.node_id, kinds=rule_kinds, level=rules_level, limit=MAX_RULES
            ),
            "profile": routines.read_profile(conn, row.node_id),
            # Instruções SQL e as colunas de cada uma; nulo sem a tabela.
            "queries": routines.read_queries(conn, row.node_id, limit=MAX_QUERIES),
        }
        if resolution is not None:
            result["resolved"] = _resolved_dict(resolution)
        return result

    return _with_conn(catalog, project, run)


NO_RULES = "grafo sem as regras das funções da base: rode o scan novo"
NO_FIELDS = "grafo sem os campos das funções da base: rode o scan novo"


def column_usage(
    catalog: ProjectCatalog,
    project: str,
    table: str,
    column: str,
    role: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Funções que leem, gravam, filtram, juntam ou ordenam por uma coluna de uma tabela, com a
    linha e o texto (cortado) da consulta — "campo → funções".

    `table`: `schema.nome` ou só o nome (sem schema, com um candidato só). `role`: `read`,
    `write`, `filter`, `join` ou `sort`.
    """
    limit = _cap(limit, MAX_QUERIES)
    offset = max(0, offset)

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        table_id_ = _resolve_table_id(conn, table)
        if not routines.has_fields(conn):
            raise QueryError(NO_FIELDS)
        if not _column_known(conn, table_id_, column):
            hints = _similar_columns(conn, table_id_, column)
            more = f" Parecidas: {', '.join(hints)}." if hints else ""
            raise QueryError(f"a tabela {table} não tem a coluna {column}.{more}")
        found = routines.column_usage(
            conn, table_id_, column, role=role, limit=limit, offset=offset
        )
        assert found is not None  # `has_fields` já confirmou a tabela acima
        return {"project": project, "table": table_id_, "column": column.lower(), **found}

    return _with_conn(catalog, project, run)


def _resolve_table_id(conn: sqlite3.Connection, table: str) -> str:
    text = table.strip()
    if text.lower().startswith("table:"):
        return text
    schema, dot, bare = text.lower().rpartition(".")
    if dot:
        node_id = f"Table:{schema}.{bare}"
        if repo.node(conn, node_id) is not None:
            return node_id
        raise QueryError(f"tabela inexistente: {table}")
    rows = conn.execute(
        "SELECT id FROM nodes WHERE label = 'Table' AND json_extract(attrs, '$.name') = ?",
        (bare,),
    ).fetchall()
    if len(rows) == 1:
        return str(rows[0][0])
    if not rows:
        raise QueryError(f"tabela inexistente: {table}")
    options = ", ".join(sorted(str(r[0]) for r in rows))
    raise QueryError(f"tabela {table} existe em mais de um schema: {options}")


def _known_columns(conn: sqlite3.Connection, table_id_: str) -> list[str] | None:
    """As colunas que o schema conhece para a tabela; `None` sem essa informação (grafo sem
    `--schema`/`--schema-db`, ou tabela placeholder) — aí não há como dizer que a coluna não
    existe, e `column_usage` segue só pelo que `routine_fields` registrou."""
    node = repo.node(conn, table_id_)
    if node is None or not node.attrs.get("columns"):
        return None
    return [str(c) for c in node.attrs["columns"]]


def _column_known(conn: sqlite3.Connection, table_id_: str, column: str) -> bool:
    known = _known_columns(conn, table_id_)
    return known is None or column.lower() in {c.lower() for c in known}


def _similar_columns(conn: sqlite3.Connection, table_id_: str, column: str) -> list[str]:
    known = _known_columns(conn, table_id_) or []
    lowered = column.lower()
    return sorted(c for c in known if lowered in c.lower())[:MAX_LIST]


def routine_rules(
    catalog: ProjectCatalog,
    project: str,
    query: str | None = None,
    kind: str | None = None,
    level: str | None = None,
    topic: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Regras em todas as funções da base, por texto da mensagem/condição, nível, tipo e assunto.

    Responde por exemplo "quais funções validam saldo?" sem ler o texto de cada uma. `kind`:
    `validation`, `error`, `warning`, `diagnostic`, `decision`, `handler` ou `comment`. `topic`
    aceita a chave ou o rótulo («multa»); exige o índice de `php-map
    index-formulas`. Cada item ganha `topic`.
    """
    limit = _cap(limit, MAX_RULES)
    offset = max(0, offset)
    topic_key = topics_module.resolve_topic_key(topic) if topic else None
    if topic and topic_key is None:
        options = ", ".join(sorted(topics_module.LABELS))
        raise QueryError(f"assunto desconhecido: {topic!r}. Use uma das chaves: {options}")

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        found = routines.search_rules(
            conn, query=query, kind=kind, level=level, topic=topic_key, limit=limit, offset=offset
        )
        if found is None:
            raise QueryError(NO_RULES)
        return {"project": project, **found}

    return _with_conn(catalog, project, run)


NO_ORIGINS = "grafo sem a origem das funções da base: rode o scan novo"


def routine_origins(
    catalog: ProjectCatalog,
    project: str,
    status: str | None = None,
    module: str | None = None,
    generated: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Relatório de origem das funções da base: contagem por status e a lista paginada.

    `sem_chamador` não é código morto: SQL dinâmico, JavaScript e agendamentos não aparecem no
    grafo. É a classificação completa (toda função tem status e motivo), não a origem completa.
    """
    if status is not None and status not in routine_origin.STATUSES:
        raise QueryError(f"status aceita {', '.join(routine_origin.STATUSES)}")
    limit = _cap(limit, MAX_ORIGINS)
    offset = max(0, offset)

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        report = routine_origin.list_origins(
            conn, status=status, module=module, generated=generated, limit=limit, offset=offset
        )
        if report is None:
            raise QueryError(NO_ORIGINS)
        return {"project": project, **report}

    return _with_conn(catalog, project, run)


# --------------------------------------------------------------------------- camadas


def layers(
    catalog: ProjectCatalog,
    project: str,
    path: str,
    min_confidence: float = layers_module.DEFAULT_MIN_CONFIDENCE,
    max_paths: int = 10,
    max_down: int = 60,
) -> dict[str, Any]:
    """Camadas de um fonte: para cima, os caminhos até o menu (menu → tela → JS → rota →
    controller → este fonte); para baixo, o que ele alcança por camada (service, repository,
    model, tabela). `path`: arquivo (caminho, nome ou trecho) ou o id de uma `Class:`/`Method:`.
    A camada é deduzida do nome; cada passo traz o tipo de aresta e a confiança."""
    max_paths = _cap(max_paths, layers_module.MAX_UP_PATHS)
    max_down = _cap(max_down, layers_module.MAX_DOWN_NODES)
    options = layers_module.LayerOptions(
        min_confidence=max(0.0, min(1.0, min_confidence)),
        max_up_paths=max_paths,
        max_down_nodes=max_down,
    )

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        match = layers_module.resolve_target(conn, path)
        if match.file is None:
            more = f" Candidatos: {', '.join(match.candidates[:8])}" if match.candidates else ""
            raise QueryError(f"fonte não encontrado ou ambíguo: {path}.{more}")
        result = layers_module.file_layers(conn, match.file, options)
        return {"project": project, **result.to_json()}

    return _with_conn(catalog, project, run)


# --------------------------------------------------------------------------- fronteira


def _others(catalog: ProjectCatalog, project: str) -> list[Path]:
    return [p.db for p in catalog.projects if p.slug != project]


def cut_plan(
    catalog: ProjectCatalog,
    project: str,
    natureza: str | None = None,
    protocol: str | None = None,
    endpoints: str | None = None,
    limit: int = 50,
    depth: int = boundary.DEFAULT_DEPTH,
) -> dict[str, Any]:
    """Plano de corte por endpoint (leitura → réplica, comando → fila), com o `cross` dos outros
    projetos do catálogo quando houver."""
    if natureza is not None and natureza not in boundary.NATUREZAS:
        raise QueryError(f"natureza aceita: {', '.join(boundary.NATUREZAS)}")
    target = catalog.get(project)
    others = _others(catalog, project)
    selection = endpoints or ("matched" if others else "called")
    try:
        report = boundary.build_report(
            target.db, others, selection=selection, depth=_cap(depth, 10)
        )
    except UsageError as exc:
        raise QueryError(str(exc)) from exc
    rows = [
        p
        for p in report.endpoints
        if (natureza is None or p.natureza == natureza)
        and (protocol is None or p.protocol == protocol)
    ]
    cap = _cap(limit, MAX_CROSS)
    return {
        "project": report.project,
        "selection": report.selection,
        "depth": report.depth,
        "natureza_counts": report.natureza_counts,
        "total": len(rows),
        "endpoints": [compact(asdict(p)) for p in rows[:cap]],
        "truncated": len(rows) > cap,
        "warnings": list(report.warnings),
    }


def module_boundary(catalog: ProjectCatalog, project: str, module: str) -> dict[str, Any]:
    target = catalog.get(project)
    try:
        card = boundary.build_module_card(target.db, module)
    except UsageError as exc:
        raise QueryError(str(exc)) from exc
    result: dict[str, Any] = compact(asdict(card))
    result["cost_formula"] = boundary.COST_FORMULA
    return result


def extraction_candidates(catalog: ProjectCatalog, project: str, top: int = 15) -> dict[str, Any]:
    target = catalog.get(project)
    has_modules, total, ranking = boundary.build_candidates(target.db, _cap(top, 100))
    return {
        "project": project,
        "modules_total": total,
        "cost_formula": boundary.COST_FORMULA,
        "note": "apoio à decisão, não veredito: confira os componentes de cada módulo",
        "candidates": [asdict(c) for c in ranking],
        "warning": None if has_modules else boundary.NO_MODULES_WARNING,
    }


# --------------------------------------------------------------------------- ficha


def routine_card(
    catalog: ProjectCatalog,
    project: str,
    routine: str,
    source_lines: int = cards.DEFAULT_SOURCE_LINES,
    format: str = "markdown",
) -> dict[str, Any]:
    """Ficha em português da função `routine` (sem IA): para que serve, onde é usada, o que
    recebe e devolve, dados que usa (com rótulo do dicionário), o que valida, o que chama e quem
    a chama, texto original. `format`: `markdown` (pronto para colar numa IA) ou `json`
    (estrutura). `routine` aceita o id completo, `schema.nome` ou só o nome; nome com várias
    assinaturas que só repassam para uma resolve sozinho, com `resolved` na resposta.
    """
    if format not in ("markdown", "json"):
        raise QueryError("format aceita markdown ou json")
    source_lines = _cap(source_lines, 2000, minimum=0)

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        found = routines.find_routines(conn, routine)
        resolution: routines.RoutineResolution | None = None
        if len(found) > 1:
            resolution = routines.resolve_family(conn, routine)
            if resolution.resolved_by != "repasse" or resolution.chosen is None:
                return {
                    "project": project,
                    "ambiguous": True,
                    "message": (
                        "mais de uma assinatura ou schema: chame de novo com o id de uma delas"
                    ),
                    "candidates": [
                        _routine_candidate(conn, r, resolution.delegates.get(r.node_id))
                        for r in found
                    ],
                }
        # Passa o nome original (não o id já resolvido): `function_card` refaz a mesma resolução
        # e, com repasse, acrescenta o aviso em `warnings`.
        card = cards.function_card(conn, routine, source_lines=source_lines)
        if format == "json":
            result: dict[str, Any] = {"project": project, "card": cards.function_as_dict(card)}
        else:
            result = {
                "project": project,
                "id": card.id,
                "markdown": cards.render_function_markdown(card),
            }
        if resolution is not None and resolution.resolved_by == "repasse":
            result["resolved"] = _resolved_dict(resolution)
        return result

    return _with_conn(catalog, project, run)


def screen_card(
    catalog: ProjectCatalog,
    project: str,
    entry: str,
    depth: int = rules.DEFAULT_DEPTH,
    format: str = "markdown",
) -> dict[str, Any]:
    """Ficha em português da tela `entry` (`emp4_x.php` ou `File:emp4_x.php`): menu, dados
    gravados e lidos, funções da base chamadas (com um resumo das validações de cada), mensagens
    de validação e erro, anotações do autor. `format`: `markdown` ou `json`."""
    if format not in ("markdown", "json"):
        raise QueryError("format aceita markdown ou json")

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        card = cards.screen_card(conn, entry, depth=_cap(depth, repo.MAX_DEPTH, 1))
        if format == "json":
            return {"project": project, "card": cards.as_dict(card)}
        return {
            "project": project,
            "entry": card.entry,
            "markdown": cards.render_screen_markdown(card, level=1),
        }

    return _with_conn(catalog, project, run)


# --------------------------------------------------------------------------- de onde vem (55)


def value_origin(
    catalog: ProjectCatalog,
    project: str,
    question: str,
    routine: str | None = None,
    targets: list[str] | None = None,
    max_words: int = value_origin_module.DEFAULT_MAX_WORDS,
) -> dict[str, Any]:
    """ "De onde vem este valor?", em duas chamadas, sem IA:

    1. **Sem `targets`:** interpreta a pergunta (termos × rótulos do dicionário, só dentro do
       alcance de `routine`) e devolve os candidatos (`target`, `label`, `reason`, `score`).
       Sem `routine`, sugere funções pela busca, com aviso.
    2. **Com `targets`** (`schema.tabela.coluna`, de um candidato da fase 1 ou digitado): a
       cadeia de cada alvo até a coluna base e até a tela que a cadastra (ou a hipótese pelo
       nome), e o dossiê em Markdown — pronto para colar numa IA sem acesso ao mapa.
    """

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        try:
            interpretation = value_origin_module.interpret(conn, question, routine)
        except value_origin_module.ValueOriginError as exc:
            raise QueryError(str(exc)) from exc
        if not targets:
            return {
                "project": project,
                "function": interpretation.function,
                "suggested_functions": list(interpretation.suggested_functions),
                "reach": len(interpretation.reach),
                "candidates": [
                    {
                        "target": (
                            f"{value_origin_module.short(c.table)}.{c.column}"
                            if c.column
                            else value_origin_module.short(c.table)
                        ),
                        "label": c.label,
                        "reason": c.reason,
                        "score": c.score,
                    }
                    for c in interpretation.candidates
                ],
                "warnings": list(interpretation.warnings),
                "next": "chame de novo com targets=['schema.tabela.coluna', ...]",
            }
        try:
            origin = value_origin_module.trace_value(conn, interpretation, targets)
            dossier = value_origin_module.render_dossier(
                origin, max_words=_cap(max_words, 6000, 500)
            )
        except value_origin_module.ValueOriginError as exc:
            raise QueryError(str(exc)) from exc
        return {
            "project": project,
            "dossier": dossier,
            "words": len(dossier.split()),
            "warnings": list(origin.warnings),
        }

    return _with_conn(catalog, project, run)


# --------------------------------------------------------------------------- fórmulas (71)


def formulas(
    catalog: ProjectCatalog,
    project: str,
    routine: str | None = None,
    depth: int = formulas_module.DEFAULT_DEPTH,
    max_words: int = formulas_module.DEFAULT_MAX_WORDS,
    field: str | None = None,
    menu: str | None = None,
    table: str | None = None,
    max_readers: int = table_access_module.DEFAULT_MAX_READERS,
    page: int = 1,
    structured: bool = False,
) -> dict[str, Any]:
    """Análise profunda de uma função (sem IA): cada cálculo (destino, fórmula, linha),
    os dados de entrada levados até `tabela.campo` e o menu onde cada um é cadastrado, mais o que
    não foi possível rastrear. Desce às funções auxiliares chamadas dentro de uma fórmula (até
    `depth` níveis e 10 funções). Com `field` (`schema.tabela.coluna`): onde o campo é
    cadastrado, as funções que o gravam (fórmula e entradas) e quem o usa. Com `menu` (id
    `BusinessAction:menu:N` ou trecho do caminho): o que o item de menu toca, as funções
    da base com a contagem de fórmulas e as tabelas que a tela grava. Com `table`
    (`schema.tabela`): os campos, onde a tabela é cadastrada e **todo o código PHP que a
    grava e a lê** (SQL direto, DAO, model Eloquent e migrações, cada um com operação, linhas,
    camada e menu), com a contagem completa de leitores (`max_readers` os lista, até 500). O
    dossiê de `field` traz a mesma seção em `access`. Para cada gravação pelo DAO, os
    campos que o código PHP preenche (`fills`: atribuição `$oDao->campo = expr` ou o formulário da
    tela, que o DAO lê do `$_POST`); o valor é o texto do código, não o valor resolvido. Um dos
    quatro.

    O dossiê de função abre com a **Visão geral** (quantas fórmulas por assunto —
    correção, juros, multa, desconto, inflator… — e por função; o assunto é pista pelo nome ou
    pela função chamada) e a resposta traz `overview` e `suggestions` (perguntas sugeridas) na
    página 1.

    O dossiê **inteiro** (sem o corte de `max_words`/tetos de linhas) é dividido em
    páginas de ~`max_words` palavras nas fronteiras de `## ` (`page`, 1…N); `pages`, `next_page` e
    `sections` (em que página está cada seção) acompanham. `dossier` (o JSON estruturado, igual em
    todas as páginas) só vem com `structured=true`. `page` maior que o total é erro."""
    if sum(bool(x) for x in (routine, field, menu, table)) != 1:
        raise QueryError("informe routine, field, menu ou table (um dos quatro)")
    words_cap = _cap(max_words, 8000, 300)
    wanted_page = max(1, page)

    def paged(
        markdown_full: str,
        *,
        dossier_data: dict[str, Any],
        page1_extra: dict[str, Any] | None = None,
        resolved: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pages = paging.paginate(markdown_full, words_cap)
        if wanted_page > len(pages):
            raise QueryError(f"página {wanted_page} não existe: o dossiê tem {len(pages)} páginas")
        current = pages[wanted_page - 1]
        result: dict[str, Any] = {
            "project": project,
            "markdown": current.markdown,
            "page": current.number,
            "pages": current.total,
            "next_page": current.number + 1 if current.number < current.total else None,
            "sections": paging.section_index(pages),
        }
        if current.number == 1 and page1_extra:
            result.update(page1_extra)
        if structured:
            result["dossier"] = dossier_data
        if resolved is not None:
            result["resolved"] = resolved  # em todas as páginas, não só na 1ª
        return result

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        if menu:
            try:
                menu_view = menu_dossier_module.menu_dossier(conn, menu)
            except menu_dossier_module.AmbiguousMenuError as exc:
                return {
                    "project": project,
                    "ambiguous": True,
                    "candidates": [{"id": i, "menu_path": path} for i, path in exc.candidates],
                    "markdown": str(exc),
                }
            return paged(
                menu_dossier_module.render_menu_dossier(menu_view),
                dossier_data=menu_dossier_module.menu_dossier_json(menu_view),
                page1_extra={
                    "suggestions": topics_module.suggestion_json(
                        topics_module.suggestions_for_menu(menu_view)
                    )
                },
            )
        if table:
            dossier_table = formulas_module.table_dossier(
                conn, table, max_readers=_cap(max_readers, table_access_module.MAX_READERS, 0)
            )
            return paged(
                formulas_module.render_table_dossier(dossier_table, max_words=words_cap, full=True),
                dossier_data=formulas_module.table_dossier_json(dossier_table),
                page1_extra={
                    "suggestions": topics_module.suggestion_json(
                        topics_module.suggestions_for_table(dossier_table)
                    )
                },
            )
        if field:
            dossier_field = formulas_module.field_dossier(
                conn, field, max_readers=_cap(max_readers, table_access_module.MAX_READERS, 0)
            )
            return paged(
                formulas_module.render_field_dossier(dossier_field, max_words=words_cap, full=True),
                dossier_data=formulas_module.field_dossier_json(dossier_field),
                page1_extra={
                    "suggestions": topics_module.suggestion_json(
                        topics_module.suggestions_for_field(dossier_field)
                    )
                },
            )
        assert routine is not None
        try:
            # Nome com várias assinaturas que só repassam para uma resolve sozinho
            # dentro de `calc_dossier`; só levanta se continuar ambíguo.
            # No modo do MCP o teto de fórmulas sobe para 400 (em vez de 60), porque
            # quem corta pelo tamanho da resposta agora é a paginação, não o dossiê.
            dossier = formulas_module.calc_dossier(
                conn,
                routine,
                depth=_cap(depth, 4, 0),
                max_formulas=formulas_module.MAX_PAGED_FORMULAS,
            )
            markdown_full = formulas_module.render_calc_dossier(
                dossier, max_words=words_cap, full=True
            )
        except formulas_module.AmbiguousRoutineError as exc:
            raise QueryError(str(exc)) from exc
        resolved = None
        if dossier.resolved_by == "repasse":
            resolved = {
                "by": "repasse",
                "chain": list(dossier.resolved_chain),
                "others": list(dossier.resolved_others),
            }
        return paged(
            markdown_full,
            dossier_data=formulas_module.dossier_json(dossier),
            page1_extra={
                "overview": topics_module.overview_json(topics_module.overview(dossier)),
                "suggestions": topics_module.suggestion_json(
                    topics_module.suggestions_for_calc(dossier)
                ),
            },
            resolved=resolved,
        )

    return _with_conn(catalog, project, run)


# --------------------------------------------------------------------------- assunto (89)

MAX_FORMULA_SEARCH = 50


def formula_search(
    catalog: ProjectCatalog,
    project: str,
    query: str | None = None,
    topic: str | None = None,
    module: str | None = None,
    routine: str | None = None,
    group_by: str = "routine",
    limit: int = formula_index_module.DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Busca fórmulas por assunto e/ou termo livre em **toda a base** (sem IA):
    responde "quais funções calculam juros?" ou "onde se calcula desconto do IPTU?" numa
    chamada, sem ler função por função. Exige o índice de `php-map index-formulas`.

    Pelo menos um entre `query` e `topic`. `topic` aceita a chave ou o rótulo em português
    («correção monetária»). `query` casa com o assunto (chave e rótulo), o destino, o rótulo do
    dicionário da coluna gravada, a expressão legível e o nome da função. `routine` limita a uma
    função (resolve por repasse entre sobrecargas). `group_by="routine"` (padrão):
    por função, a contagem, até 3 fórmulas de exemplo e o módulo, maior contagem primeiro.
    `group_by="formula"`: as fórmulas em si. O assunto é **pista**: confira na fórmula com
    `formulas(routine=…)`.
    """
    if not query and not topic:
        raise QueryError("informe query ou topic (ao menos um)")
    if group_by not in ("routine", "formula"):
        raise QueryError("group_by aceita routine ou formula")
    limit = _cap(limit, MAX_FORMULA_SEARCH)
    offset = max(0, offset)

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        routine_id = None
        if routine:
            found = routines.find_routines(conn, routine)
            if not found:
                hints = routines.suggest_names(conn, routine)
                more = f" Parecidas: {', '.join(hints)}." if hints else ""
                raise QueryError(f"função inexistente no projeto {project}: {routine}.{more}")
            if len(found) > 1:
                resolution = routines.resolve_family(conn, routine)
                if resolution.resolved_by != "repasse" or resolution.chosen is None:
                    return {
                        "project": project,
                        "ambiguous": True,
                        "message": (
                            "mais de uma assinatura ou schema: chame de novo com o id de uma delas"
                        ),
                        "candidates": [
                            _routine_candidate(conn, r, resolution.delegates.get(r.node_id))
                            for r in found
                        ],
                    }
                routine_id = resolution.chosen.node_id
            else:
                routine_id = found[0].node_id
        found_search = formula_index_module.search_formulas(
            conn,
            query=query,
            topic=topic,
            module=module,
            routine=routine_id,
            group_by=group_by,
            limit=limit,
            offset=offset,
        )
        if found_search is None:
            raise QueryError(formula_index_module.NO_FORMULA_INDEX)
        markdown = formula_index_module.render_formula_search(
            found_search, topic=topic, query=query
        )
        return {"project": project, "markdown": markdown, **found_search}

    return _with_conn(catalog, project, run)


def resolve_menu(catalog: ProjectCatalog, project: str, menu: str) -> dict[str, Any]:
    """O item de menu por id ou por trecho do caminho: `{id, menu_path}` ou, se o
    trecho casar com vários, `{ambiguous, candidates}`."""

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        try:
            menu_id = menu_dossier_module.resolve_menu(conn, menu)
        except menu_dossier_module.AmbiguousMenuError as exc:
            return {
                "ambiguous": True,
                "candidates": [{"id": i, "menu_path": path} for i, path in exc.candidates],
            }
        attrs = value_origin_module.node_attrs(conn, menu_id)
        return {"id": menu_id, "name": attrs.get("name"), "menu_path": attrs.get("menu_path")}

    return _with_conn(catalog, project, run)


# --------------------------------------------------------------------------- busca de campo (75)


def field_search(
    catalog: ProjectCatalog,
    project: str,
    term: str,
    limit: int = field_search_module.DEFAULT_LIMIT,
    module: str | None = None,
) -> dict[str, Any]:
    """Colunas cujo nome no dicionário casa com um termo de negócio, no banco inteiro
    (sem IA): as usadas em cálculo na frente, com as funções que leem e o menu onde a tabela é
    cadastrada. Termo genérico demais (só «valor») não busca e devolve o aviso."""

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        result = field_search_module.field_search(
            conn, term, limit=_cap(limit, field_search_module.MAX_LIMIT), module=module
        )
        return {
            "project": project,
            "markdown": field_search_module.render_field_search(result),
            "search": field_search_module.field_search_json(result),
            "suggestions": topics_module.suggestion_json(
                topics_module.suggestions_for_term(result)
            ),
        }

    return _with_conn(catalog, project, run)
