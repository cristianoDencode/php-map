"""Relatório de fronteira: plano de corte por endpoint e acoplamento por módulo.

Compõe o que já existe, sem recalcular o acoplamento e sem carregar o grafo inteiro:
- **plano de corte**: para cada endpoint, o alcance no projeto que o serve (`endpoint_walk`, o
  mesmo do `cross`) e a **natureza**: `comando` se algum nó alcançado escreve numa tabela,
  `leitura` se só lê, `indeterminado` (com motivo) se não dá para dizer. A escrita é decidida
  pelas arestas **saindo** dos nós alcançados, e não pelo tipo do passo do `Walk` (o `Walk`
  guarda só a primeira aresta que chega a cada nó: um `QUERIES` anterior esconderia o
  `INSERTS`);
- **escritores** de cada tabela: diretos (`INSERTS`/`UPDATES`/`DELETES`) e via DAO (quem chama
  ou instancia os `Method` que escrevem direto);
- **fronteira por módulo** e **candidatos à extração**, a partir das tabelas `analytics_*`,
  com o `custo_corte` explícito.
"""

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import ExitStack, closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from php_map.analysis.aggregate import NO_MODULE
from php_map.analysis.cross import (
    CASADO,
    CrossEndpoint,
    CrossReport,
    _load_project,
    _Project,
    cross_projects,
    endpoint_walk,
    project_label,
)
from php_map.errors import UsageError
from php_map.graph import store

DEFAULT_DEPTH = 6
MIN_FILES = 10  # módulos menores não entram no ranking de candidatos
MATRIX_TOP = 15
EDGE_TOP = 10
SELECTIONS = ("all", "called", "served", "matched")
COMANDO, LEITURA, INDETERMINADO = "comando", "leitura", "indeterminado"
NATUREZAS = (COMANDO, LEITURA, INDETERMINADO)
SEM_SERVIDOR, SEM_ALCANCE = "sem_servidor", "sem_alcance"
ALVO_DINAMICO, EXTERNO = "alvo_dinamico", "externo"
WRITE_TYPES = frozenset({"INSERTS", "UPDATES", "DELETES"})
NO_MODULES_WARNING = (
    "este grafo não tem módulos: escaneie com o catálogo (--catalog-db) para ver a fronteira por "
    "módulo e a matriz de acoplamento"
)
NO_ANALYTICS_WARNING = (
    "banco sem agregados (escaneado com --no-analytics): sem fronteira por módulo nem matriz"
)
COST_FORMULA = (
    "custo_corte = ca + ce + 2 × escreve_em_outros + 2 × tabelas_proprias_escritas_por_outros"
)

# Nome de tabela de verdade: `schema.nome`. Pedaços de SQL dos XML do e-cidade (`Table:public.(`,
# `Table:public.(select`, `Table:public.fc_statusdebitos($numpre,`) ficam de fora.
_REAL_TABLE = re.compile(r"^Table:[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")


def is_real_table(node_id: str) -> bool:
    return _REAL_TABLE.match(node_id) is not None


# --------------------------------------------------------------------------- registros


@dataclass(frozen=True, slots=True)
class TableWriters:
    table: str
    direct: int
    via_dao: int


@dataclass(frozen=True, slots=True)
class EndpointPlan:
    endpoint_id: str
    system: str
    protocol: str
    kind: str | None
    natureza: str
    motivo: str | None
    served_by: str | None  # projeto que serve (onde o alcance foi calculado)
    callers: int
    calls: int
    caller_projects: tuple[str, ...]
    caller_modules: tuple[str, ...]
    params: tuple[str, ...]
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    ignored_tables: int  # pedaços de SQL alcançados (não contam como tabela)
    max_writers_direct: int
    max_writers_via_dao: int
    served_by_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModuleEdge:
    module: str
    kind: str
    weight: float
    edges: int


@dataclass(frozen=True, slots=True)
class OwnedTable:
    table: str
    modules: tuple[
        str, ...
    ]  # os outros módulos que escrevem (ou os donos, em `writes_into_others`)


@dataclass(frozen=True, slots=True)
class Candidate:
    module: str
    name: str
    files: int
    ca: int
    ce: int
    writes_into_others: int
    owned_written_by_others: int
    cost: int


@dataclass(frozen=True, slots=True)
class ModuleCard:
    module: str
    name: str
    files: int
    screens: int
    tables_owned: int
    ca: int
    ce: int
    instability: float | None
    shared_tables: tuple[str, ...]
    owned_written_by_others: tuple[OwnedTable, ...]
    writes_into_others: tuple[OwnedTable, ...]
    edges_out: tuple[ModuleEdge, ...]
    edges_in: tuple[ModuleEdge, ...]
    endpoints_served: tuple[str, ...]
    endpoints_called: tuple[str, ...]
    hotspots: tuple[str, ...]
    cost: int


@dataclass(frozen=True, slots=True)
class MatrixCell:
    src: str
    dst: str
    kind: str
    weight: float
    edges: int


@dataclass(frozen=True, slots=True)
class BoundaryReport:
    project: str
    db: str
    depth: int
    selection: str
    has_modules: bool
    modules_total: int
    natureza_counts: dict[str, int]
    endpoints: tuple[EndpointPlan, ...]
    candidates: tuple[Candidate, ...]
    matrix: tuple[MatrixCell, ...]
    module: ModuleCard | None = None
    cost_formula: str = COST_FORMULA
    warnings: tuple[str, ...] = field(default=())


# --------------------------------------------------------------------------- escritores


def table_writers(conn: sqlite3.Connection, tables: Iterable[str]) -> dict[str, TableWriters]:
    """Escritores diretos e via DAO de cada tabela, em lote (uma consulta por pedaço)."""
    wanted = sorted(set(tables))
    direct: dict[str, set[str]] = {t: set() for t in wanted}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for src, dst in conn.execute(
            f"SELECT DISTINCT src, dst FROM edges WHERE dst IN ({marks})"
            " AND type IN ('INSERTS', 'UPDATES', 'DELETES')",
            chunk,
        ):
            direct[dst].add(src)
    methods = sorted({src for srcs in direct.values() for src in srcs if src.startswith("Method:")})
    callers: dict[str, set[str]] = {m: set() for m in methods}
    for start in range(0, len(methods), 500):
        chunk = methods[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for src, dst in conn.execute(
            f"SELECT DISTINCT src, dst FROM edges WHERE dst IN ({marks})"
            " AND type IN ('CALLS', 'INSTANTIATES')",
            chunk,
        ):
            callers[dst].add(src)
    result: dict[str, TableWriters] = {}
    for table in wanted:
        via = {c for m in direct[table] if m in callers for c in callers[m]}
        result[table] = TableWriters(table, len(direct[table]), len(via))
    return result


# --------------------------------------------------------------------------- plano de corte


def _motivo_without_server(endpoint: str, attrs: dict[str, Any]) -> str:
    system = attrs.get("system") or endpoint.split(":")[1]
    if system == EXTERNO:
        return EXTERNO
    if endpoint.rpartition(":")[2] == "?":
        return ALVO_DINAMICO
    return SEM_SERVIDOR


_Nature = tuple[str, str | None, set[str], set[str], int]


def _nature(project: _Project, endpoint: str, depth: int) -> _Nature:
    nb = project.nb
    servers = [(e.src, json.dumps(e.evidence)) for e in project.serves.get(endpoint, [])]
    walk = endpoint_walk(nb, endpoint, depth=depth, servers=servers)
    nb.prefetch_info(walk.steps)
    tables = {n for n in walk.steps if nb.info(n).label == "Table"}
    expanded = [n for n, step in walk.steps.items() if step.depth < depth]
    nb.prefetch(expanded)
    candidates = {
        dst
        for node in expanded
        for dst, type_, _conf, _ev in nb.out(node)
        if type_ in WRITE_TYPES and dst.startswith("Table:")
    }
    nb.prefetch_info(candidates)
    # Tabela de infraestrutura (perfil: `db_acount*`, a auditoria que toda DAO grava) não faz do
    # endpoint um comando, como a BFS já não a atravessa na leitura.
    writes = {t for t in candidates if not nb.info(t).is_infra}
    real_writes = {t for t in writes if is_real_table(t)}
    real_reads = {t for t in tables if is_real_table(t)} - real_writes
    ignored = len({t for t in tables | writes if not is_real_table(t)})
    if real_writes:
        return COMANDO, None, real_reads, real_writes, ignored
    if real_reads:
        return LEITURA, None, real_reads, real_writes, ignored
    return INDETERMINADO, SEM_ALCANCE, real_reads, real_writes, ignored


def _caller_info(
    endpoint: str, cross: CrossReport | None, main: _Project
) -> tuple[int, int, tuple[str, ...], tuple[str, ...]]:
    found: CrossEndpoint | None = None
    if cross is not None:
        found = next((e for e in cross.endpoints if e.endpoint_id == endpoint), None)
    if found is not None:
        return (
            len(found.callers),
            found.calls,
            tuple(sorted({c.project for c in found.callers})),
            tuple(sorted({c.module for c in found.callers if c.module})),
        )
    edges = main.calls.get(endpoint, [])
    calls = sum(int(e.attrs.get("occurrences", 1) or 1) for e in edges)
    files = []
    for edge in edges:
        path = edge.evidence.get("path") or main.nb.info(edge.src).path
        if path:
            files.append(f"File:{path}")
    modules = {m for m in main.nb.best_modules(files).values() if m}
    projects = (main.slug,) if edges else ()
    return len({e.src for e in edges}), calls, projects, tuple(sorted(modules))


def _select(main: _Project, cross: CrossReport | None, selection: str) -> list[str]:
    served = {e for e in main.serves if not e.endswith(":?")}
    called = set(main.calls)
    if selection == "served":
        return sorted(served)
    if selection == "called":
        return sorted(called)
    if selection == "all":
        return sorted(served | called)
    if cross is None:
        raise UsageError("--endpoints matched precisa de --cross (outro projeto com grafo)")
    return sorted(
        e.endpoint_id
        for e in cross.endpoints
        if e.status == CASADO
        and (e.served_by == main.slug or any(c.project == main.slug for c in e.callers))
    )


def endpoint_plans(
    main: _Project,
    others: Sequence[_Project],
    *,
    selection: str,
    depth: int,
    cross: CrossReport | None,
) -> list[EndpointPlan]:
    if selection not in SELECTIONS:
        raise UsageError(f"--endpoints aceita: {', '.join(SELECTIONS)}")
    projects = [main, *others]
    chosen = _select(main, cross, selection)
    computed: list[
        tuple[str, _Project | None, tuple[str, str | None, set[str], set[str], int]]
    ] = []
    for endpoint in chosen:
        server = next((p for p in projects if p.serves.get(endpoint)), None)
        result: _Nature
        if server is None:
            attrs = next((p.endpoints[endpoint] for p in projects if endpoint in p.endpoints), {})
            result = (INDETERMINADO, _motivo_without_server(endpoint, attrs), set(), set(), 0)
        else:
            result = _nature(server, endpoint, depth)
        computed.append((endpoint, server, result))
    writers: dict[str, dict[str, TableWriters]] = {}
    for project in projects:
        tables = {
            t
            for _e, server, (_n, _m, reads, writes, _i) in computed
            if server is project
            for t in reads | writes
        }
        if tables:
            writers[project.slug] = table_writers(project.nb.conn, tables)
    plans: list[EndpointPlan] = []
    for endpoint, server, (natureza, motivo, reads, writes, ignored) in computed:
        attrs = next((p.endpoints[endpoint] for p in projects if endpoint in p.endpoints), {})
        if server is not None and endpoint in server.endpoints:
            attrs = server.endpoints[endpoint]
        _, system, protocol, _name = (endpoint.split(":", 3) + ["", "", ""])[:4]
        known = writers.get(server.slug, {}) if server else {}
        counts = [known[t] for t in reads | writes if t in known]
        callers, calls, caller_projects, caller_modules = _caller_info(endpoint, cross, main)
        files: tuple[str, ...] = ()
        if server is not None:
            files = tuple(
                sorted(
                    {
                        str(e.evidence.get("path") or e.src.partition(":")[2])
                        for e in server.serves.get(endpoint, [])
                    }
                )
            )
        plans.append(
            EndpointPlan(
                endpoint_id=endpoint,
                system=str(attrs.get("system") or system),
                protocol=str(attrs.get("protocol") or protocol),
                kind=attrs.get("kind"),
                natureza=natureza,
                motivo=motivo,
                served_by=server.slug if server else None,
                callers=callers,
                calls=calls,
                caller_projects=caller_projects,
                caller_modules=caller_modules,
                params=tuple(str(p.get("name")) for p in attrs.get("params") or []),
                reads=tuple(sorted(reads)),
                writes=tuple(sorted(writes)),
                ignored_tables=ignored,
                max_writers_direct=max((c.direct for c in counts), default=0),
                max_writers_via_dao=max((c.via_dao for c in counts), default=0),
                served_by_files=files,
            )
        )
    plans.sort(key=lambda p: (-p.calls, p.endpoint_id))
    return plans


# --------------------------------------------------------------------------- módulos


def _module_id(conn: sqlite3.Connection, module: str) -> str:
    wanted = module if module.startswith("Module:") or module == NO_MODULE else f"Module:{module}"
    rows = [r[0] for r in conn.execute("SELECT module FROM analytics_modules")]
    if wanted in rows:
        return wanted
    key = module.removeprefix("Module:").lower()
    hints = sorted(r for r in rows if r.lower().removeprefix("module:").startswith(key[:3]))
    tip = f" (parecidos: {', '.join(hints[:8])})" if hints else ""
    raise UsageError(f"módulo inexistente: {module}{tip}")


def _has_modules(conn: sqlite3.Connection) -> tuple[bool, int, str | None]:
    """`(tem módulos, quantos, aviso)`. Sem as tabelas `analytics_*` (`--no-analytics`), a parte
    de módulos fica de fora com aviso; o plano de corte não depende delas."""
    try:
        rows = [r[0] for r in conn.execute("SELECT module FROM analytics_modules")]
    except sqlite3.OperationalError:
        return False, 0, NO_ANALYTICS_WARNING
    real = [r for r in rows if r != NO_MODULE]
    return bool(real), len(real), None if real else NO_MODULES_WARNING


def _owned_written_by_others(conn: sqlite3.Connection, module: str) -> list[OwnedTable]:
    out: list[OwnedTable] = []
    for table, writers in conn.execute(
        "SELECT table_id, writer_modules FROM analytics_tables t"
        " WHERE EXISTS (SELECT 1 FROM json_each(t.owner_modules) o WHERE o.value = ?)"
        " ORDER BY table_id",
        (module,),
    ):
        others = sorted(m for m in json.loads(writers) if m not in (module, NO_MODULE))
        if others:
            out.append(OwnedTable(table, tuple(others)))
    return out


def _writes_into_others(conn: sqlite3.Connection, module: str) -> list[OwnedTable]:
    out: list[OwnedTable] = []
    for table, owners in conn.execute(
        "SELECT table_id, owner_modules FROM analytics_tables t"
        " WHERE EXISTS (SELECT 1 FROM json_each(t.writer_modules) w WHERE w.key = ?)"
        " AND json_array_length(t.owner_modules) > 0"
        " AND NOT EXISTS (SELECT 1 FROM json_each(t.owner_modules) o WHERE o.value = ?)"
        " ORDER BY table_id",
        (module, module),
    ):
        out.append(OwnedTable(table, tuple(sorted(json.loads(owners)))))
    return out


def _cost(ca: int, ce: int, writes_into: int, owned_written: int) -> int:
    return ca + ce + 2 * writes_into + 2 * owned_written


def extraction_candidates(conn: sqlite3.Connection, top: int | None = None) -> list[Candidate]:
    """Módulos com `files ≥ MIN_FILES`, do menor para o maior `custo_corte` (empate: mais
    arquivos primeiro). É apoio à decisão: os componentes vão junto."""
    rows = conn.execute(
        "SELECT module, name, files, ca, ce FROM analytics_modules"
        " WHERE module != ? AND files >= ?",
        (NO_MODULE, MIN_FILES),
    ).fetchall()
    found: list[Candidate] = []
    for module, name, files, ca, ce in rows:
        writes_into = len(_writes_into_others(conn, module))
        owned = len(_owned_written_by_others(conn, module))
        cost = _cost(ca, ce, writes_into, owned)
        found.append(Candidate(module, name or module, files, ca, ce, writes_into, owned, cost))
    found.sort(key=lambda c: (c.cost, -c.files, c.module))
    return found if top is None else found[:top]


def coupling_matrix(conn: sqlite3.Connection) -> list[MatrixCell]:
    return [
        MatrixCell(src, dst, kind, float(weight), int(edges))
        for src, dst, kind, weight, edges in conn.execute(
            "SELECT src, dst, kind, weight, edges FROM analytics_module_edges"
            " ORDER BY weight DESC, src, dst, kind"
        )
    ]


def _edges(conn: sqlite3.Connection, module: str, *, incoming: bool) -> tuple[ModuleEdge, ...]:
    mine, other = ("dst", "src") if incoming else ("src", "dst")
    rows = conn.execute(
        f"SELECT {other}, kind, sum(weight), sum(edges) FROM analytics_module_edges"
        f" WHERE {mine} = ? AND {other} != ? GROUP BY {other}, kind"
        " ORDER BY sum(weight) DESC, 1, 2 LIMIT ?",
        (module, module, EDGE_TOP),
    )
    return tuple(ModuleEdge(m, k, float(w), int(e)) for m, k, w, e in rows)


def _module_endpoints(project: _Project, module: str) -> tuple[list[str], list[str]]:
    nb = project.nb

    def files_of(nodes: Iterable[str], evidence_paths: Iterable[str | None]) -> list[str]:
        out = []
        for node, path in zip(nodes, evidence_paths, strict=True):
            real = (
                path
                or nb.info(node).path
                or (node.partition(":")[2] if node.startswith("File:") else None)
            )
            out.append(f"File:{real}" if real else "")
        return out

    def belongs(edges_by_endpoint: dict[str, list[Any]], use_evidence: bool) -> list[str]:
        result = []
        for endpoint, edges in sorted(edges_by_endpoint.items()):
            nodes = [e.src for e in edges]
            paths = [e.evidence.get("path") if use_evidence else None for e in edges]
            files = [f for f in files_of(nodes, paths) if f]
            if module in set(nb.best_modules(files).values()):
                result.append(endpoint)
        return result

    return belongs(project.serves, False), belongs(project.calls, True)


def module_card(conn: sqlite3.Connection, project: _Project, module: str) -> ModuleCard:
    module = _module_id(conn, module)
    row = conn.execute(
        "SELECT module, name, files, screens, ca, ce, instability FROM analytics_modules"
        " WHERE module = ?",
        (module,),
    ).fetchone()
    owned = conn.execute(
        "SELECT table_id, shared FROM analytics_tables t"
        " WHERE EXISTS (SELECT 1 FROM json_each(t.owner_modules) o WHERE o.value = ?)",
        (module,),
    ).fetchall()
    owned_written = _owned_written_by_others(conn, module)
    writes_into = _writes_into_others(conn, module)
    served, called = _module_endpoints(project, module)
    hotspots = tuple(
        r[0]
        for r in conn.execute(
            "SELECT node_id FROM analytics_hotspots WHERE (',' || module || ',') LIKE ?"
            " ORDER BY fan_in_modules DESC, in_degree DESC, node_id LIMIT 10",
            (f"%,{module},%",),
        )
    )
    _, name, files, screens, ca, ce, instability = row
    return ModuleCard(
        module=module,
        name=name or module,
        files=files,
        screens=screens,
        tables_owned=len(owned),
        ca=ca,
        ce=ce,
        instability=instability,
        shared_tables=tuple(sorted(t for t, shared in owned if shared)),
        owned_written_by_others=tuple(owned_written),
        writes_into_others=tuple(writes_into),
        edges_out=_edges(conn, module, incoming=False),
        edges_in=_edges(conn, module, incoming=True),
        endpoints_served=tuple(served),
        endpoints_called=tuple(called),
        hotspots=hotspots,
        cost=_cost(ca, ce, len(writes_into), len(owned_written)),
    )


# --------------------------------------------------------------------------- relatório


def build_report(
    db: Path,
    cross_dbs: Sequence[Path] = (),
    *,
    module: str | None = None,
    selection: str | None = None,
    depth: int = DEFAULT_DEPTH,
    top: int | None = None,
) -> BoundaryReport:
    """O relatório inteiro. `selection` padrão: `matched` com `cross_dbs`, `called` sem."""
    if depth < 1:
        raise UsageError("--depth precisa ser pelo menos 1")
    chosen = selection or ("matched" if cross_dbs else "called")
    if chosen not in SELECTIONS:
        raise UsageError(f"--endpoints aceita: {', '.join(SELECTIONS)}")
    if chosen == "matched" and not cross_dbs:
        raise UsageError("--endpoints matched precisa de --cross (outro projeto com grafo)")
    meta = store.load_meta(db)
    slug = project_label(db, meta)[0]
    warnings: list[str] = []
    cross = cross_projects([db, *cross_dbs]) if cross_dbs else None
    with ExitStack() as stack:
        main = _load_project(stack, db, slug, meta)
        others = []
        for other in cross_dbs:
            other_meta = store.load_meta(other)
            others.append(
                _load_project(stack, other, project_label(other, other_meta)[0], other_meta)
            )
        plans = endpoint_plans(main, others, selection=chosen, depth=depth, cross=cross)
        conn = main.nb.conn  # a conexão somente leitura do projeto principal
        has_modules, total, modules_warning = _has_modules(conn)
        card = None
        candidates: list[Candidate] = []
        matrix: list[MatrixCell] = []
        if has_modules:
            candidates = extraction_candidates(conn, top)
            matrix = coupling_matrix(conn)
            if module is not None:
                card = module_card(conn, main, module)
        else:
            warnings.append(modules_warning or NO_MODULES_WARNING)
            if module is not None:
                raise UsageError(f"módulo inexistente: {module} ({warnings[-1]})")
    counts = Counter(p.natureza for p in plans)
    return BoundaryReport(
        project=slug,
        db=str(db),
        depth=depth,
        selection=chosen,
        has_modules=has_modules,
        modules_total=total,
        natureza_counts={n: counts.get(n, 0) for n in NATUREZAS},
        endpoints=tuple(plans),
        candidates=tuple(candidates),
        matrix=tuple(matrix),
        module=card,
        warnings=tuple(warnings),
    )


def build_module_card(db: Path, module: str) -> ModuleCard:
    """Só o cartão de um módulo (sem o plano de corte), para o MCP."""
    meta = store.load_meta(db)
    with ExitStack() as stack:
        main = _load_project(stack, db, project_label(db, meta)[0], meta)
        has_modules, _total, warning = _has_modules(main.nb.conn)
        if not has_modules:
            raise UsageError(f"módulo inexistente: {module} ({warning})")
        return module_card(main.nb.conn, main, module)


def build_candidates(db: Path, top: int | None = None) -> tuple[bool, int, list[Candidate]]:
    """`(tem módulos, total de módulos, ranking)`, sem o plano de corte, para o MCP."""
    with closing(store.connect_readonly(db)) as conn:
        has_modules, total, _warning = _has_modules(conn)
        return has_modules, total, extraction_candidates(conn, top) if has_modules else []
