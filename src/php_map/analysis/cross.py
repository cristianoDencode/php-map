"""`cross`: cruza os grafos de projetos diferentes pelo ID do `Endpoint`.

A costura é o nó `Endpoint`: o ID nomeia **quem serve**, então o mesmo ID existe no
grafo de quem serve (com `SERVES`) e no de quem chama (placeholder, com `CALLS_REMOTE`). Cruzar é
um join por esse ID, sem fundir os grafos e sem gravar nada.

`analysis/` só lê grafo persistido (`estrutura-projeto.md`): aqui os bancos são abertos em modo
somente leitura e **nunca carregados inteiros**. O do e-cidade tem ~160 mil nós e ~840 mil arestas
(~1,5 GB no NetworkX); o cruzamento lê só os `Endpoint`, as arestas que chegam neles e a vizinhança
da busca em largura (`Neighborhood`, sob demanda).
"""

import json
import sqlite3
from collections import Counter, deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from php_map.analysis import (
    LEAF_LABELS,
    TABLE_OPS,
    TRAVERSED,
    Step,
    Walk,
)
from php_map.analysis.trace import TableTouch, TraceOptions, TraceResult, trace
from php_map.errors import UsageError
from php_map.graph import store

if TYPE_CHECKING:
    from php_map.graph.builder import KnowledgeGraph

SCHEMA_VERSION = 1

CASADO = "casado"
SEM_SERVIDOR = "chamado_sem_servidor"
SEM_CLIENTE = "servido_sem_cliente"
STATUSES = (CASADO, SEM_SERVIDOR, SEM_CLIENTE)

# O que o endpoint alcança no sistema que o serve (especificação §1). Sem `INCLUDES`/`REQUIRES`/
# `REFERENCES`: o `trace` segue includes porque parte de uma tela; o endpoint parte do que o
# XML ou o `setClass` declara.
CROSS_EDGES = frozenset(
    {"QUERIES", "INSERTS", "UPDATES", "DELETES", "CALLS", "INSTANTIATES", "CALLS_RPC"}
)
DEFAULT_DEPTH = 3
SERVED_BY = "SERVED_BY"  # passo sintético: o método/função PHP que implementa o endpoint
_SERVER_LABELS = ("Method:", "Function:")  # `File` (XML, despachante) traria o arquivo inteiro
_EXTERNAL = "externo"
_DYNAMIC = "?"
_CHUNK = 500  # `IN (...)`: abaixo do limite de variáveis de qualquer SQLite


@dataclass(frozen=True, slots=True)
class Evidence:
    """Quem implementa o endpoint: o nó e o arquivo/linha da `SERVES`."""

    project: str
    node: str
    path: str | None
    line: int | None


@dataclass(frozen=True, slots=True)
class CrossCall:
    """Uma `CALLS_REMOTE` para o endpoint (aresta agregada: `occurrences` chamadas no local)."""

    project: str
    caller: str
    path: str | None
    line: int | None
    module: str | None  # `Module:*` do arquivo que chama (BELONGS_TO), None se não houver
    args: dict[str, Any]  # `evidence.args` (`argc` e `literals`)
    confidence: float
    occurrences: int = 1


@dataclass(frozen=True, slots=True)
class Reach:
    """O que o endpoint alcança no grafo de quem serve, até `depth`."""

    tables: tuple[str, ...]  # nós `Table:*`
    modules: tuple[str, ...]  # nós `Module:*` dos arquivos no caminho
    nodes: int  # total de nós alcançados (sem o próprio endpoint)


EMPTY_REACH = Reach((), (), 0)


@dataclass(frozen=True, slots=True)
class CrossEndpoint:
    endpoint_id: str
    system: str  # quem serve (`ecidade`, `zend1app`)
    protocol: str
    name: str
    kind: str | None
    served_by: str | None  # projeto (`meta.project`) que serve, None se nenhum
    servers: tuple[Evidence, ...]
    callers: tuple[CrossCall, ...]
    params: tuple[dict[str, Any], ...]  # o contrato declarado, de quem serve
    reaches: Reach
    status: str  # CASADO | SEM_SERVIDOR | SEM_CLIENTE

    @property
    def calls(self) -> int:
        return sum(call.occurrences for call in self.callers)


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    slug: str
    name: str
    profile: str
    db: str
    served: int  # endpoints (de verdade) que o grafo serve
    called: int  # endpoints (de verdade) que o grafo chama


@dataclass(frozen=True, slots=True)
class ModuleCount:
    module: str | None  # `Module:*` de quem chama; None = arquivo sem módulo
    calls: int
    endpoints: int


@dataclass(frozen=True, slots=True)
class Dependency:
    """O que o projeto `caller` usa do projeto `server`, só pelos endpoints casados."""

    caller: str
    server: str
    endpoints: int
    calls: int
    modules: tuple[ModuleCount, ...]  # módulos de quem chama, por nº de chamadas
    tables: tuple[str, ...]  # tabelas de quem serve alcançadas por esses endpoints
    reached_modules: tuple[str, ...]  # módulos de quem serve alcançados


@dataclass(frozen=True, slots=True)
class ProjectCount:
    project: str
    calls: int


@dataclass(frozen=True, slots=True)
class CrossReport:
    """Tudo o que o `cross` calcula. O JSON é este objeto inteiro (a interface consome)."""

    schema_version: int
    depth: int
    projects: tuple[ProjectInfo, ...]
    counts: dict[str, int]
    endpoints: tuple[CrossEndpoint, ...]
    dependencies: tuple[Dependency, ...]
    dynamic_calls: tuple[ProjectCount, ...]  # chamadas a `Endpoint:*:?` (nome não literal)
    external_calls: tuple[ProjectCount, ...]  # chamadas a `Endpoint:externo:*` (fora dos grafos)
    warnings: tuple[str, ...] = field(default=())

    def filtered(
        self, only: str | None = None, system: str | None = None, protocol: str | None = None
    ) -> "CrossReport":
        """Mesmo relatório só com os endpoints do filtro; o resumo é recalculado."""
        kept = tuple(
            e
            for e in self.endpoints
            if (only is None or e.status == only)
            and (system is None or e.system == system)
            and (protocol is None or e.protocol == protocol)
        )
        projects = tuple(
            replace(
                info,
                served=sum(1 for e in kept if e.served_by == info.slug),
                called=sum(1 for e in kept if any(c.project == info.slug for c in e.callers)),
            )
            for info in self.projects
        )
        return replace(
            self,
            projects=projects,
            endpoints=kept,
            counts=_counts(kept),
            dependencies=_dependencies(kept),
        )


# --------------------------------------------------------------------------- vizinhança lazy


@dataclass(frozen=True, slots=True)
class NodeInfo:
    label: str
    is_infra: bool
    path: str | None
    resolved: bool


_OutEdge = tuple[str, str, float, str]  # destino, tipo, confiança, evidência (JSON)


class Neighborhood:
    """Lê do banco, sob demanda e com cache, só o que a busca em largura precisa.

    `edge_types` limita as arestas de saída buscadas (`CROSS_EDGES` no `cross`, `TRAVERSED` no
    `trace --cross`). As consultas usam os índices por `src` e `dst`, sem varrer `edges`.
    """

    def __init__(self, conn: sqlite3.Connection, edge_types: Iterable[str]) -> None:
        self._conn = conn
        self._types = tuple(sorted(edge_types))
        self._out: dict[str, list[_OutEdge]] = {}
        self._info: dict[str, NodeInfo] = {}
        self._module: dict[str, str | None] = {}

    @property
    def conn(self) -> sqlite3.Connection:
        """A conexão (somente leitura) que a vizinhança consulta."""
        return self._conn

    def prefetch(self, nodes: Iterable[str]) -> None:
        missing = [n for n in dict.fromkeys(nodes) if n not in self._out]
        marks = ",".join("?" * len(self._types))
        for chunk in _chunks(missing):
            for node in chunk:
                self._out[node] = []
            rows = self._conn.execute(
                "SELECT src, dst, type, confidence, evidence FROM edges"
                f" WHERE src IN ({','.join('?' * len(chunk))}) AND type IN ({marks})",
                (*chunk, *self._types),
            )
            for src, dst, type_, confidence, evidence in rows:
                self._out[src].append((dst, type_, confidence, evidence))

    def out(self, node: str) -> list[_OutEdge]:
        self.prefetch([node])
        return self._out[node]

    def prefetch_info(self, nodes: Iterable[str]) -> None:
        missing = [n for n in dict.fromkeys(nodes) if n not in self._info]
        for chunk in _chunks(missing):
            rows = self._conn.execute(
                "SELECT id, label, json_extract(attrs, '$.is_infra'),"
                " json_extract(attrs, '$.path'), json_extract(attrs, '$.resolved') FROM nodes"
                f" WHERE id IN ({','.join('?' * len(chunk))})",
                chunk,
            )
            for node, label, is_infra, path, resolved in rows:
                self._info[node] = NodeInfo(label, bool(is_infra), path, resolved != 0)
            for node in chunk:  # aresta para nó que o banco não tem: o rótulo vem do ID
                self._info.setdefault(
                    node, NodeInfo(node.partition(":")[0], False, None, resolved=False)
                )

    def info(self, node: str) -> NodeInfo:
        self.prefetch_info([node])
        return self._info[node]

    def best_modules(self, file_ids: Iterable[str]) -> dict[str, str | None]:
        """`Module:*` de cada `File` (BELONGS_TO de maior confiança; empate, o menor ID)."""
        wanted = list(dict.fromkeys(file_ids))
        missing = [f for f in wanted if f not in self._module]
        for chunk in _chunks(missing):
            best: dict[str, tuple[float, str]] = {}
            rows = self._conn.execute(
                "SELECT src, dst, confidence FROM edges WHERE type = 'BELONGS_TO'"
                f" AND src IN ({','.join('?' * len(chunk))})",
                chunk,
            )
            for src, dst, confidence in rows:
                if dst.startswith("Module:") and (
                    src not in best or (-confidence, dst) < (-best[src][0], best[src][1])
                ):
                    best[src] = (confidence, dst)
            for file_id in chunk:
                self._module[file_id] = best[file_id][1] if file_id in best else None
        return {f: self._module[f] for f in wanted}

    def servers(self, endpoint: str) -> list[tuple[str, str]]:
        """`(nó, evidência JSON)` de cada `SERVES` que chega no endpoint."""
        rows = self._conn.execute(
            "SELECT src, evidence FROM edges WHERE dst = ? AND type = 'SERVES' ORDER BY src",
            (endpoint,),
        )
        return [(src, evidence) for src, evidence in rows]


def _chunks(items: Sequence[str]) -> Iterator[list[str]]:
    for start in range(0, len(items), _CHUNK):
        yield list(items[start : start + _CHUNK])


def lazy_walk(
    nb: Neighborhood,
    root: str,
    *,
    depth: int,
    min_confidence: float = 0.0,
    include_infra: bool = False,
    expand_pgfunctions: bool = False,
    with_servers: Sequence[tuple[str, str]] = (),
) -> Walk:
    """O mesmo `analysis.walk` (BFS, nós uma vez, vizinhos em ordem de ID), sem o grafo inteiro.

    Folhas (`Table`, `RpcCall`, `PgFunction`) não são expandidas. `with_servers` (`(nó, evidência)`
    de `SERVES`) entram a 1 passo da raiz como `SERVED_BY`: o método PHP que implementa o
    endpoint é de onde o código continua.
    """
    steps = {root: Step(root, None, None, None, 0)}
    queue: deque[str] = deque([root])
    if depth >= 1:
        for node, evidence in with_servers:
            usable = node.startswith(_SERVER_LABELS) and node not in steps
            if usable and (include_infra or not nb.info(node).is_infra):
                steps[node] = Step(node, root, SERVED_BY, 1.0, 1, json.loads(evidence))
                queue.append(node)
    while queue:
        batch = list(queue)
        queue.clear()
        nb.prefetch_info(batch)
        expandable = [
            n
            for n in batch
            if steps[n].depth < depth and (n == root or _expands(nb, n, expand_pgfunctions))
        ]
        nb.prefetch(expandable)
        nb.prefetch_info(dst for n in expandable for dst, *_ in nb.out(n))
        for current in expandable:
            step = steps[current]
            ordered = sorted(nb.out(current), key=lambda e: (e[0], -e[2], e[1]))
            for neighbor, type_, confidence, evidence in ordered:
                if confidence < min_confidence or neighbor in steps:
                    continue
                if not include_infra and nb.info(neighbor).is_infra:
                    continue
                steps[neighbor] = Step(
                    neighbor, current, type_, confidence, step.depth + 1, json.loads(evidence)
                )
                queue.append(neighbor)
    return Walk(root, steps)


def _expands(nb: Neighborhood, node: str, expand_pgfunctions: bool) -> bool:
    label = nb.info(node).label
    if label == "PgFunction":
        return expand_pgfunctions
    return label not in LEAF_LABELS


# --------------------------------------------------------------------------- carga


@dataclass(slots=True)
class _Edge:
    src: str
    dst: str
    confidence: float
    evidence: dict[str, Any]
    attrs: dict[str, Any]


@dataclass(slots=True)
class _Project:
    slug: str
    name: str
    profile: str
    db: Path
    nb: Neighborhood
    endpoints: dict[str, dict[str, Any]]
    serves: dict[str, list[_Edge]]
    calls: dict[str, list[_Edge]]


def project_label(db: Path, meta: Mapping[str, str]) -> tuple[str, str | None]:
    """`(slug, aviso)`: o `meta.project`, ou o nome do arquivo para bancos anteriores à 34."""
    slug = meta.get("project")
    if slug:
        return slug, None
    return db.stem, f"{db}: banco sem meta.project (mapa antigo); usando '{db.stem}'"


def _is_real(endpoint: str, attrs: Mapping[str, Any]) -> bool:
    """Endpoint que dá para casar: nem `?` (nome não literal) nem `externo` (fora dos grafos)."""
    system = attrs.get("system") or endpoint.split(":")[1]
    return system != _EXTERNAL and endpoint.rpartition(":")[2] != _DYNAMIC


def _load_project(stack: ExitStack, db: Path, slug: str, meta: Mapping[str, str]) -> _Project:
    conn = stack.enter_context(closing(store.connect_readonly(db)))
    endpoints: dict[str, dict[str, Any]] = {}
    for node, attrs in conn.execute("SELECT id, attrs FROM nodes WHERE label = 'Endpoint'"):
        endpoints[node] = json.loads(attrs)
    serves: dict[str, list[_Edge]] = {}
    calls: dict[str, list[_Edge]] = {}
    # Arestas que chegam num Endpoint, pelo índice de `dst` (a faixa é o prefixo "Endpoint:").
    rows = conn.execute(
        "SELECT src, dst, type, confidence, evidence, attrs FROM edges"
        " WHERE dst >= 'Endpoint:' AND dst < 'Endpoint;' ORDER BY id"
    )
    for src, dst, type_, confidence, evidence, attrs in rows:
        target = serves if type_ == "SERVES" else calls if type_ == "CALLS_REMOTE" else None
        if target is not None:
            edge = _Edge(src, dst, confidence, json.loads(evidence), json.loads(attrs))
            target.setdefault(dst, []).append(edge)
    nb = Neighborhood(conn, CROSS_EDGES)
    return _Project(
        slug,
        meta.get("project_name") or slug,
        meta.get("profile") or "desconhecido",
        db,
        nb,
        endpoints,
        serves,
        calls,
    )


# --------------------------------------------------------------------------- cruzamento


def cross_projects(db_paths: Sequence[Path], depth: int = DEFAULT_DEPTH) -> CrossReport:
    """Cruza 2 ou mais bancos de grafo pelo ID do `Endpoint`.

    Levanta `UsageError` para menos de dois bancos ou dois do mesmo projeto (`meta.project`
    igual, ex.: duas épocas do e-cidade): o join casaria o projeto com ele mesmo.
    """
    if len(db_paths) < 2:
        raise UsageError("informe ao menos dois bancos (--graph repetido) para cruzar")
    if depth < 0:
        raise UsageError("--depth não pode ser negativo")
    metas = [store.load_meta(db) for db in db_paths]
    warnings: list[str] = []
    slugs: list[str] = []
    for db, meta in zip(db_paths, metas, strict=True):
        slug, warning = project_label(db, meta)
        if warning:
            warnings.append(warning)
        slugs.append(slug)
    _check_distinct(db_paths, metas, slugs)
    with ExitStack() as stack:
        projects = [
            _load_project(stack, db, slug, meta)
            for db, slug, meta in zip(db_paths, slugs, metas, strict=True)
        ]
        return _cross(projects, depth, warnings)


def _check_distinct(
    db_paths: Sequence[Path], metas: Sequence[Mapping[str, str]], slugs: list[str]
) -> None:
    """Dois bancos do mesmo projeto são erro; só o nome do arquivo repetido é desambiguado."""
    declared = Counter(s for s, m in zip(slugs, metas, strict=True) if m.get("project"))
    for slug, count in declared.items():
        if count > 1:
            raise UsageError(
                f"dois bancos do mesmo projeto ('{slug}'): cruzar exige projetos diferentes"
                " (duas épocas do mesmo sistema não têm costura entre si)"
            )
    if len(set(slugs)) < len(slugs):  # nomes de arquivo iguais (sem meta.project): pelas pastas
        for index, (db, meta) in enumerate(zip(db_paths, metas, strict=True)):
            if not meta.get("project"):
                slugs[index] = f"{db.parent.name}/{db.stem}"
        if len(set(slugs)) < len(slugs):
            raise UsageError("não consegui distinguir os bancos: use nomes de arquivo diferentes")


def _cross(projects: list[_Project], depth: int, warnings: list[str]) -> CrossReport:
    # Nó `Endpoint` sem `SERVES` nem `CALLS_REMOTE` não é nem servido nem chamado: fica de fora.
    attrs_of = {n: a for p in projects for n, a in p.endpoints.items()}
    ids = sorted(
        {n for p in projects for n in (*p.serves, *p.calls) if _is_real(n, attrs_of.get(n, {}))}
    )
    call_modules = {p.slug: _caller_modules(p) for p in projects}
    endpoints: list[CrossEndpoint] = []
    for endpoint in ids:
        endpoints.append(_cross_endpoint(endpoint, projects, call_modules, depth, warnings))
    endpoints.sort(key=lambda e: (-len(e.callers), e.endpoint_id))
    infos = tuple(
        ProjectInfo(
            p.slug,
            p.name,
            p.profile,
            str(p.db),
            served=sum(1 for e in endpoints if e.served_by == p.slug),
            called=sum(1 for e in endpoints if any(c.project == p.slug for c in e.callers)),
        )
        for p in projects
    )
    ordered = tuple(endpoints)
    return CrossReport(
        SCHEMA_VERSION,
        depth,
        infos,
        _counts(ordered),
        ordered,
        _dependencies(ordered),
        _special_calls(projects, dynamic=True),
        _special_calls(projects, dynamic=False),
        tuple(dict.fromkeys(warnings)),
    )


def _caller_modules(project: _Project) -> dict[str, str | None]:
    """Arquivo que chama (`File:*`) → `Module:*`, para todas as `CALLS_REMOTE` do projeto."""
    files = {
        _caller_file(edge)
        for edges in project.calls.values()
        for edge in edges
        if _caller_file(edge) is not None
    }
    return project.nb.best_modules(f for f in files if f is not None)


def _evidence(project: str, edge: _Edge) -> Evidence:
    """Quem implementa e onde: um `File` que serve (o XML, o despachante) aponta para o próprio
    arquivo, e só leva a linha da evidência se for o mesmo arquivo; um método, para o local da
    declaração do endpoint (`setClass`)."""
    path, line = edge.evidence.get("path"), edge.evidence.get("line")
    if edge.src.startswith("File:"):
        own = edge.src.partition(":")[2]
        return Evidence(project, edge.src, own, line if own == path else None)
    return Evidence(project, edge.src, path, line)


def _caller_file(edge: _Edge) -> str | None:
    if edge.src.startswith("File:"):
        return edge.src
    path = edge.evidence.get("path")
    return f"File:{path}" if path else None


def _cross_endpoint(
    endpoint: str,
    projects: list[_Project],
    call_modules: Mapping[str, Mapping[str, str | None]],
    depth: int,
    warnings: list[str],
) -> CrossEndpoint:
    servers_in = [p for p in projects if endpoint in p.serves]
    if len(servers_in) > 1:
        names = ", ".join(p.slug for p in servers_in)
        warnings.append(f"{endpoint} é servido por mais de um projeto ({names}); vale o primeiro")
    server = servers_in[0] if servers_in else None
    declared = server.endpoints.get(endpoint, {}) if server else {}
    attrs = declared or next(
        (p.endpoints[endpoint] for p in projects if endpoint in p.endpoints), {}
    )
    _, _, rest = endpoint.partition(":")
    system, _, rest = rest.partition(":")
    protocol, _, name = rest.partition(":")
    evidences = tuple(_evidence(p.slug, e) for p in servers_in[:1] for e in p.serves[endpoint])
    callers = tuple(
        sorted(
            (
                CrossCall(
                    p.slug,
                    e.src,
                    e.evidence.get("path"),
                    e.evidence.get("line"),
                    call_modules[p.slug].get(_caller_file(e) or ""),
                    dict(e.evidence.get("args") or {}),
                    e.confidence,
                    int(e.attrs.get("occurrences") or 1),
                )
                for p in projects
                for e in p.calls.get(endpoint, [])
            ),
            key=lambda c: (c.project, c.path or "", c.line or 0, c.caller),
        )
    )
    status = (CASADO if callers else SEM_CLIENTE) if server else SEM_SERVIDOR
    reaches = _reach(server, endpoint, depth) if server else EMPTY_REACH
    return CrossEndpoint(
        endpoint,
        str(attrs.get("system") or system),
        str(attrs.get("protocol") or protocol),
        str(attrs.get("name") or name),
        attrs.get("kind") if server else None,
        server.slug if server else None,
        evidences,
        callers,
        tuple(declared.get("params") or ()),
        reaches,
        status,
    )


def endpoint_walk(
    nb: Neighborhood,
    endpoint: str,
    *,
    depth: int,
    servers: Sequence[tuple[str, str]] | None = None,
) -> Walk:
    """O alcance de um endpoint no projeto que o serve: `lazy_walk` a partir do `Endpoint`, com
    os métodos/funções que o servem (`SERVES`) a 1 passo. `servers` (`(nó, evidência JSON)`)
    evita reler as arestas quando quem chama já as tem (o `cross`). Usado também pelo relatório
    de fronteira."""
    with_servers = nb.servers(endpoint) if servers is None else servers
    return lazy_walk(nb, endpoint, depth=depth, with_servers=with_servers)


def _reach(project: _Project, endpoint: str, depth: int) -> Reach:
    nb = project.nb
    servers = [(e.src, json.dumps(e.evidence)) for e in project.serves[endpoint]]
    walk = endpoint_walk(nb, endpoint, depth=depth, servers=servers)
    nb.prefetch_info(walk.steps)
    infos = {node: nb.info(node) for node in walk.steps if node != endpoint}
    tables = sorted(n for n, info in infos.items() if info.label == "Table")
    paths = {
        f"File:{info.path}"
        for info in infos.values()
        if info.path and info.label in ("Method", "Function", "Class", "File")
    }
    modules = {m for m in nb.best_modules(sorted(paths)).values() if m}
    return Reach(tuple(tables), tuple(sorted(modules)), len(infos))


def _counts(endpoints: Iterable[CrossEndpoint]) -> dict[str, int]:
    seen = Counter(e.status for e in endpoints)
    return {status: seen.get(status, 0) for status in STATUSES}


def _dependencies(endpoints: Iterable[CrossEndpoint]) -> tuple[Dependency, ...]:
    """Por par (quem chama, quem serve): módulos, tabelas e endpoints dos casados."""
    pairs: dict[tuple[str, str], list[tuple[CrossEndpoint, list[CrossCall]]]] = {}
    for endpoint in endpoints:
        if endpoint.status != CASADO or endpoint.served_by is None:
            continue
        by_caller: dict[str, list[CrossCall]] = {}
        for call in endpoint.callers:
            if call.project != endpoint.served_by:
                by_caller.setdefault(call.project, []).append(call)
        for caller, calls in by_caller.items():
            pairs.setdefault((caller, endpoint.served_by), []).append((endpoint, calls))
    result = []
    for (caller, server), items in sorted(pairs.items()):
        per_module: dict[str | None, list[int]] = {}
        for _endpoint, calls in items:
            for module in {c.module for c in calls}:
                per_module.setdefault(module, [0, 0])
                per_module[module][0] += sum(c.occurrences for c in calls if c.module == module)
                per_module[module][1] += 1
        modules = tuple(
            ModuleCount(module, calls, count)
            for module, (calls, count) in sorted(
                per_module.items(), key=lambda kv: (-kv[1][0], kv[0] is None, kv[0] or "")
            )
        )
        result.append(
            Dependency(
                caller,
                server,
                len(items),
                sum(c.occurrences for _, calls in items for c in calls),
                modules,
                tuple(sorted({t for e, _ in items for t in e.reaches.tables})),
                tuple(sorted({m for e, _ in items for m in e.reaches.modules})),
            )
        )
    return tuple(result)


def _special_calls(projects: list[_Project], *, dynamic: bool) -> tuple[ProjectCount, ...]:
    """Chamadas que não casam com ninguém: nome não literal (`?`) ou para fora (`externo`)."""
    counts = []
    for project in projects:
        total = 0
        for endpoint, edges in project.calls.items():
            system = endpoint.split(":")[1]
            is_dynamic = endpoint.rpartition(":")[2] == _DYNAMIC and system != _EXTERNAL
            if (is_dynamic and dynamic) or (system == _EXTERNAL and not dynamic):
                total += sum(int(e.attrs.get("occurrences") or 1) for e in edges)
        if total:
            counts.append(ProjectCount(project.slug, total))
    return tuple(counts)


# --------------------------------------------------------------------------- trace atravessando


@dataclass(frozen=True, slots=True)
class Crossing:
    """O `trace` entrou em um `Endpoint` placeholder e continuou no projeto que o serve."""

    endpoint: str
    from_project: str
    to_project: str
    depth: int  # profundidade do endpoint no trace de origem
    path: list[str]  # do arquivo de entrada até o endpoint, no projeto de origem
    walk: Walk  # no projeto de destino, a partir do endpoint
    labels: dict[str, str]
    tables: list[TableTouch]


@dataclass(frozen=True, slots=True)
class CrossTraceResult:
    primary: TraceResult
    project: str  # projeto do trace de origem
    crossings: list[Crossing]
    unserved: list[str]  # endpoints chamados que nenhum dos outros bancos serve


def trace_across(
    graph: "KnowledgeGraph", project: str, options: TraceOptions, others: Sequence[Path]
) -> CrossTraceResult:
    """`trace` que atravessa a costura: ao chegar num `Endpoint` placeholder, continua nos `others`.

    O trace de origem passa a seguir `CALLS_REMOTE` (`follow_remote`); a profundidade que sobra
    ao chegar no endpoint é a que o trace tem no outro projeto.
    """
    options = replace(options, follow_remote=True)
    metas = [store.load_meta(db) for db in others]
    slugs = [project_label(db, meta)[0] for db, meta in zip(others, metas, strict=True)]
    if project in slugs:
        raise UsageError(f"o banco do --cross é do mesmo projeto do trace ('{project}')")
    if len(set(slugs)) < len(slugs):
        raise UsageError("dois --cross do mesmo projeto: informe projetos diferentes")
    primary = trace(graph, options)
    candidates = [
        node
        for node in primary.walk.steps
        if primary.labels[node] == "Endpoint"
        and graph.nodes[node].get("resolved") is False
        and _is_real(node, graph.nodes[node])
    ]
    crossings: list[Crossing] = []
    unserved: list[str] = []
    with ExitStack() as stack:
        neighborhoods = [
            (
                slug,
                Neighborhood(stack.enter_context(closing(store.connect_readonly(db))), TRAVERSED),
            )
            for db, slug in zip(others, slugs, strict=True)
        ]
        for endpoint in candidates:
            for slug, nb in neighborhoods:
                servers = nb.servers(endpoint)
                if servers:
                    step = primary.walk.steps[endpoint]
                    crossings.append(
                        _crossing(nb, endpoint, project, slug, step, primary, servers, options)
                    )
                    break
            else:
                unserved.append(endpoint)
    return CrossTraceResult(primary, project, crossings, sorted(unserved))


def _crossing(
    nb: Neighborhood,
    endpoint: str,
    from_project: str,
    to_project: str,
    step: Step,
    primary: TraceResult,
    servers: list[tuple[str, str]],
    options: TraceOptions,
) -> Crossing:
    left = max(0, options.depth - step.depth)
    walk = lazy_walk(
        nb,
        endpoint,
        depth=left,
        min_confidence=options.min_confidence,
        include_infra=options.include_infra,
        expand_pgfunctions=options.expand_pgfunctions,
        with_servers=servers,
    )
    nb.prefetch_info(walk.steps)
    labels = {node: nb.info(node).label for node in walk.steps}
    tables: list[TableTouch] = []
    for origin in walk.steps:
        if origin != endpoint and not _expands(nb, origin, options.expand_pgfunctions):
            continue
        for target, type_, confidence, evidence in nb.out(origin):
            operation = TABLE_OPS.get(type_)
            if operation is None or target not in walk.steps or confidence < options.min_confidence:
                continue
            tables.append(TableTouch(operation, target, origin, confidence, json.loads(evidence)))
    tables.sort(key=lambda t: (t.operation, t.table, t.origin))
    return Crossing(
        endpoint,
        from_project,
        to_project,
        step.depth,
        primary.walk.path_to(endpoint),
        walk,
        labels,
        tables,
    )
