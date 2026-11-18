"""Tela Comunicação: endpoints e rotas numa lista só, sob demanda.

Lê o grafo servido (somente leitura, sem carregá-lo inteiro): os nós `Endpoint` e `Route`, as
arestas que chegam nos endpoints (`SERVES`, `CALLS_REMOTE`) e as `ROUTES_TO` das rotas. A lista
é montada na hora, filtrada e **paginada aqui** (o navegador nunca recebe os milhares de ações
de RPC do e-cidade); o alcance (`reaches`) é calculado só para as linhas da página, com a mesma
busca do `cross` (`Neighborhood`/`lazy_walk`).

O outro lado da costura vem do `CrossReport`, quando há outro projeto cadastrado
com grafo; sem ele, a linha só diz que o sistema que serve não está mapeado.
"""

import json
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from php_map.analysis.cross import (
    CASADO,
    CROSS_EDGES,
    SEM_CLIENTE,
    SEM_SERVIDOR,
    CrossEndpoint,
    CrossReport,
    Neighborhood,
    lazy_walk,
)
from php_map.errors import QueryError
from php_map.graph.model import module_id

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
CALLER_SAMPLE = 3
REACH_TABLES_SAMPLE = 5
REACH_DEPTH = 3
ENTRA, SAI = "entra", "sai"
EXTERNAL = "externo"
HTTP = "http"
_PROTOCOL_ORDER = {"soap": 0, "rpc": 1, "http": 2, EXTERNAL: 3}


@dataclass(frozen=True, slots=True)
class Impl:
    """Quem implementa: o nó e onde (arquivo e linha)."""

    node: str
    path: str | None
    line: int | None


@dataclass(frozen=True, slots=True)
class Caller:
    project: str | None  # None = o próprio grafo servido
    node: str
    path: str | None
    line: int | None
    module: str | None
    args: dict[str, Any]
    occurrences: int


@dataclass(frozen=True, slots=True)
class ReachSummary:
    tables: tuple[str, ...]
    modules: tuple[str, ...]
    nodes: int


@dataclass(frozen=True, slots=True)
class Row:
    endpoint_id: str
    node_label: str  # "Endpoint" | "Route"
    system: str
    protocol: str
    category: str  # protocolo para filtro: "externo" quando o sistema é `externo`
    kind: str | None
    name: str
    direction: str
    resolved: bool
    params: tuple[dict[str, Any], ...]
    implemented_by: tuple[Impl, ...]
    callers: int  # chamadas neste grafo (soma de `occurrences`)
    caller_sample: tuple[Caller, ...]
    module: str | None
    reaches: ReachSummary | None = None
    status: str | None = None  # do `cross`: casado | chamado_sem_servidor | servido_sem_cliente


@dataclass(frozen=True, slots=True)
class Facet:
    value: str
    count: int


@dataclass(frozen=True, slots=True)
class Counters:
    serves: int
    calls: int
    with_params: int
    orphans: int | None  # só com o `cross` (outro projeto com grafo)
    cross_available: bool


@dataclass(frozen=True, slots=True)
class Page:
    total: int
    items: tuple[Row, ...]
    counters: Counters
    systems: tuple[Facet, ...]
    protocols: tuple[Facet, ...]
    modules: tuple[Facet, ...]
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class Counterpart:
    """O outro lado da costura, vindo do `cross`."""

    available: bool
    message: str | None
    status: str | None = None
    served_by: str | None = None
    servers: tuple[Impl, ...] = ()
    params: tuple[dict[str, Any], ...] = ()
    reaches: ReachSummary | None = None
    callers: tuple[Caller, ...] = ()  # chamadas vindas dos outros projetos
    system: str | None = None


@dataclass(frozen=True, slots=True)
class Detail:
    row: Row
    callers: tuple[Caller, ...]
    attrs: dict[str, Any] = field(default_factory=dict)
    counterpart: Counterpart | None = None


# --------------------------------------------------------------------------- carga


@dataclass(slots=True)
class _Edge:
    src: str
    confidence: float
    evidence: dict[str, Any]
    attrs: dict[str, Any]


def normalize(text: str) -> str:
    """Minúsculas e sem acento, para a busca por texto."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _chunks(items: Sequence[str], size: int = 500) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _incoming(conn: sqlite3.Connection) -> tuple[dict[str, list[_Edge]], dict[str, list[_Edge]]]:
    """`SERVES` e `CALLS_REMOTE` que chegam em `Endpoint`, pelo índice de `dst`."""
    serves: dict[str, list[_Edge]] = {}
    calls: dict[str, list[_Edge]] = {}
    rows = conn.execute(
        "SELECT src, dst, type, confidence, evidence, attrs FROM edges"
        " WHERE dst >= 'Endpoint:' AND dst < 'Endpoint;' ORDER BY id"
    )
    for src, dst, type_, confidence, evidence, attrs in rows:
        target = serves if type_ == "SERVES" else calls if type_ == "CALLS_REMOTE" else None
        if target is not None:
            target.setdefault(dst, []).append(
                _Edge(src, confidence, json.loads(evidence), json.loads(attrs))
            )
    return serves, calls


def _routes_to(conn: sqlite3.Connection) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    out: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    rows = conn.execute(
        "SELECT src, dst, evidence FROM edges WHERE type = 'ROUTES_TO'"
        " AND src >= 'Route:' AND src < 'Route;' ORDER BY id"
    )
    for src, dst, evidence in rows:
        out.setdefault(src, []).append((dst, json.loads(evidence)))
    return out


def _node_paths(
    conn: sqlite3.Connection, ids: Iterable[str]
) -> dict[str, tuple[str | None, int | None]]:
    """`path` e `line_start` dos nós (os `Method` de `ROUTES_TO`)."""
    wanted = list(dict.fromkeys(ids))
    found: dict[str, tuple[str | None, int | None]] = {}
    for chunk in _chunks(wanted):
        rows = conn.execute(
            "SELECT id, json_extract(attrs, '$.path'), json_extract(attrs, '$.line_start')"
            f" FROM nodes WHERE id IN ({','.join('?' * len(chunk))})",
            chunk,
        )
        for node, path, line in rows:
            found[node] = (path, line)
    return found


def _split_endpoint(node_id: str) -> tuple[str, str, str]:
    _, _, rest = node_id.partition(":")
    system, _, rest = rest.partition(":")
    protocol, _, name = rest.partition(":")
    return system, protocol, name


def _file(path: str | None) -> str | None:
    return f"File:{path}" if path else None


def _file_of(edge: _Edge) -> str | None:
    """O `File:*` de quem aponta para o endpoint (o próprio nó, ou o arquivo da evidência)."""
    return edge.src if edge.src.startswith("File:") else _file(edge.evidence.get("path"))


def _implementer(edge: _Edge, node_paths: dict[str, tuple[str | None, int | None]]) -> Impl:
    """Onde mora quem serve: o próprio arquivo (`File`), ou a declaração do método/função
    (a evidência da `SERVES` é onde o endpoint foi declarado: o XML, o `setClass`)."""
    path, line = edge.evidence.get("path"), edge.evidence.get("line")
    if edge.src.startswith("File:"):
        own = edge.src.partition(":")[2]
        return Impl(edge.src, own, line if own == path else None)
    found = node_paths.get(edge.src)
    return Impl(edge.src, *found) if found and found[0] else Impl(edge.src, path, line)


def _caller(edge: _Edge, modules: dict[str, str | None], project: str | None = None) -> Caller:
    path = edge.evidence.get("path")
    file_node = _file_of(edge)
    return Caller(
        project,
        edge.src,
        path,
        edge.evidence.get("line"),
        modules.get(file_node or ""),
        dict(edge.evidence.get("args") or {}),
        int(edge.attrs.get("occurrences") or 1),
    )


def _sorted_callers(callers: Iterable[Caller]) -> tuple[Caller, ...]:
    """Por projeto, arquivo e linha: a mesma ordem do `cross`."""
    return tuple(
        sorted(callers, key=lambda c: (c.project or "", c.path or "", c.line or 0, c.node))
    )


def _top_module(callers: Sequence[Caller]) -> str | None:
    counts = Counter(c.module for c in callers if c.module)
    return min(counts, key=lambda m: (-counts[m], m)) if counts else None


@dataclass(slots=True)
class _Index:
    """Tudo o que a lista precisa, montado a partir do banco (sem os `reaches`)."""

    rows: list[Row]
    serve_edges: dict[str, list[_Edge]]


def _build(conn: sqlite3.Connection, nb: Neighborhood) -> _Index:
    serves, calls = _incoming(conn)
    routes_to = _routes_to(conn)
    endpoint_rows = [
        (node, json.loads(attrs))
        for node, attrs in conn.execute("SELECT id, attrs FROM nodes WHERE label = 'Endpoint'")
    ]
    route_rows = [
        (node, json.loads(attrs))
        for node, attrs in conn.execute("SELECT id, attrs FROM nodes WHERE label = 'Route'")
    ]
    server_nodes = [
        e.src for edges in serves.values() for e in edges if not e.src.startswith("File:")
    ]
    method_ids = [dst for targets in routes_to.values() for dst, _ in targets]
    method_paths = _node_paths(conn, [*method_ids, *server_nodes])

    # Módulos de todos os arquivos que aparecem (quem implementa, quem chama, o controller).
    files: set[str | None] = set()
    for edges in (*serves.values(), *calls.values()):
        for edge in edges:
            files.add(_file_of(edge))
    for path, _ in method_paths.values():
        files.add(_file(path))
    modules = nb.best_modules(sorted(f for f in files if f))

    rows: list[Row] = []
    for node, attrs in endpoint_rows:
        served, called = serves.get(node, []), calls.get(node, [])
        if not served and not called:
            continue  # nó solto: nem servido nem chamado
        system, protocol, name = _split_endpoint(node)
        system = str(attrs.get("system") or system)
        protocol = str(attrs.get("protocol") or protocol)
        caller_list = _sorted_callers(_caller(edge, modules) for edge in called)
        impl = tuple(_implementer(e, method_paths) for e in served)
        module = None
        if served:  # o primeiro implementador cujo arquivo tem módulo
            for edge in served:
                module = modules.get(_file_of(edge) or "")
                if module:
                    break
        else:
            module = _top_module(caller_list)
        rows.append(
            Row(
                endpoint_id=node,
                node_label="Endpoint",
                system=system,
                protocol=protocol,
                category=EXTERNAL if system == EXTERNAL else protocol,
                kind=attrs.get("kind"),
                name=str(attrs.get("name") or name),
                direction=ENTRA if served else SAI,
                resolved=bool(served) and attrs.get("resolved") is not False,
                params=tuple(attrs.get("params") or ()),
                implemented_by=impl,
                callers=sum(c.occurrences for c in caller_list),
                caller_sample=caller_list[:CALLER_SAMPLE],
                module=module,
            )
        )
    for node, attrs in route_rows:
        targets = routes_to.get(node, [])
        impl_list = [
            Impl(dst, *method_paths.get(dst, (evidence.get("path"), evidence.get("line"))))
            for dst, evidence in targets
        ]
        if not impl_list and attrs.get("path"):
            impl_list = [Impl(node, attrs.get("path"), attrs.get("line"))]
        module = None
        if attrs.get("module"):
            module = module_id(str(attrs["module"]))
        else:
            for item in impl_list:
                module = modules.get(_file(item.path) or "")
                if module:
                    break
        name = str(attrs.get("uri") or node.partition(":")[2])
        rows.append(
            Row(
                endpoint_id=node,
                node_label="Route",
                system="",  # a rota é do próprio projeto servido
                protocol=HTTP,
                category=HTTP,
                kind="route",
                name=name,
                direction=ENTRA,
                resolved=True,
                params=tuple(attrs.get("params") or ()),
                implemented_by=tuple(impl_list),
                callers=0,
                caller_sample=(),
                module=module,
            )
        )
    return _Index(rows, serves)


# --------------------------------------------------------------------------- lista


def _matches(row: Row, text: str) -> bool:
    haystack = normalize(
        " ".join(
            [
                row.endpoint_id,
                row.name,
                row.kind or "",
                " ".join(str(p.get("name", "")) for p in row.params),
                " ".join(i.path or "" for i in row.implemented_by),
            ]
        )
    )
    return all(term in haystack for term in normalize(text).split())


def _facets(values: Iterable[str | None]) -> tuple[Facet, ...]:
    counts = Counter(v for v in values if v)
    return tuple(Facet(v, counts[v]) for v in sorted(counts, key=lambda v: (-counts[v], v)))


def _reach(nb: Neighborhood, row: Row, serve_edges: dict[str, list[_Edge]]) -> ReachSummary | None:
    """Alcance (tabelas, módulos, nós) de um endpoint servido, na mesma busca do `cross`."""
    if row.node_label != "Endpoint" or row.direction != ENTRA:
        return None
    servers = [(e.src, json.dumps(e.evidence)) for e in serve_edges.get(row.endpoint_id, [])]
    walk = lazy_walk(nb, row.endpoint_id, depth=REACH_DEPTH, with_servers=servers)
    nb.prefetch_info(walk.steps)
    infos = {n: nb.info(n) for n in walk.steps if n != row.endpoint_id}
    tables = sorted(n for n, info in infos.items() if info.label == "Table")
    paths = sorted(
        {
            f"File:{info.path}"
            for info in infos.values()
            if info.path and info.label in ("Method", "Function", "Class", "File")
        }
    )
    mods = sorted({m for m in nb.best_modules(paths).values() if m})
    return ReachSummary(tuple(tables), tuple(mods), len(infos))


def _cross_endpoint(cross: CrossReport | None, endpoint_id: str) -> CrossEndpoint | None:
    if cross is None:
        return None
    for endpoint in cross.endpoints:
        if endpoint.endpoint_id == endpoint_id:
            return endpoint
    return None


def list_communication(
    conn: sqlite3.Connection,
    *,
    system: str | None = None,
    protocol: str | None = None,
    direction: str | None = None,
    module: str | None = None,
    search: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    cross: CrossReport | None = None,
) -> Page:
    """A lista paginada com filtros. Os contadores e as facetas vêm do conjunto inteiro."""
    if direction not in (None, ENTRA, SAI):
        raise QueryError("direction aceita entra ou sai")
    limit = max(0, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    nb = Neighborhood(conn, CROSS_EDGES)
    index = _build(conn, nb)
    by_cross = {e.endpoint_id: e.status for e in cross.endpoints} if cross else {}
    everything = [
        replace(r, status=by_cross.get(r.endpoint_id)) if r.node_label == "Endpoint" else r
        for r in index.rows
    ]

    def keep(row: Row) -> bool:
        return (
            (system is None or row.system == system)
            and (protocol is None or row.category == protocol)
            and (direction is None or row.direction == direction)
            and (module is None or row.module == module)
            and (not search or _matches(row, search))
        )

    kept = sorted(
        (r for r in everything if keep(r)),
        key=lambda r: (
            -r.callers,
            _PROTOCOL_ORDER.get(r.category, 9),
            r.name.lower(),
            r.endpoint_id,
        ),
    )
    page = kept[offset : offset + limit]
    items = tuple(replace(r, reaches=_reach(nb, r, index.serve_edges)) for r in page)
    orphans = (
        sum(1 for r in everything if r.status in (SEM_SERVIDOR, SEM_CLIENTE)) if cross else None
    )
    counters = Counters(
        serves=sum(1 for r in everything if r.direction == ENTRA),
        calls=sum(1 for r in everything if r.direction == SAI),
        with_params=sum(1 for r in everything if r.params),
        orphans=orphans,
        cross_available=cross is not None,
    )
    return Page(
        total=len(kept),
        items=items,
        counters=counters,
        systems=_facets(r.system for r in everything),
        protocols=_facets(r.category for r in everything),
        modules=_facets(r.module for r in everything),
        offset=offset,
        limit=limit,
    )


# --------------------------------------------------------------------------- detalhe


def _counterpart(row: Row, found: CrossEndpoint | None, cross: CrossReport | None) -> Counterpart:
    if cross is None:
        side = "que serve" if row.direction == SAI else "que chama"
        return Counterpart(
            available=False,
            message=(
                f"O sistema {side} ({row.system}) não está mapeado: "
                "cadastre o projeto para ver o outro lado."
            ),
            system=row.system,
        )
    if found is None:
        return Counterpart(
            available=True, message="Este endpoint não aparece no cruzamento.", system=row.system
        )
    reaches = ReachSummary(found.reaches.tables, found.reaches.modules, found.reaches.nodes)
    others = tuple(
        Caller(c.project, c.caller, c.path, c.line, c.module, dict(c.args), c.occurrences)
        for c in found.callers
    )
    message = None
    if found.status == SEM_SERVIDOR:
        message = "Ninguém serve este endpoint nos projetos mapeados."
    elif found.status == SEM_CLIENTE:
        message = "Nenhum dos projetos mapeados chama este endpoint (não quer dizer código morto)."
    servers = tuple(Impl(s.node, s.path, s.line) for s in found.servers)
    return Counterpart(
        available=True,
        message=message,
        status=found.status,
        served_by=found.served_by,
        servers=servers,
        params=tuple(found.params),
        reaches=reaches if found.served_by else None,
        callers=others,
        system=row.system,
    )


def endpoint_detail(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    local_project: str | None = None,
    cross: CrossReport | None = None,
) -> Detail:
    """O detalhe de um endpoint ou de uma rota: parâmetros, quem implementa, quem chama e o que
    alcança; com o `cross`, também o outro lado da costura."""
    nb = Neighborhood(conn, CROSS_EDGES)
    index = _build(conn, nb)
    row = next((r for r in index.rows if r.endpoint_id == node_id), None)
    if row is None:
        raise QueryError(f"endpoint ou rota não encontrado no grafo: {node_id}")
    attrs_row = conn.execute("SELECT attrs FROM nodes WHERE id = ?", (node_id,)).fetchone()
    attrs: dict[str, Any] = json.loads(attrs_row[0]) if attrs_row else {}
    if row.node_label == "Route":
        return Detail(row=row, callers=(), attrs=attrs, counterpart=None)

    serves, calls = _incoming(conn)
    called = calls.get(node_id, [])
    files = sorted({f for e in called if (f := _file_of(e))})
    modules = nb.best_modules(files)
    callers = _sorted_callers(_caller(e, modules, local_project) for e in called)
    found = _cross_endpoint(cross, node_id)
    reaches = _reach(nb, row, serves)
    full = replace(
        row,
        callers=sum(c.occurrences for c in callers),
        caller_sample=callers[:CALLER_SAMPLE],
        reaches=reaches,
        status=found.status if found else None,
    )
    counterpart = _counterpart(row, found, cross)
    return Detail(row=full, callers=callers, attrs=attrs, counterpart=counterpart)


__all__ = [
    "CASADO",
    "Caller",
    "Counterpart",
    "Counters",
    "Detail",
    "Facet",
    "Impl",
    "Page",
    "ReachSummary",
    "Row",
    "endpoint_detail",
    "list_communication",
]
