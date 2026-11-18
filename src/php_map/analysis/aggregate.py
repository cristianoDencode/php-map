"""Agregados do mapeamento, calculados no `scan`.

Acoplamento entre módulos (Ca, Ce, instabilidade), tabelas compartilhadas, pontos quentes e
telas fora do menu, num passe linear sobre as arestas do builder (sem networkx). As tabelas
`analytics_*` são gravadas depois do `save`, numa transação própria, como o índice de busca.

Regras:
- contam as arestas de `TRAVERSED` (as do `trace`) com confiança ≥ `analytics_min_confidence`
  e sem infraestrutura nas duas pontas; cada aresta agregada pesa 1 (presença), não
  `occurrences`;
- módulo de um nó: `BELONGS_TO` (qualquer fonte) de `File` e `Table`; `Class`/`Function`
  herdam dos arquivos que as declaram, e `Method` da classe. Sem módulo → `(sem módulo)`;
- chamada local: se o arquivo de quem chama também declara o símbolo (o índice global
  junta num nó só as funções homônimas de telas diferentes), o destino fica só com o
  módulo desse arquivo, e não com o de todos os declarantes;
- aresta entre nós com vários módulos conta para cada par, com peso `1 / (n_src × n_dst)`;
  pares do mesmo módulo não contam;
- arestas com `Endpoint`, `Route` ou JS numa ponta ficam de
  fora: são fronteira ou ponte sem módulo. O `MAPS_TO` (model → tabela) conta como
  `code`;
- `(sem módulo)` aparece nas arestas e tem linha própria, mas não conta como módulo vizinho no
  Ca/Ce dos outros (senão todo módulo ganharia +1 só pela falta de cobertura).
"""

import json
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from php_map.analysis import TRAVERSED
from php_map.analysis.routine_origin import Mention, RoutineOrigins, compute_origins
from php_map.config import Settings
from php_map.errors import StoreError
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import EdgeType, NodeLabel, key_of

NO_MODULE = "(sem módulo)"
MAX_SAMPLES = 10
HOTSPOT_LABELS = ("File", "Table", "PgFunction", "Method", "Function")
READ_OPS = frozenset({"QUERIES"})
WRITE_OPS = frozenset({"INSERTS", "UPDATES", "DELETES"})
# Tela da raiz do e-cidade (a mesma regex de `scripts/measure_precision.SCREEN`).
# `File` que não é código PHP: XML de requisição do webservice e JS.
NOT_CODE_FILE_KINDS = frozenset({"ws_xml", "js"})
SCREEN = re.compile(r"^[a-z]{3}\d_.+\.php$")

SCHEMA = """
DROP TABLE IF EXISTS analytics_modules;
DROP TABLE IF EXISTS analytics_module_edges;
DROP TABLE IF EXISTS analytics_tables;
DROP TABLE IF EXISTS analytics_hotspots;
DROP TABLE IF EXISTS analytics_screens;
DROP TABLE IF EXISTS analytics_endpoints;
DROP TABLE IF EXISTS analytics_routines;
DROP TABLE IF EXISTS analytics_routine_entries;
DROP TABLE IF EXISTS analytics_routine_mentions;
CREATE TABLE analytics_modules (
  module TEXT PRIMARY KEY, name TEXT, files INTEGER, screens INTEGER, tables INTEGER,
  ca INTEGER, ce INTEGER, instability REAL, weight_in REAL, weight_out REAL,
  shared_tables INTEGER
);
CREATE TABLE analytics_module_edges (
  src TEXT, dst TEXT, kind TEXT, weight REAL, edges INTEGER, samples TEXT,
  PRIMARY KEY (src, dst, kind)
);
CREATE TABLE analytics_tables (
  table_id TEXT PRIMARY KEY, owner_modules TEXT, reader_modules TEXT, writer_modules TEXT,
  readers INTEGER, writers INTEGER, shared INTEGER
);
CREATE TABLE analytics_hotspots (
  node_id TEXT PRIMARY KEY, label TEXT, module TEXT, in_degree INTEGER, out_degree INTEGER,
  fan_in_modules INTEGER, rank INTEGER
);
CREATE TABLE analytics_screens (
  file_id TEXT PRIMARY KEY, module TEXT, in_menu INTEGER, menu_items INTEGER,
  tables_written INTEGER, tables_read INTEGER, reached_by INTEGER
);
CREATE TABLE analytics_endpoints (
  system TEXT, protocol TEXT, kind TEXT, endpoints INTEGER, served INTEGER, called INTEGER,
  called_unserved INTEGER, with_params INTEGER, unknown_target INTEGER,
  PRIMARY KEY (system, protocol, kind)
);
CREATE TABLE analytics_routines (
  routine_id TEXT PRIMARY KEY, status TEXT, reason TEXT, module TEXT, module_confidence REAL,
  generated INTEGER, callers_php INTEGER, callers_routines INTEGER, triggers INTEGER,
  entries INTEGER
);
CREATE TABLE analytics_routine_entries (
  routine_id TEXT, kind TEXT, target TEXT, label TEXT, distance INTEGER, via TEXT,
  confidence REAL
);
CREATE INDEX ix_routine_entries ON analytics_routine_entries(routine_id);
CREATE TABLE analytics_routine_mentions (
  routine_id TEXT, file_id TEXT, line INTEGER, source TEXT, confidence REAL
);
CREATE INDEX ix_routine_mentions ON analytics_routine_mentions(routine_id);
"""


# --------------------------------------------------------------------------- resultado


@dataclass(frozen=True, slots=True)
class ModuleRow:
    module: str
    name: str
    files: int
    screens: int
    tables: int
    ca: int
    ce: int
    instability: float | None
    weight_in: float
    weight_out: float
    shared_tables: int


@dataclass(frozen=True, slots=True)
class ModuleEdgeRow:
    src: str
    dst: str
    kind: str  # 'code' | 'table_read' | 'table_write'
    weight: float
    edges: int
    samples: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class TableRow:
    table_id: str
    owner_modules: tuple[str, ...]
    reader_modules: dict[str, int]
    writer_modules: dict[str, int]
    shared: bool

    @property
    def readers(self) -> int:
        return len(self.reader_modules)

    @property
    def writers(self) -> int:
        return len(self.writer_modules)


@dataclass(frozen=True, slots=True)
class HotspotRow:
    node_id: str
    label: str
    module: str
    in_degree: int
    out_degree: int
    fan_in_modules: int
    rank: int  # posição dentro do label


@dataclass(frozen=True, slots=True)
class ScreenRow:
    file_id: str
    module: str
    in_menu: bool
    menu_items: int
    tables_written: int
    tables_read: int
    reached_by: int


@dataclass(frozen=True, slots=True)
class EndpointRow:
    """Endpoints de um sistema/protocolo/tipo: quantos, servidos, chamados e os
    chamados sem quem sirva **neste grafo** (no grafo de quem chama, são todos os remotos: o
    casamento de verdade é o `cross`)."""

    system: str
    protocol: str
    kind: str
    endpoints: int
    served: int
    called: int
    called_unserved: int
    with_params: int = 0  # com parâmetro declarado (XML, assinatura ou `$oParam`)
    unknown_target: int = 0  # destino `?` (saída para fora cujo destino não é literal)


@dataclass(slots=True)
class AnalyticsStats:
    modules: int = 0
    module_edges: int = 0
    tables_shared: int = 0
    hotspots: int = 0
    screens: int = 0
    screens_in_menu: int = 0
    endpoints: int = 0
    edges_counted: int = 0  # arestas do grafo que passaram nos filtros
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class Analytics:
    modules: tuple[ModuleRow, ...]
    module_edges: tuple[ModuleEdgeRow, ...]
    tables: tuple[TableRow, ...]
    hotspots: tuple[HotspotRow, ...]
    screens: tuple[ScreenRow, ...]
    stats: AnalyticsStats = field(default_factory=AnalyticsStats)
    endpoints: tuple[EndpointRow, ...] = ()
    # origem de cada `PgFunction` (menu, rota, endpoint ou arquivo; ou o motivo).
    routine_origins: RoutineOrigins | None = None


# --------------------------------------------------------------------------- cálculo


@dataclass(slots=True)
class _Pair:
    weight: float = 0.0
    edges: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)


def build_analytics(builder: GraphBuilder, settings: Settings) -> Analytics:
    """Agregados do grafo em memória. Função pura: não grava nada."""
    started = time.perf_counter()
    labels, infra, real = _nodes(builder)
    info = _membership(builder, labels)
    membership, owners, menu_items = info.modules, info.owners, info.menu_items
    method_class, declared_in = info.method_class, info.declared_in
    infra |= {method for method, cls in method_class.items() if cls in infra}
    js_files = {
        node
        for node, label in labels.items()
        if label == NodeLabel.FILE and builder.node_attrs(node).get("kind") == "js"
    }

    def modules_of(node: str) -> tuple[str, ...]:
        return membership.get(node) or (NO_MODULE,)

    def files_of(node: str) -> set[str]:
        if labels.get(node) == NodeLabel.FILE:
            return {node}
        return declared_in.get(method_class.get(node, node), set())

    def target_modules(src: str, dst: str) -> tuple[str, ...]:
        declarers = declared_in.get(method_class.get(dst, dst))
        if declarers is None or len(declarers) < 2:
            return modules_of(dst)
        local = files_of(src) & declarers  # chamada local: vale só o arquivo de quem chama
        if not local:
            return modules_of(dst)
        return tuple(sorted({m for f in local for m in membership.get(f, ())})) or (NO_MODULE,)

    pairs: dict[tuple[str, str, str], _Pair] = {}
    in_degree: Counter[str] = Counter()
    out_degree: Counter[str] = Counter()
    fan_in: dict[str, set[str]] = {}
    readers: dict[str, Counter[str]] = {}
    writers: dict[str, Counter[str]] = {}
    direct_read: dict[str, set[str]] = {}
    direct_write: dict[str, set[str]] = {}
    instantiates: dict[str, set[str]] = {}
    reached_by: dict[str, set[str]] = {}
    counted = 0
    for edge in builder.iter_edges():
        type_ = edge.type.value
        if type_ not in TRAVERSED or edge.confidence < settings.analytics_min_confidence:
            continue
        if edge.src in infra or edge.dst in infra:
            continue
        if NodeLabel.ENDPOINT in (labels.get(edge.src), labels.get(edge.dst)):
            continue  # fronteira do sistema: sem módulo, só poluiria o acoplamento
        if edge.src in js_files or edge.dst in js_files:
            continue  # JS: sem módulo, não é código PHP de módulo
        if NodeLabel.ROUTE in (labels.get(edge.src), labels.get(edge.dst)):
            continue  # rota: sem módulo; `ROUTES_TO`/`CALLS_HTTP` só poluiriam
        counted += 1
        src_modules, dst_modules = modules_of(edge.src), target_modules(edge.src, edge.dst)
        is_table = labels.get(edge.dst) == NodeLabel.TABLE and type_ in READ_OPS | WRITE_OPS
        kind = ("table_read" if type_ in READ_OPS else "table_write") if is_table else "code"
        share = 1.0 / (len(src_modules) * len(dst_modules))
        for a in src_modules:
            for b in dst_modules:
                if a == b:
                    continue
                pair = pairs.setdefault((a, b, kind), _Pair())
                pair.weight += share
                pair.edges += 1
                if len(pair.samples) < MAX_SAMPLES:
                    pair.samples.append(
                        {
                            "src_node": edge.src,
                            "dst_node": edge.dst,
                            "type": type_,
                            "path": edge.evidence.get("path"),
                            "line": edge.evidence.get("line"),
                        }
                    )
        in_degree[edge.dst] += 1
        out_degree[edge.src] += 1
        fan_in.setdefault(edge.dst, set()).update(m for m in src_modules if m != NO_MODULE)
        if labels.get(edge.src) == NodeLabel.FILE:
            reached_by.setdefault(edge.dst, set()).add(edge.src)
        if is_table:
            usage = readers if kind == "table_read" else writers
            usage.setdefault(edge.dst, Counter()).update(src_modules)
            direct = direct_read if kind == "table_read" else direct_write
            direct.setdefault(edge.src, set()).add(edge.dst)
        elif type_ == EdgeType.INSTANTIATES:
            instantiates.setdefault(edge.src, set()).add(edge.dst)

    module_edges = tuple(
        ModuleEdgeRow(a, b, kind, round(p.weight, 6), p.edges, tuple(p.samples))
        for (a, b, kind), p in sorted(pairs.items())
    )
    tables = _tables(labels, infra, owners, readers, writers)
    rows = _module_rows(builder, labels, real, infra, membership, module_edges, tables)
    hotspots = _hotspots(labels, membership, in_degree, out_degree, fan_in, settings)
    screens = _screens(
        labels,
        real,
        membership,
        menu_items,
        reached_by,
        _class_tables(method_class, direct_read),
        _class_tables(method_class, direct_write),
        direct_read,
        direct_write,
        instantiates,
    )
    stats = AnalyticsStats(
        modules=len(rows),
        module_edges=len(module_edges),
        tables_shared=sum(1 for t in tables if t.shared),
        hotspots=len(hotspots),
        screens=len(screens),
        screens_in_menu=sum(1 for s in screens if s.in_menu),
        edges_counted=counted,
        seconds=round(time.perf_counter() - started, 2),
    )
    endpoints = _endpoint_rows(builder)
    stats.endpoints = sum(row.endpoints for row in endpoints)
    origins = compute_origins(
        ((n.id, n.label.value, n.attrs) for n in builder.iter_nodes()),
        ((e.src, e.dst, e.type.value, e.confidence) for e in builder.iter_edges()),
        min_confidence=settings.analytics_min_confidence,
    )
    return Analytics(rows, module_edges, tables, hotspots, screens, stats, endpoints, origins)


def _endpoint_rows(builder: GraphBuilder) -> tuple[EndpointRow, ...]:
    """Contagem de `Endpoint` por sistema, protocolo e `kind` (`(chamado)` = só placeholder)."""
    served: set[str] = set()
    called: set[str] = set()
    for edge in builder.iter_edges():
        if edge.type == EdgeType.SERVES:
            served.add(edge.dst)
        elif edge.type == EdgeType.CALLS_REMOTE:
            called.add(edge.dst)
    groups: dict[tuple[str, str, str], list[int]] = {}
    for node in builder.iter_nodes():
        if node.label != NodeLabel.ENDPOINT:
            continue
        key = (
            str(node.attrs.get("system") or ""),
            str(node.attrs.get("protocol") or ""),
            str(node.attrs.get("kind") or "(chamado)"),
        )
        row = groups.setdefault(key, [0, 0, 0, 0, 0, 0])
        row[0] += 1
        is_served, is_called = node.id in served, node.id in called
        row[1] += is_served
        row[2] += is_called
        row[3] += is_called and not is_served
        row[4] += bool(node.attrs.get("params"))
        row[5] += str(node.attrs.get("name")) == "?"
    return tuple(EndpointRow(*key, *counts) for key, counts in sorted(groups.items()))


def _nodes(builder: GraphBuilder) -> tuple[dict[str, NodeLabel], set[str], set[str]]:
    """Label de cada nó, os nós `is_infra` e os reais (não placeholder)."""
    labels: dict[str, NodeLabel] = {}
    infra: set[str] = set()
    real: set[str] = set()
    for node in builder.iter_nodes():
        labels[node.id] = node.label
        if node.attrs.get("is_infra"):
            infra.add(node.id)
        if node.attrs.get("resolved") is not False:
            real.add(node.id)
    return labels, infra, real


@dataclass(frozen=True, slots=True)
class _Membership:
    modules: dict[str, tuple[str, ...]]  # nó → módulos
    owners: dict[str, tuple[str, ...]]  # Table → módulos do dicionário
    menu_items: Counter[str]  # File → nº de IMPLEMENTS_ACTION
    method_class: dict[str, str]  # Method → Class
    declared_in: dict[str, set[str]]  # Class/Function → Files que a declaram


def _membership(builder: GraphBuilder, labels: Mapping[str, NodeLabel]) -> _Membership:
    """Módulos de cada nó, donos de cada tabela, itens de menu, `Method → Class` e declarantes.

    `File`/`Table` pelo `BELONGS_TO`; `Class`/`Function` pelos arquivos que os declaram;
    `Method` pela classe. Resolvido uma vez, antes do passe das arestas.
    """
    direct: dict[str, set[str]] = {}
    owners: dict[str, set[str]] = {}
    declared_in: dict[str, set[str]] = {}
    method_class: dict[str, str] = {}
    menu_items: Counter[str] = Counter()
    for edge in builder.iter_edges():
        if edge.type == EdgeType.BELONGS_TO:
            direct.setdefault(edge.src, set()).add(edge.dst)
            if labels.get(edge.src) == NodeLabel.TABLE:
                owners.setdefault(edge.src, set()).add(edge.dst)
        elif edge.type == EdgeType.DECLARES:
            if labels.get(edge.src) == NodeLabel.FILE:
                declared_in.setdefault(edge.dst, set()).add(edge.src)
            elif labels.get(edge.dst) == NodeLabel.METHOD:
                method_class[edge.dst] = edge.src
        elif edge.type == EdgeType.IMPLEMENTS_ACTION:
            menu_items[edge.src] += 1
    membership: dict[str, tuple[str, ...]] = {
        node: tuple(sorted(modules)) for node, modules in direct.items()
    }
    for symbol, files in declared_in.items():
        modules = sorted({m for f in files for m in direct.get(f, ())})
        if modules:
            membership[symbol] = tuple(modules)
    for method, cls in method_class.items():
        if cls in membership:
            membership[method] = membership[cls]
    owner_map = {table: tuple(sorted(modules)) for table, modules in owners.items()}
    return _Membership(membership, owner_map, menu_items, method_class, declared_in)


def _tables(
    labels: Mapping[str, NodeLabel],
    infra: set[str],
    owners: Mapping[str, tuple[str, ...]],
    readers: Mapping[str, Counter[str]],
    writers: Mapping[str, Counter[str]],
) -> tuple[TableRow, ...]:
    """Tabelas com dono ou com uso. Compartilhada = usada por módulo que não é dono (com dono)
    ou por 2+ módulos (sem dono). `(sem módulo)` aparece nos mapas, mas não decide."""
    ids = {t for t in (*owners, *readers, *writers) if labels.get(t) == NodeLabel.TABLE}
    rows = []
    for table in sorted(ids - infra):
        own = owners.get(table, ())
        read = dict(sorted(readers.get(table, Counter()).items()))
        write = dict(sorted(writers.get(table, Counter()).items()))
        users = (set(read) | set(write)) - {NO_MODULE}
        shared = bool(users - set(own)) if own else len(users) > 1
        rows.append(TableRow(table, own, read, write, shared))
    return tuple(rows)


def _module_rows(
    builder: GraphBuilder,
    labels: Mapping[str, NodeLabel],
    real: set[str],
    infra: set[str],
    membership: Mapping[str, tuple[str, ...]],
    module_edges: Iterable[ModuleEdgeRow],
    tables: Iterable[TableRow],
) -> tuple[ModuleRow, ...]:
    names = {
        node.id: str(node.attrs.get("name") or key_of(node.id))
        for node in builder.iter_nodes()
        if node.label == NodeLabel.MODULE
    }
    names[NO_MODULE] = NO_MODULE
    files: Counter[str] = Counter()
    screens: Counter[str] = Counter()
    table_count: Counter[str] = Counter()
    for node, label in labels.items():
        if node not in real or node in infra:
            continue
        modules = membership.get(node) or (NO_MODULE,)
        if label == NodeLabel.FILE:
            if builder.node_attrs(node).get("kind") in NOT_CODE_FILE_KINDS:
                continue  # XML de requisição e JS: não são código PHP
            files.update(modules)
            if SCREEN.match(key_of(node)):
                screens.update(modules)
        elif label == NodeLabel.TABLE:
            table_count.update(modules)
    afferent: dict[str, set[str]] = {m: set() for m in names}
    efferent: dict[str, set[str]] = {m: set() for m in names}
    weight_in: dict[str, float] = {}
    weight_out: dict[str, float] = {}
    for edge in module_edges:
        if edge.src != NO_MODULE:
            afferent.setdefault(edge.dst, set()).add(edge.src)
            weight_in[edge.dst] = weight_in.get(edge.dst, 0.0) + edge.weight
        if edge.dst != NO_MODULE:
            efferent.setdefault(edge.src, set()).add(edge.dst)
            weight_out[edge.src] = weight_out.get(edge.src, 0.0) + edge.weight
    shared: Counter[str] = Counter()
    for table in tables:
        if table.shared:
            shared.update(table.owner_modules)
    rows = []
    for module in sorted(names):
        ca, ce = len(afferent.get(module, ())), len(efferent.get(module, ()))
        rows.append(
            ModuleRow(
                module,
                names[module],
                files[module],
                screens[module],
                table_count[module],
                ca,
                ce,
                round(ce / (ca + ce), 4) if ca + ce else None,
                round(weight_in.get(module, 0.0), 4),
                round(weight_out.get(module, 0.0), 4),
                shared[module],
            )
        )
    return tuple(rows)


def _hotspots(
    labels: Mapping[str, NodeLabel],
    membership: Mapping[str, tuple[str, ...]],
    in_degree: Counter[str],
    out_degree: Counter[str],
    fan_in: Mapping[str, set[str]],
    settings: Settings,
) -> tuple[HotspotRow, ...]:
    """Por label: maior `fan_in_modules` e, no desempate, maior `in_degree` (e o ID)."""
    by_label: dict[str, list[str]] = {label: [] for label in HOTSPOT_LABELS}
    for node in in_degree:
        node_label = labels.get(node)
        if node_label is not None and node_label.value in by_label:
            by_label[node_label.value].append(node)
    rows = []
    for label in HOTSPOT_LABELS:
        ranked = sorted(by_label[label], key=lambda n: (-len(fan_in.get(n, ())), -in_degree[n], n))
        for rank, node in enumerate(ranked[: settings.analytics_hotspots], 1):
            rows.append(
                HotspotRow(
                    node,
                    label,
                    ",".join(membership.get(node) or (NO_MODULE,)),
                    in_degree[node],
                    out_degree[node],
                    len(fan_in.get(node, ())),
                    rank,
                )
            )
    return tuple(rows)


def _class_tables(
    method_class: Mapping[str, str], direct: Mapping[str, set[str]]
) -> dict[str, set[str]]:
    """`Class` → tabelas tocadas pelos métodos dela (para as telas que a instanciam)."""
    result: dict[str, set[str]] = {}
    for method, cls in method_class.items():
        if method in direct:
            result.setdefault(cls, set()).update(direct[method])
    return result


def _screens(
    labels: Mapping[str, NodeLabel],
    real: set[str],
    membership: Mapping[str, tuple[str, ...]],
    menu_items: Counter[str],
    reached_by: Mapping[str, set[str]],
    class_read: Mapping[str, set[str]],
    class_write: Mapping[str, set[str]],
    direct_read: Mapping[str, set[str]],
    direct_write: Mapping[str, set[str]],
    instantiates: Mapping[str, set[str]],
) -> tuple[ScreenRow, ...]:
    """Telas da raiz. Tabelas: as do SQL da própria tela e as dos métodos das classes que ela
    instancia (um nível: é assim que as telas gravam, pela DAO)."""
    rows = []
    for node, label in sorted(labels.items()):
        if label != NodeLabel.FILE or node not in real or not SCREEN.match(key_of(node)):
            continue
        read = set(direct_read.get(node, ()))
        write = set(direct_write.get(node, ()))
        for cls in instantiates.get(node, ()):
            read |= class_read.get(cls, set())
            write |= class_write.get(cls, set())
        rows.append(
            ScreenRow(
                node,
                ",".join(membership.get(node) or (NO_MODULE,)),
                menu_items[node] > 0,
                menu_items[node],
                len(write),
                len(read),
                len(reached_by.get(node, set()) - {node}),
            )
        )
    return tuple(rows)


# --------------------------------------------------------------------------- gravação


def write_analytics(db_path: Path, analytics: Analytics) -> None:
    """Recria as tabelas `analytics_*` no banco do grafo, numa transação."""
    if not db_path.is_file():
        raise StoreError(f"banco do grafo inexistente: {db_path}. Rode o scan primeiro.")
    started = time.perf_counter()
    built_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executescript("BEGIN;" + SCHEMA)
            conn.executemany(
                "INSERT INTO analytics_modules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (r.module, r.name, r.files, r.screens, r.tables, r.ca, r.ce,
                     r.instability, r.weight_in, r.weight_out, r.shared_tables)
                    for r in analytics.modules
                ),
            )  # fmt: skip
            conn.executemany(
                "INSERT INTO analytics_module_edges VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (e.src, e.dst, e.kind, e.weight, e.edges, _json(list(e.samples)))
                    for e in analytics.module_edges
                ),
            )
            conn.executemany(
                "INSERT INTO analytics_tables VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    (t.table_id, _json(list(t.owner_modules)), _json(t.reader_modules),
                     _json(t.writer_modules), t.readers, t.writers, int(t.shared))
                    for t in analytics.tables
                ),
            )  # fmt: skip
            conn.executemany(
                "INSERT INTO analytics_hotspots VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    (h.node_id, h.label, h.module, h.in_degree, h.out_degree,
                     h.fan_in_modules, h.rank)
                    for h in analytics.hotspots
                ),
            )  # fmt: skip
            conn.executemany(
                "INSERT INTO analytics_screens VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    (s.file_id, s.module, int(s.in_menu), s.menu_items, s.tables_written,
                     s.tables_read, s.reached_by)
                    for s in analytics.screens
                ),
            )  # fmt: skip
            conn.executemany(
                "INSERT INTO analytics_endpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (e.system, e.protocol, e.kind, e.endpoints, e.served, e.called,
                     e.called_unserved, e.with_params, e.unknown_target)
                    for e in analytics.endpoints
                ),
            )  # fmt: skip
            origins = analytics.routine_origins
            if origins is not None:
                conn.executemany(
                    "INSERT INTO analytics_routines VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        (r.routine_id, r.status, r.reason, r.module, r.module_confidence,
                         int(r.generated), r.callers_php, r.callers_routines, r.triggers,
                         r.entries)
                        for r in origins.routines
                    ),
                )  # fmt: skip
                conn.executemany(
                    "INSERT INTO analytics_routine_entries VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        (e.routine_id, e.kind, e.target, e.label, e.distance,
                         _json(list(e.via)), e.confidence)
                        for e in origins.entries
                    ),
                )  # fmt: skip
            conn.execute(
                "INSERT OR REPLACE INTO meta VALUES (?, ?)", ("analytics_built_at", built_at)
            )
    except sqlite3.Error as exc:
        raise StoreError(f"falha ao gravar os agregados em {db_path}: {exc}") from exc
    stats = analytics.stats
    stats.seconds = round(stats.seconds + time.perf_counter() - started, 2)


def write_mentions(db_path: Path, mentions: Iterable[Mention]) -> None:
    """Grava as menções por texto (hipótese) em `analytics_routine_mentions`."""
    try:
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("DELETE FROM analytics_routine_mentions")
            conn.executemany(
                "INSERT INTO analytics_routine_mentions VALUES (?, ?, ?, ?, ?)",
                ((m.routine_id, m.file_id, m.line, m.source, m.confidence) for m in mentions),
            )
    except sqlite3.Error as exc:
        raise StoreError(f"falha ao gravar as menções em {db_path}: {exc}") from exc


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
