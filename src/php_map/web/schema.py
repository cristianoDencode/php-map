"""Schema GraphQL da API.

Nomes em inglês (camelCase no GraphQL); textos e mensagens em português. Os limites
(`limit` ≤ 500, `maxNodes` ≤ 2000, `depth` ≤ 10) são cortados, não viram erro: o `Subgraph`
avisa em `truncated` e em `summary.clamped`.

As consultas são só leitura. As mutações (o scan, os projetos e o acesso)
exigem `Origin` local e `Content-Type: application/json`, e o
`--read-only` as desliga.
"""

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, TypeVar, cast

import strawberry
from strawberry.extensions import QueryDepthLimiter
from strawberry.fastapi import BaseContext
from strawberry.scalars import JSON

from php_map import config
from php_map.access import users
from php_map.analysis import layers as layers_analysis
from php_map.analysis import routine_origin
from php_map.analysis.impact import DaoTouch
from php_map.config import Settings
from php_map.errors import QueryError
from php_map.graph import store
from php_map.ingest import ast_cache
from php_map.pipeline import ECIDADE_MARKER, validate_source
from php_map.profiles.base import PROFILES
from php_map.search.query import SearchHit as FoundHit
from php_map.search.query import search as run_search
from php_map.web import communication as comm
from php_map.web import jobs, menus, projects, repo, rules
from php_map.web import source as src
from php_map.web import state as projects_state
from php_map.web.access_api import AccessMutations, AccessQueries
from php_map.web.permissions import AdminOnlyMutations
from php_map.web.settings import SCHEMA_SOURCES
from php_map.web.state import AppState
from php_map.web.tunnel_api import TunnelMutations, TunnelQueries

MAX_QUERY_DEPTH = 8


# --------------------------------------------------------------------------- contexto


class Context(BaseContext):
    """Uma conexão somente leitura por requisição, aberta na primeira consulta."""

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self.db_path = state.current_db()  # lido uma vez: a troca do banco não afeta
        self._conn: sqlite3.Connection | None = None

    def require_db(self) -> Path:
        """O banco servido; sem ele (projeto ativo sem scan ainda), um erro que diz o que fazer."""
        if self.db_path is None:
            active = self.state.active_project()
            raise QueryError(
                projects_state.NO_GRAPH.format(name=active.name if active else "ativo")
            )
        return self.db_path

    @property
    def user(self) -> str | None:
        """Usuário logado; `None` sem login ou fora de uma requisição HTTP."""
        request = self.request
        return getattr(request.state, "user", None) if request is not None else None

    @property
    def role(self) -> str:
        """Papel de quem chama. Sem usuário (sem login) vale `admin`; com usuário e sem papel
        informado, o mais restrito."""
        if self.user is None:
            return users.ADMIN
        request = self.request
        return str(getattr(request.state, "role", users.READER)) if request else users.READER

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = store.connect_readonly(self.require_db())
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def workspace(self) -> src.Workspace:
        # Com projetos cadastrados, a pasta é a do projeto ativo, com o marcador
        # do perfil dele; sem eles, vale a ordem de antes (--source, configuração, grafo).
        project = self.state.active_project() if self.state.project_store.exists() else None
        marker = PROFILES[project.profile].required_marker if project else ECIDADE_MARKER
        graph_source = repo.graph_source(self.conn) if self.db_path is not None else None
        return src.resolve_workspace(
            self.state.arg_source,
            self.state.settings_path,
            graph_source,
            project.source if project else None,
            marker,
        )


F = TypeVar("F", bound=Callable[..., Any])


def described(text: str) -> Callable[[F], F]:
    """`strawberry.field(description=…)` sem perder o tipo do resolver (mypy estrito)."""
    return cast(Callable[[F], F], strawberry.field(description=text))


def _ctx(info: strawberry.Info) -> Context:
    context: Context = info.context
    return context


def _limit(value: int, maximum: int = repo.MAX_LIMIT) -> int:
    return max(0, min(value, maximum))


# --------------------------------------------------------------------------- tipos


@strawberry.enum
class Direction(Enum):
    OUT = "OUT"
    IN = "IN"


@strawberry.type
class Edge:
    src: strawberry.ID
    dst: strawberry.ID
    type: str
    source: str
    confidence: float
    evidence: JSON
    occurrences: int
    sample_lines: JSON

    @classmethod
    def of(cls, row: repo.EdgeRow) -> "Edge":
        return cls(
            src=strawberry.ID(row.src),
            dst=strawberry.ID(row.dst),
            type=row.type,
            source=row.source,
            confidence=row.confidence,
            evidence=JSON(row.evidence),
            occurrences=int(row.attrs.get("occurrences", 1)),
            sample_lines=JSON(row.attrs.get("sample_lines", [])),
        )


@strawberry.type
class Membership:
    module: strawberry.ID
    name: str
    source: str
    confidence: float


@strawberry.type
class EdgePage:
    total: int
    items: list[Edge]
    nodes: list["Node"]


@strawberry.type
class Node:
    id: strawberry.ID
    label: str
    name: str | None
    path: str | None
    modules: list[str]
    attrs: JSON

    @classmethod
    def of(cls, row: repo.NodeRow) -> "Node":
        name, path = row.attrs.get("name"), row.attrs.get("path")
        return cls(
            id=strawberry.ID(row.id),
            label=row.label,
            name=str(name) if name is not None else None,
            path=str(path) if path is not None else None,
            modules=row.modules,
            attrs=JSON(row.attrs),
        )

    @described("Arestas saindo (OUT) ou chegando (IN), paginadas.")
    def neighbors(
        self,
        info: strawberry.Info,
        direction: Direction = Direction.OUT,
        types: list[str] | None = None,
        min_confidence: float = 0.0,
        include_infra: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> EdgePage:
        total, edges, found = repo.neighbors(
            _ctx(info).conn,
            str(self.id),
            incoming=direction == Direction.IN,
            types=types,
            min_confidence=min_confidence,
            include_infra=include_infra,
            limit=_limit(limit),
            offset=max(0, offset),
        )
        return EdgePage(
            total=total,
            items=[Edge.of(e) for e in edges],
            nodes=[Node.of(n) for n in found.values()],
        )

    @described("Módulos com a fonte (catálogo ou regra de nome) e a confiança.")
    def memberships(self, info: strawberry.Info) -> list[Membership]:
        return [
            Membership(
                module=strawberry.ID(m["module"]),
                name=m["name"],
                source=m["source"],
                confidence=m["confidence"],
            )
            for m in repo.memberships(_ctx(info).conn, str(self.id))
        ]


@strawberry.type
class DaoUser:
    origin: strawberry.ID
    label: str
    operation: str = strawberry.field(description="INSERT, UPDATE, DELETE ou SELECT.")
    dao_method: str = strawberry.field(description="Método do DAO chamado (`incluir`).")
    confidence: float
    path: str | None
    line: int | None

    @classmethod
    def of(cls, touch: DaoTouch) -> "DaoUser":
        return cls(
            origin=strawberry.ID(touch.origin),
            label=touch.label,
            operation=touch.operation,
            dao_method=touch.dao_method,
            confidence=touch.confidence,
            path=touch.evidence.get("path"),
            line=touch.evidence.get("line"),
        )


@strawberry.type
class Subgraph:
    root: strawberry.ID
    nodes: list[Node]
    edges: list[Edge]
    depth_of: JSON
    parent_of: JSON = strawberry.field(
        description="Pai de cada nó na busca (o nó mais perto da raiz); a raiz não aparece."
    )
    truncated: bool
    summary: JSON
    dao_users: list[DaoUser] = strawberry.field(
        default_factory=list,
        description="Só no impacto: quem chama o DAO (`cl_*`) que toca a tabela.",
    )

    @classmethod
    def of(cls, data: repo.SubgraphData) -> "Subgraph":
        steps = data.walk.walk.steps
        return cls(
            root=strawberry.ID(data.root),
            nodes=[Node.of(n) for n in data.nodes],
            edges=[Edge.of(e) for e in data.edges],
            depth_of=JSON(data.depth_of),
            parent_of=JSON({n: s.parent for n, s in steps.items() if s.parent is not None}),
            truncated=data.truncated,
            summary=JSON(data.summary),
            dao_users=[DaoUser.of(d) for d in data.dao_users],
        )


@strawberry.type
class SearchHit:
    node_id: strawberry.ID
    label: str
    modules: list[str]
    score: float
    matched: int
    field: str
    snippet: str
    next_command: str

    @classmethod
    def of(cls, hit: FoundHit) -> "SearchHit":
        return cls(
            node_id=strawberry.ID(hit.node_id),
            label=hit.label,
            modules=list(hit.modules),
            score=hit.score,
            matched=hit.matched,
            field=hit.field,
            snippet=hit.snippet,
            next_command=hit.next_command,
        )


@strawberry.type
class ModuleStats:
    module: strawberry.ID
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
    owned_tables: int = strawberry.field(
        description="Tabelas de que o módulo é dono (dicionário), inclusive as só referenciadas."
    )
    owned_shared: int = strawberry.field(
        description="Das tabelas próprias, quantas outros módulos leem ou gravam."
    )
    coverage: float | None = strawberry.field(
        description="Parte do peso das dependências que vai para um módulo conhecido (não para"
        " `(sem módulo)`); nulo sem dependências."
    )


@strawberry.type
class ModuleEdge:
    src: strawberry.ID
    dst: strawberry.ID
    kind: str
    weight: float
    edges: int
    samples: JSON


@strawberry.type
class TableStats:
    table_id: strawberry.ID
    owner_modules: list[str]
    reader_modules: JSON
    writer_modules: JSON
    readers: int
    writers: int
    shared: bool


@strawberry.type
class Hotspot:
    node_id: strawberry.ID
    label: str
    module: str
    in_degree: int
    out_degree: int
    fan_in_modules: int
    rank: int


@strawberry.type
class ScreenStats:
    file_id: strawberry.ID
    module: str
    in_menu: bool
    menu_items: int
    tables_written: int
    tables_read: int
    reached_by: int


@strawberry.type
class ModuleDetail:
    module: ModuleStats
    incoming: list[ModuleEdge]
    outgoing: list[ModuleEdge]
    tables: list[TableStats]
    screens: list[ScreenStats]
    writes_foreign: list[TableStats] = strawberry.field(
        description="Tabelas de outros módulos que este grava."
    )
    hotspots: list["Hotspot"]


@strawberry.type
class Workspace:
    source: str | None
    origin: str
    exists: bool
    is_ecidade: bool
    graph_source: str | None
    marker: str = strawberry.field(
        default="libs/db_stdlib.php", description="Arquivo que o perfil exige na raiz da pasta."
    )


@strawberry.type
class SourceFile:
    path: str
    total_lines: int
    start_line: int
    lines: list[str]
    highlight: int | None
    truncated: bool
    size_bytes: int


@strawberry.type(description="Ponto de origem de uma função: menu, rota, endpoint ou arquivo.")
class RoutineOriginEntry:
    kind: str = strawberry.field(description="menu, route, endpoint ou file.")
    target: strawberry.ID
    label: str
    distance: int = strawberry.field(description="1 = chamador direto.")
    via: list[str] = strawberry.field(description="Da origem até a função.")
    confidence: float = strawberry.field(description="A menor das arestas do caminho.")


@strawberry.type(description="Arquivo que cita o nome da função: hipótese, não aresta.")
class RoutineMention:
    file: strawberry.ID
    line: int
    confidence: float
    hypothesis: bool


@strawberry.type(description="Por onde a função da base é usada.")
class RoutineOrigin:
    status: str = strawberry.field(
        description="com_origem, so_arquivo, gerada, sem_chamador ou cadeia_sem_origem."
    )
    reason: str
    module: str | None
    module_confidence: float | None
    generated: bool
    callers_php: int
    callers_routines: int
    triggers: int
    entries: list[RoutineOriginEntry]
    mentions: list[RoutineMention]

    @classmethod
    def of(cls, data: dict[str, Any]) -> "RoutineOrigin":
        callers = data["callers"]
        return cls(
            status=data["status"],
            reason=data["reason"],
            module=data["module"],
            module_confidence=data["module_confidence"],
            generated=data["generated"],
            callers_php=callers["php"],
            callers_routines=callers["functions"],
            triggers=callers["triggers"],
            entries=[
                RoutineOriginEntry(
                    kind=e["kind"],
                    target=strawberry.ID(e["target"]),
                    label=e["label"],
                    distance=e["distance"],
                    via=e["via"],
                    confidence=e["confidence"],
                )
                for e in data["entries"]
            ],
            mentions=[
                RoutineMention(
                    file=strawberry.ID(m["file"]),
                    line=m["line"],
                    confidence=m["confidence"],
                    hypothesis=m["hypothesis"],
                )
                for m in data["mentions"]
            ],
        )


@strawberry.type(description="Texto de uma função da base, em páginas de linhas.")
class RoutineSource:
    id: strawberry.ID
    schema: str
    name: str
    language: str
    generated: bool = strawberry.field(description="Gerada pela auditoria (`fc_auditoria_*`).")
    read_at: str = strawberry.field(description="Quando o scan leu o texto da base.")
    sha1: str
    total_lines: int
    start_line: int
    end_line: int
    lines: list[str] = strawberry.field(description="Linhas `start_line`..`end_line`, sem número.")
    truncated: bool

    @described("Por onde a função é usada; nulo em grafo antigo.")
    def origin(self, info: strawberry.Info) -> RoutineOrigin | None:
        data = routine_origin.read_origin(_ctx(info).conn, str(self.id))
        return RoutineOrigin.of(data) if data else None

    @classmethod
    def of(cls, page: repo.RoutinePage) -> "RoutineSource":
        return cls(
            id=strawberry.ID(page.node_id),
            schema=page.schema,
            name=page.name,
            language=page.language,
            generated=page.generated,
            read_at=page.read_at,
            sha1=page.sha1,
            total_lines=page.total_lines,
            start_line=page.start_line,
            end_line=page.end_line,
            lines=page.lines,
            truncated=page.truncated,
        )


# --------------------------------------------------------------------------- ficha


@strawberry.type(description="Evidência `arquivo:linha` e o nó de onde a aresta parte.")
class RuleEvidence:
    path: str
    line: int | None
    origin: strawberry.ID = strawberry.field(name="from")

    @classmethod
    def of(cls, item: rules.Evidence) -> "RuleEvidence":
        return cls(path=item.path, line=item.line, origin=strawberry.ID(item.origin))


@strawberry.type
class RuleMenu:
    id: strawberry.ID
    path: str | None
    paths: list[str]
    help: str | None
    desctec: str | None
    active: bool
    params: str | None


@strawberry.type
class RuleAction:
    file: str
    cases: list[str]


@strawberry.type
class RuleParticipant:
    node: strawberry.ID
    label: str
    name: str | None
    depth: int
    via: str | None
    parent: strawberry.ID | None
    confidence: float | None
    evidence: JSON


@strawberry.type
class RuleColumn:
    column: str
    label: str | None
    description: str | None


@strawberry.type
class RuleTable:
    table: strawberry.ID
    name: str
    operations: list[str]
    catalog: JSON | None
    columns_labeled: list[RuleColumn]
    evidence: list[RuleEvidence]
    occurrences: int


@strawberry.type
class RuleRpc:
    function: strawberry.ID
    name: str
    resolved: bool
    evidence: RuleEvidence


@strawberry.type
class RuleTrigger:
    table: strawberry.ID
    function: strawberry.ID
    evidence: RuleEvidence | None


@strawberry.type
class RuleMessage:
    text: str
    kind: str
    category: str = strawberry.field(
        description="Classificação heurística por palavra-chave: validação, erro ou mensagem."
    )
    file: str
    line: int
    scope: str


@strawberry.type
class RuleNote:
    text: str
    file: str
    line: int
    scope: str


@strawberry.type
class RuleStats:
    files: int
    methods: int
    tables: int
    messages: int
    notes: int
    truncated: bool


@strawberry.type
class BusinessRules:
    entry: strawberry.ID
    depth: int
    min_confidence: float
    modules: list[Membership]
    menus: list[RuleMenu]
    actions: list[RuleAction]
    participants: list[RuleParticipant]
    writes: list[RuleTable]
    reads: list[RuleTable]
    rpc: list[RuleRpc]
    triggers: list[RuleTrigger]
    messages: list[RuleMessage]
    notes: list[RuleNote]
    stats: RuleStats
    clamped: list[str]


def _rule_table(use: rules.TableUse) -> RuleTable:
    return RuleTable(
        table=strawberry.ID(use.table),
        name=use.name,
        operations=use.operations,
        catalog=JSON(use.catalog) if use.catalog else None,
        columns_labeled=[
            RuleColumn(column=c.column, label=c.label, description=c.description)
            for c in use.columns_labeled
        ],
        evidence=[RuleEvidence.of(e) for e in use.evidence],
        occurrences=use.occurrences,
    )


def _business_rules(data: rules.BusinessRules) -> BusinessRules:
    return BusinessRules(
        entry=strawberry.ID(data.entry),
        depth=data.depth,
        min_confidence=data.min_confidence,
        modules=[
            Membership(
                module=strawberry.ID(m["module"]),
                name=m["name"],
                source=m["source"],
                confidence=m["confidence"],
            )
            for m in data.modules
        ],
        menus=[
            RuleMenu(
                id=strawberry.ID(m.id),
                path=m.path,
                paths=m.paths,
                help=m.help,
                desctec=m.desctec,
                active=m.active,
                params=m.params,
            )
            for m in data.menus
        ],
        actions=[RuleAction(file=a.file, cases=a.cases) for a in data.actions],
        participants=[
            RuleParticipant(
                node=strawberry.ID(p.node),
                label=p.label,
                name=p.name,
                depth=p.depth,
                via=p.via,
                parent=strawberry.ID(p.parent) if p.parent else None,
                confidence=p.confidence,
                evidence=JSON(p.evidence),
            )
            for p in data.participants
        ],
        writes=[_rule_table(t) for t in data.writes],
        reads=[_rule_table(t) for t in data.reads],
        rpc=[
            RuleRpc(
                function=strawberry.ID(r.function),
                name=r.name,
                resolved=r.resolved,
                evidence=RuleEvidence.of(r.evidence),
            )
            for r in data.rpc
        ],
        triggers=[
            RuleTrigger(
                table=strawberry.ID(t.table),
                function=strawberry.ID(t.function),
                evidence=RuleEvidence.of(t.evidence) if t.evidence else None,
            )
            for t in data.triggers
        ],
        messages=[
            RuleMessage(
                text=m.text,
                kind=m.kind,
                category=m.category or "mensagem",
                file=m.file,
                line=m.line,
                scope=m.scope,
            )
            for m in data.messages
        ],
        notes=[RuleNote(text=n.text, file=n.file, line=n.line, scope=n.scope) for n in data.notes],
        stats=RuleStats(
            files=data.stats.files,
            methods=data.stats.methods,
            tables=data.stats.tables,
            messages=data.stats.messages,
            notes=data.stats.notes,
            truncated=data.stats.truncated,
        ),
        clamped=data.clamped,
    )


def _module_stats(row: tuple[Any, ...]) -> ModuleStats:
    module, name, files, screens, tables, ca, ce, inst, w_in, w_out, shared, owned = row[:12]
    owned_shared, coverage = row[12:]
    return ModuleStats(
        module=strawberry.ID(module),
        name=name,
        files=files,
        screens=screens,
        tables=tables,
        ca=ca,
        ce=ce,
        instability=inst,
        weight_in=w_in,
        weight_out=w_out,
        shared_tables=shared,
        owned_tables=owned,
        owned_shared=owned_shared,
        coverage=coverage,
    )


def _module_edge(row: tuple[Any, ...]) -> ModuleEdge:
    src_id, dst_id, kind, weight, edges, samples = row
    return ModuleEdge(
        src=strawberry.ID(src_id),
        dst=strawberry.ID(dst_id),
        kind=kind,
        weight=weight,
        edges=edges,
        samples=JSON(json.loads(samples)),
    )


def _table_stats(row: tuple[Any, ...]) -> TableStats:
    table, owners, readers, writers, n_readers, n_writers, shared = row
    return TableStats(
        table_id=strawberry.ID(table),
        owner_modules=json.loads(owners),
        reader_modules=JSON(json.loads(readers)),
        writer_modules=JSON(json.loads(writers)),
        readers=n_readers,
        writers=n_writers,
        shared=bool(shared),
    )


def _hotspot(row: tuple[Any, ...]) -> Hotspot:
    return Hotspot(
        node_id=strawberry.ID(row[0]),
        label=row[1],
        module=row[2],
        in_degree=row[3],
        out_degree=row[4],
        fan_in_modules=row[5],
        rank=row[6],
    )


def _screen(row: tuple[Any, ...]) -> ScreenStats:
    file, module, in_menu, items, written, read, reached = row
    return ScreenStats(
        file_id=strawberry.ID(file),
        module=module,
        in_menu=bool(in_menu),
        menu_items=items,
        tables_written=written,
        tables_read=read,
        reached_by=reached,
    )


# --------------------------------------------------------------------------- menus por fonte


@strawberry.type
class MenuModule:
    module: strawberry.ID
    name: str


@strawberry.type
class ViaStep:
    """Passo do caminho, do arquivo de entrada até o fonte; a aresta é a do passo anterior
    até este (`incoming`: a aresta real vai deste nó para o anterior, como `DECLARES`)."""

    node: strawberry.ID
    label: str
    edge_type: str | None
    confidence: float | None
    path: str | None
    line: int | None
    incoming: bool


@strawberry.type
class FileMenuItem:
    menu: strawberry.ID
    name: str | None
    paths: list[str]
    modules: list[MenuModule]
    active: bool
    params: str | None
    entry: strawberry.ID
    distance: int
    via: list[ViaStep]


@strawberry.type
class DistanceCount:
    distance: int
    count: int


@strawberry.type
class FileMenus:
    file: strawberry.ID | None = strawberry.field(
        description="Arquivo resolvido; nulo quando o nome é ambíguo ou não existe."
    )
    candidates: list[strawberry.ID]
    max_distance: int
    total: int
    unfiltered: int = strawberry.field(
        description="Itens até a distância, antes de módulo e texto."
    )
    by_distance: list[DistanceCount]
    truncated: bool
    clamped: list[str]
    items: list[FileMenuItem]


def _file_menus(data: menus.FileMenus) -> FileMenus:
    return FileMenus(
        file=strawberry.ID(data.file),
        candidates=[],
        max_distance=data.max_distance,
        total=data.total,
        unfiltered=data.unfiltered,
        by_distance=[DistanceCount(distance=d, count=c) for d, c in data.by_distance.items()],
        truncated=data.truncated,
        clamped=data.clamped,
        items=[
            FileMenuItem(
                menu=strawberry.ID(h.menu),
                name=h.name,
                paths=h.paths,
                modules=[MenuModule(module=strawberry.ID(m), name=n) for m, n in h.modules],
                active=h.active,
                params=h.params,
                entry=strawberry.ID(h.entry),
                distance=h.distance,
                via=[
                    ViaStep(
                        node=strawberry.ID(v.node),
                        label=v.label,
                        edge_type=v.edge_type,
                        confidence=v.confidence,
                        path=v.path,
                        line=v.line,
                        incoming=v.incoming,
                    )
                    for v in h.via
                ],
            )
            for h in data.items
        ],
    )


# --------------------------------------------------------------------------- camadas


@strawberry.type(description="Um nó de um caminho de camadas (a aresta é a do passo anterior).")
class LayerStep:
    node: strawberry.ID
    label: str
    layer: str = strawberry.field(description="Deduzida do nome e do tipo; não é aresta.")
    name: str
    edge_type: str | None
    confidence: float | None
    path: str | None
    line: int | None


@strawberry.type(description="Do menu até o fonte, em camadas.")
class LayerPath:
    menu: strawberry.ID
    menu_path: str | None
    distance: int
    confidence: float = strawberry.field(description="A menor confiança das arestas do caminho.")
    steps: list[LayerStep]


@strawberry.type(description="Método de um DAO que o fonte chama.")
class LayerMember:
    name: str
    node: strawberry.ID
    edge_type: str
    confidence: float


@strawberry.type
class LayerDownNode:
    node: strawberry.ID
    label: str
    layer: str
    name: str
    depth: int
    parent: strawberry.ID
    edge_type: str
    confidence: float = strawberry.field(description="A menor confiança do caminho até o nó.")
    path: str | None
    line: int | None
    factory: str | None = strawberry.field(
        description="DAO: `getDao` ou `usarDao`, como o fonte obteve a classe; nulo se não souber."
    )
    members: list[LayerMember] = strawberry.field(
        description="DAO: os métodos que o fonte chama (sem o `__construct`)."
    )


@strawberry.type
class LayerCount:
    layer: str
    count: int


@strawberry.type
class FileLayers:
    node: strawberry.ID | None = strawberry.field(
        description="Fonte resolvido; nulo quando o nome é ambíguo ou não existe."
    )
    candidates: list[strawberry.ID]
    label: str | None
    layer: str | None
    name: str | None
    min_confidence: float
    up: list[LayerPath]
    up_total: int
    up_truncated: bool
    down: list[LayerDownNode]
    down_total: int
    down_truncated: bool
    down_by_layer: list[LayerCount]


def _file_layers(data: layers_analysis.Layers) -> FileLayers:
    return FileLayers(
        node=strawberry.ID(data.node),
        candidates=[],
        label=data.label,
        layer=data.layer,
        name=data.name,
        min_confidence=data.min_confidence,
        up=[
            LayerPath(
                menu=strawberry.ID(p.menu),
                menu_path=p.menu_path,
                distance=p.distance,
                confidence=p.confidence,
                steps=[
                    LayerStep(
                        node=strawberry.ID(s.node),
                        label=s.label,
                        layer=s.layer,
                        name=s.name,
                        edge_type=s.edge_type,
                        confidence=s.confidence,
                        path=s.path,
                        line=s.line,
                    )
                    for s in p.steps
                ],
            )
            for p in data.up
        ],
        up_total=data.up_total,
        up_truncated=data.up_truncated,
        down=[
            LayerDownNode(
                node=strawberry.ID(d.node),
                label=d.label,
                layer=d.layer,
                name=d.name,
                depth=d.depth,
                parent=strawberry.ID(d.parent),
                edge_type=d.edge_type,
                confidence=d.confidence,
                path=d.path,
                line=d.line,
                factory=d.factory,
                members=[
                    LayerMember(
                        name=m.name,
                        node=strawberry.ID(m.node),
                        edge_type=m.edge_type,
                        confidence=m.confidence,
                    )
                    for m in d.members
                ],
            )
            for d in data.down
        ],
        down_total=data.down_total,
        down_truncated=data.down_truncated,
        down_by_layer=[LayerCount(layer=k, count=v) for k, v in data.down_by_layer.items()],
    )


# --------------------------------------------------------------------------- consultas


# --------------------------------------------------------------------------- projetos


@strawberry.type(description="Um sistema mapeado (e-cidade, zend1). O DSN nunca aparece aqui.")
class Project:
    slug: str
    name: str
    profile: str
    source: str
    source_exists: bool
    has_graph: bool
    last_scan: str | None
    schema_from_db: bool
    dsn_env: str | None = strawberry.field(
        description="NOME da variável do .env com a conexão (nunca o valor)."
    )
    dsn_available: bool = strawberry.field(description="A variável existe no ambiente ou no .env.")
    active: bool
    scanning: bool

    @classmethod
    def of(cls, item: projects.Project, ctx: Context) -> "Project":
        state = ctx.state
        dsn_name = item.dsn_env or config.CATALOG_DSN_ENV
        active = state.active_project()
        return cls(
            slug=item.slug,
            name=item.name,
            profile=item.profile,
            source=item.source,
            source_exists=bool(item.source) and Path(item.source).is_dir(),
            has_graph=item.has_graph(),
            last_scan=item.last_scan,
            schema_from_db=item.schema_from_db,
            dsn_env=item.dsn_env,
            dsn_available=config.env_value(dsn_name) is not None,
            active=active is not None and active.slug == item.slug,
            scanning=state.jobs.is_running() and state.jobs.current_slug() == item.slug,
        )


@strawberry.type
class ProfileOption:
    name: str
    marker: str = strawberry.field(description="Arquivo que o perfil exige na raiz da pasta.")


@strawberry.type
class ProfileMarker:
    profile: str
    marker: str
    found: bool


@strawberry.type(
    description="Qual perfil a pasta parece ser. Só sugere: quem confirma é o usuário."
)
class ProfileSuggestion:
    exists: bool
    profile: str | None
    markers: list[ProfileMarker]


@strawberry.input
class ProjectInput:
    name: str
    profile: str
    source: str
    slug: str | None = None
    schema_from_db: bool = False
    dsn_env: str | None = strawberry.field(
        default=None, description="NOME da variável do .env, nunca a conexão."
    )


@strawberry.input
class ProjectUpdate:
    name: str | None = None
    profile: str | None = None
    source: str | None = None
    schema_from_db: bool | None = None
    dsn_env: str | None = None
    clear_dsn_env: bool = False


# ----------------------------------------------------------------- comunicação


@strawberry.type
class CommunicationParam:
    name: str
    required: bool | None
    type: str | None
    source: str | None


@strawberry.type
class CommunicationImpl:
    node: strawberry.ID
    path: str | None
    line: int | None


@strawberry.type
class CommunicationCaller:
    project: str | None = strawberry.field(description="None = o projeto ativo.")
    node: strawberry.ID
    path: str | None
    line: int | None
    module: str | None
    args: JSON
    occurrences: int


@strawberry.type
class CommunicationReach:
    tables: list[strawberry.ID]
    modules: list[strawberry.ID]
    nodes: int


@strawberry.type
class CommunicationRow:
    endpoint_id: strawberry.ID
    node_label: str = strawberry.field(description="Endpoint ou Route.")
    system: str
    protocol: str
    category: str = strawberry.field(description="Protocolo do filtro: externo para o que sai.")
    kind: str | None
    name: str
    direction: str = strawberry.field(description="entra (SERVES/rota) ou sai (CALLS_REMOTE).")
    resolved: bool
    params: list[CommunicationParam]
    implemented_by: list[CommunicationImpl]
    callers: int
    caller_sample: list[CommunicationCaller]
    module: str | None
    reaches: CommunicationReach | None
    status: str | None = strawberry.field(
        description="Do cruzamento: casado, chamado_sem_servidor ou servido_sem_cliente."
    )


@strawberry.type
class CommunicationFacet:
    value: str
    count: int


@strawberry.type
class CommunicationCounters:
    serves: int
    calls: int
    with_params: int
    orphans: int | None = strawberry.field(description="Só com outro projeto cadastrado.")
    cross_available: bool


@strawberry.type
class CommunicationPage:
    total: int
    offset: int
    limit: int
    items: list[CommunicationRow]
    counters: CommunicationCounters
    systems: list[CommunicationFacet]
    protocols: list[CommunicationFacet]
    modules: list[CommunicationFacet]
    cross_note: str | None = strawberry.field(
        description="Por que não há o outro lado da costura (nenhum outro grafo, por exemplo)."
    )


@strawberry.type
class EndpointCounterpart:
    available: bool
    message: str | None
    status: str | None
    served_by: str | None
    servers: list[CommunicationImpl]
    params: list[CommunicationParam]
    reaches: CommunicationReach | None
    callers: list[CommunicationCaller]
    system: str | None


@strawberry.type
class EndpointDetail:
    row: CommunicationRow
    callers: list[CommunicationCaller]
    attrs: JSON
    counterpart: EndpointCounterpart | None


def _comm_params(items: Any) -> list[CommunicationParam]:
    return [
        CommunicationParam(
            name=str(p.get("name", "")),
            required=p.get("required") if isinstance(p.get("required"), bool) else None,
            type=p.get("type"),
            source=p.get("source"),
        )
        for p in items
    ]


def _comm_impl(items: Any) -> list[CommunicationImpl]:
    return [CommunicationImpl(node=strawberry.ID(i.node), path=i.path, line=i.line) for i in items]


def _comm_caller(c: comm.Caller) -> CommunicationCaller:
    return CommunicationCaller(
        project=c.project,
        node=strawberry.ID(c.node),
        path=c.path,
        line=c.line,
        module=c.module,
        args=JSON(c.args),
        occurrences=c.occurrences,
    )


def _comm_reach(reach: comm.ReachSummary | None) -> CommunicationReach | None:
    if reach is None:
        return None
    return CommunicationReach(
        tables=[strawberry.ID(t) for t in reach.tables],
        modules=[strawberry.ID(m) for m in reach.modules],
        nodes=reach.nodes,
    )


def _comm_row(row: comm.Row) -> CommunicationRow:
    return CommunicationRow(
        endpoint_id=strawberry.ID(row.endpoint_id),
        node_label=row.node_label,
        system=row.system,
        protocol=row.protocol,
        category=row.category,
        kind=row.kind,
        name=row.name,
        direction=row.direction,
        resolved=row.resolved,
        params=_comm_params(row.params),
        implemented_by=_comm_impl(row.implemented_by),
        callers=row.callers,
        caller_sample=[_comm_caller(c) for c in row.caller_sample],
        module=row.module,
        reaches=_comm_reach(row.reaches),
        status=row.status,
    )


def _comm_counterpart(item: comm.Counterpart | None) -> EndpointCounterpart | None:
    if item is None:
        return None
    return EndpointCounterpart(
        available=item.available,
        message=item.message,
        status=item.status,
        served_by=item.served_by,
        servers=_comm_impl(item.servers),
        params=_comm_params(item.params),
        reaches=_comm_reach(item.reaches),
        callers=[_comm_caller(c) for c in item.callers],
        system=item.system,
    )


@strawberry.type
class Query(
    AccessQueries,
    TunnelQueries,
):
    @described("Contagens do scan (por label, tipo e fonte) e o `meta`.")
    def stats(self, info: strawberry.Info) -> JSON:
        ctx = _ctx(info)
        data = repo.stats(ctx.conn)
        # O cabeçalho diz quando o grafo servido foi gerado pela interface.
        data["served"] = {"from_interface": ctx.state.from_interface(ctx.require_db())}
        return JSON(data)

    @strawberry.field
    def node(self, info: strawberry.Info, id: strawberry.ID) -> Node | None:
        row = repo.node(_ctx(info).conn, str(id))
        return Node.of(row) if row else None

    @strawberry.field
    def nodes(self, info: strawberry.Info, ids: list[strawberry.ID]) -> list[Node]:
        found = repo.nodes(_ctx(info).conn, [str(i) for i in ids[: repo.MAX_LIMIT]])
        return [Node.of(row) for row in found.values()]

    @described("Busca textual.")
    def search(
        self,
        info: strawberry.Info,
        text: str,
        label: str | None = None,
        module: str | None = None,
        limit: int = 20,
        any_term: bool = False,
    ) -> list[SearchHit]:
        hits = run_search(
            _ctx(info).require_db(),
            text,
            label=label,
            module=module,
            limit=_limit(limit),
            any_term=any_term,
        )
        return [SearchHit.of(h) for h in hits]

    @described("Sugestões enquanto se digita (Ctrl+K): a busca por prefixo.")
    def suggest(
        self, info: strawberry.Info, prefix: str, limit: int = 8, label: str | None = None
    ) -> list[SearchHit]:
        hits = run_search(
            _ctx(info).require_db(), prefix, label=label, limit=_limit(limit, 50), any_term=True
        )
        return [SearchHit.of(h) for h in hits]

    @described("Da tela até o banco (a mesma BFS do `php-map trace`).")
    def trace(
        self,
        info: strawberry.Info,
        entry: str,
        depth: int = 8,
        min_confidence: float = 0.0,
        include_infra: bool = False,
        expand_pg_functions: bool = False,
        max_nodes: int = 500,
    ) -> Subgraph:
        data = repo.trace(
            _ctx(info).conn,
            entry,
            depth=depth,
            min_confidence=min_confidence,
            include_infra=include_infra,
            expand_pgfunctions=expand_pg_functions,
            max_nodes=max_nodes,
        )
        return Subgraph.of(data)

    @described("Quem é afetado por uma tabela (a BFS do `php-map impact`).")
    def impact(
        self,
        info: strawberry.Info,
        table: str,
        schema: str | None = None,
        depth: int = 10,
        min_confidence: float = 0.0,
        include_infra: bool = False,
        max_nodes: int = 500,
    ) -> Subgraph:
        data = repo.impact(
            _ctx(info).conn,
            table,
            schema,
            depth=depth,
            min_confidence=min_confidence,
            include_infra=include_infra,
            max_nodes=max_nodes,
        )
        return Subgraph.of(data)

    @described("Diagrama Mermaid de `trace` (root = arquivo) ou `impact`.")
    def mermaid(
        self,
        info: strawberry.Info,
        kind: str,
        # `root` é reservado pelo Strawberry (objeto pai): o argumento GraphQL mantém o nome.
        target: Annotated[str, strawberry.argument(name="root")],
        depth: int = 6,
        max_nodes: int = 60,
        min_confidence: float = 0.0,
        include_infra: bool = False,
        expand_pg_functions: bool = False,
    ) -> str:
        conn = _ctx(info).conn
        size = max(2, min(max_nodes, repo.MAX_NODES))
        # Mesmos filtros do `trace`/`impact`: o diagrama exportado é o da tela.
        if kind == "trace":
            data = repo.trace(
                conn,
                target,
                depth=depth,
                min_confidence=min_confidence,
                include_infra=include_infra,
                expand_pgfunctions=expand_pg_functions,
                max_nodes=repo.MAX_NODES,
            )
            return repo.mermaid(data, size, reverse=False)
        if kind == "impact":
            schema, _sep, table = target.rpartition(".")
            data = repo.impact(
                conn,
                table,
                schema or None,
                depth=depth,
                min_confidence=min_confidence,
                include_infra=include_infra,
                max_nodes=repo.MAX_NODES,
            )
            return repo.mermaid(data, size, reverse=True)
        raise QueryError("kind aceita trace ou impact")

    @described(
        "Ficha de regras de negócio de uma tela: menus, ações, mensagens, dados"
        " gravados e lidos, funções do banco e triggers, com arquivo e linha."
    )
    def business_rules(
        self,
        info: strawberry.Info,
        entry: str,
        depth: int = rules.DEFAULT_DEPTH,
        min_confidence: float = rules.DEFAULT_MIN_CONFIDENCE,
    ) -> BusinessRules:
        data = rules.business_rules(
            _ctx(info).conn, entry, depth=depth, min_confidence=min_confidence
        )
        return _business_rules(data)

    @described(
        "Autocompletar da tela Menus por fonte: arquivos cujo caminho contém o"
        " texto (mínimo de 2 caracteres), os que casam pelo nome primeiro."
    )
    def find_files(self, info: strawberry.Info, text: str, limit: int = 10) -> list[strawberry.ID]:
        ctx = _ctx(info)
        found = menus.find_files(
            ctx.conn, text, _limit(limit, menus.FIND_LIMIT), ctx.workspace().source
        )
        return [strawberry.ID(f) for f in found]

    @described(
        "Menus do e-cidade que chegam a um fonte: a busca reversa a partir do"
        " arquivo e do que ele declara, com a distância e o caminho de cada menu."
    )
    def menus_for_file(
        self,
        info: strawberry.Info,
        file: str,
        max_distance: int = menus.MENU_DEFAULT_DISTANCE,
        module: str | None = None,
        text: str | None = None,
    ) -> FileMenus:
        ctx = _ctx(info)
        match = menus.resolve_file(ctx.conn, file, workspace_root=ctx.workspace().source)
        if match.file is None:
            return FileMenus(
                file=None,
                candidates=[strawberry.ID(c) for c in match.candidates],
                max_distance=max(0, min(max_distance, menus.MENU_MAX_DISTANCE)),
                total=0,
                unfiltered=0,
                by_distance=[],
                truncated=False,
                clamped=[],
                items=[],
            )
        data = menus.menus_for_file(
            ctx.conn, match.file, max_distance=max_distance, module=module or None, text=text
        )
        return _file_menus(data)

    @described(
        "Camadas de um fonte: os caminhos até o menu (tela, JS, rota, controller) e"
        " o que ele alcança (service, repository, model, tabela). A camada é deduzida do nome."
    )
    def layers(self, info: strawberry.Info, path: str, min_confidence: float = 0.5) -> FileLayers:
        ctx = _ctx(info)
        match = layers_analysis.resolve_target(ctx.conn, path, ctx.workspace().source)
        if match.file is None:
            return FileLayers(
                node=None,
                candidates=[strawberry.ID(c) for c in match.candidates],
                label=None,
                layer=None,
                name=None,
                min_confidence=min_confidence,
                up=[],
                up_total=0,
                up_truncated=False,
                down=[],
                down_total=0,
                down_truncated=False,
                down_by_layer=[],
            )
        options = layers_analysis.LayerOptions(min_confidence=max(0.0, min(1.0, min_confidence)))
        return _file_layers(layers_analysis.file_layers(ctx.conn, match.file, options))

    @described("Módulos com Ca, Ce e instabilidade.")
    def modules(self, info: strawberry.Info) -> list[ModuleStats]:
        return [_module_stats(r) for r in repo.module_rows(_ctx(info).conn)]

    @strawberry.field
    def module(self, info: strawberry.Info, id: strawberry.ID) -> ModuleDetail | None:
        conn = _ctx(info).conn
        rows = repo.module_rows(conn, str(id))
        if not rows:
            return None
        return ModuleDetail(
            module=_module_stats(rows[0]),
            incoming=[_module_edge(r) for r in repo.module_edge_rows(conn, dst=str(id))],
            outgoing=[_module_edge(r) for r in repo.module_edge_rows(conn, src=str(id))],
            tables=[_table_stats(r) for r in repo.table_rows(conn, module=str(id), limit=500)],
            screens=[
                _screen(r)
                for r in repo.screen_rows(conn, in_menu=None, module=str(id), limit=500, offset=0)
            ],
            writes_foreign=[_table_stats(r) for r in repo.foreign_written_rows(conn, str(id))],
            hotspots=[
                _hotspot(r) for r in repo.hotspot_rows(conn, label=None, module=str(id), limit=20)
            ],
        )

    @strawberry.field
    def module_edges(
        self, info: strawberry.Info, kind: str | None = None, min_weight: float = 0.0
    ) -> list[ModuleEdge]:
        rows = repo.module_edge_rows(_ctx(info).conn, kind=kind, min_weight=min_weight)
        return [_module_edge(r) for r in rows]

    @strawberry.field
    def tables(
        self,
        info: strawberry.Info,
        shared: bool | None = None,
        module: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TableStats]:
        rows = repo.table_rows(
            _ctx(info).conn,
            shared=shared,
            module=module,
            limit=_limit(limit),
            offset=max(0, offset),
        )
        return [_table_stats(r) for r in rows]

    @strawberry.field
    def hotspots(
        self,
        info: strawberry.Info,
        label: str | None = None,
        module: str | None = None,
        limit: int = 50,
    ) -> list[Hotspot]:
        rows = repo.hotspot_rows(_ctx(info).conn, label=label, module=module, limit=_limit(limit))
        return [_hotspot(r) for r in rows]

    @described("Total de tabelas com os mesmos filtros de `tables` (para paginar).")
    def tables_total(
        self, info: strawberry.Info, shared: bool | None = None, module: str | None = None
    ) -> int:
        return repo.table_count(_ctx(info).conn, shared=shared, module=module)

    @strawberry.field
    def screens(
        self,
        info: strawberry.Info,
        in_menu: bool | None = None,
        module: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ScreenStats]:
        rows = repo.screen_rows(
            _ctx(info).conn,
            in_menu=in_menu,
            module=module,
            limit=_limit(limit),
            offset=max(0, offset),
        )
        return [_screen(r) for r in rows]

    @described("Texto de uma função da base. Nulo se a função não tem texto no grafo.")
    def routine_source(
        self, info: strawberry.Info, id: strawberry.ID, offset: int = 1, limit: int = 200
    ) -> RoutineSource | None:
        page = repo.routine_page(_ctx(info).conn, str(id), offset, limit)
        return RoutineSource.of(page) if page else None

    @described("Origem de uma função da base. Nulo sem a tabela ou sem a função.")
    def routine_origin(self, info: strawberry.Info, id: strawberry.ID) -> RoutineOrigin | None:
        data = routine_origin.read_origin(_ctx(info).conn, str(id))
        return RoutineOrigin.of(data) if data else None

    @described("O grafo servido tem a origem das funções (grafo escaneado com a origem).")
    def has_routine_origins(self, info: strawberry.Info) -> bool:
        return routine_origin.has_origins(_ctx(info).conn)

    @described("O grafo servido tem o texto das funções da base (escaneado com o banco).")
    def has_routine_texts(self, info: strawberry.Info) -> bool:
        return repo.has_routine_texts(_ctx(info).conn)

    @described("Pasta do e-cidade em uso.")
    def workspace(self, info: strawberry.Info) -> Workspace:
        return _workspace(_ctx(info))

    @described("Arquivo da pasta do e-cidade (só leitura).")
    def source_file(
        self, info: strawberry.Info, path: str, line: int | None = None, context: int = 30
    ) -> SourceFile:
        found = src.read_source(_ctx(info).workspace(), path, line, context)
        return SourceFile(
            path=found.path,
            total_lines=found.total_lines,
            start_line=found.start_line,
            lines=found.lines,
            highlight=found.highlight,
            truncated=found.truncated,
            size_bytes=found.size_bytes,
        )

    @described("Projetos cadastrados. Mesmo sem grafo, aparecem na lista.")
    def projects(self, info: strawberry.Info) -> list[Project]:
        ctx = _ctx(info)
        return [Project.of(p, ctx) for p in ctx.state.projects()]

    @described("O projeto ativo: o que o servidor serve. Nulo sem nenhum projeto.")
    def active_project(self, info: strawberry.Info) -> Project | None:
        ctx = _ctx(info)
        active = ctx.state.active_project()
        return Project.of(active, ctx) if active else None

    @described("Perfis disponíveis e o arquivo que cada um exige na raiz da pasta.")
    def project_profiles(self) -> list[ProfileOption]:
        return [ProfileOption(name=p.name, marker=p.required_marker) for p in PROFILES.values()]

    @described("Sugere o perfil de uma pasta pelos marcadores. A confirmação é do usuário.")
    def suggest_profile(self, source: str) -> ProfileSuggestion:
        found = projects.suggest(source)
        return ProfileSuggestion(
            exists=found.exists,
            profile=found.profile,
            markers=[ProfileMarker(profile=n, marker=m, found=f) for n, m, f in found.markers],
        )

    @described(
        "Superfície de comunicação: endpoints SOAP/RPC e rotas HTTP numa lista só, "
        "filtrada e paginada aqui. `protocol` aceita soap, rpc, http e externo."
    )
    def communication(
        self,
        info: strawberry.Info,
        system: str | None = None,
        protocol: str | None = None,
        direction: str | None = None,
        module: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> CommunicationPage:
        ctx = _ctx(info)
        report, reason = ctx.state.cross_report()
        page = comm.list_communication(
            ctx.conn,
            system=system or None,
            protocol=protocol or None,
            direction=direction or None,
            module=module or None,
            search=(search or "").strip() or None,
            limit=limit,
            offset=offset,
            cross=report,
        )
        return CommunicationPage(
            total=page.total,
            offset=page.offset,
            limit=page.limit,
            items=[_comm_row(r) for r in page.items],
            counters=CommunicationCounters(
                serves=page.counters.serves,
                calls=page.counters.calls,
                with_params=page.counters.with_params,
                orphans=page.counters.orphans,
                cross_available=page.counters.cross_available,
            ),
            systems=[CommunicationFacet(value=f.value, count=f.count) for f in page.systems],
            protocols=[CommunicationFacet(value=f.value, count=f.count) for f in page.protocols],
            modules=[CommunicationFacet(value=f.value, count=f.count) for f in page.modules],
            cross_note=None if report is not None else reason,
        )

    @described("Detalhe de um endpoint ou rota, com o outro lado da costura (cross).")
    def endpoint_detail(self, info: strawberry.Info, id: strawberry.ID) -> EndpointDetail:
        ctx = _ctx(info)
        report, _ = ctx.state.cross_report()
        active = ctx.state.active_project()
        found = comm.endpoint_detail(
            ctx.conn, str(id), local_project=active.slug if active else None, cross=report
        )
        return EndpointDetail(
            row=_comm_row(found.row),
            callers=[_comm_caller(c) for c in found.callers],
            attrs=JSON(found.attrs),
            counterpart=_comm_counterpart(found.counterpart),
        )

    @described("Scan pela interface: o atual ou o último. Consultado a cada 2 s.")
    def scan_job(self, info: strawberry.Info) -> "ScanJob | None":
        job = _ctx(info).state.jobs.current()
        return ScanJob.of(job) if job is not None else None

    @described("Valores iniciais do formulário de scan e o que está disponível.")
    def scan_defaults(self, info: strawberry.Info) -> "ScanDefaults":
        ctx = _ctx(info)
        saved = ctx.state.settings.load()
        project = ctx.state.active_project()
        profile = project.profile if project else config.DEFAULT_PROFILE
        dsn_name = (project.dsn_env if project else None) or config.CATALOG_DSN_ENV
        dsn = config.env_value(dsn_name) is not None  # só se existe; o valor nunca sai
        catalog = dsn and profile == config.DEFAULT_PROFILE  # só o e-cidade tem catálogo
        docker, docker_message = ctx.state.docker_status()
        saved_schema = "db" if project and project.schema_from_db else saved.schema_source
        default_source = (
            project.source
            if project
            else saved.source or (_workspace(ctx).source if ctx.db_path else None)
        )
        return ScanDefaults(
            source=default_source,
            schema=saved.schema,
            schema_source=_schema_source(saved_schema, saved.schema, dsn),
            schema_db_available=dsn,
            catalog_from_env=saved.catalog_from_env and catalog,
            catalog_available=catalog,
            project=project.slug if project else None,
            project_name=project.name if project else None,
            profile=profile,
            marker=PROFILES[profile].required_marker,
            dsn_env=dsn_name,
            reuse_ast=saved.reuse_ast,
            docker_available=docker,
            docker_message=docker_message,
            recent_sources=saved.recent_sources,
            read_only=ctx.state.read_only,
        )

    @described("Extração (cache de AST) desta pasta no `out` do servidor.")
    def ast_cache(self, info: strawberry.Info, source: str) -> "AstCache":
        folder = source.strip()
        if not folder:
            raise QueryError("informe a pasta do e-cidade")
        out_dir = _ctx(info).state.out_dir
        assert out_dir is not None
        found = ast_cache.cache_info(out_dir, Path(folder), Settings())
        return AstCache(
            exists=found.exists,
            reusable=found.reusable,
            files=found.files,
            updated_at=found.updated_at,
            reason=found.reason,
            message=_cache_message(found),
        )


def _schema_source(saved: str | None, paths: list[str], db_available: bool) -> str:
    """Fonte do schema no formulário: a salva, se ainda der para usar; senão o
    banco com o DSN, os arquivos salvos ou nenhuma."""
    if saved == "db" and not db_available:
        saved = None
    if saved in SCHEMA_SOURCES:
        return str(saved)
    if db_available:
        return "db"
    return "files" if paths else "none"


def _cache_message(found: ast_cache.CacheInfo) -> str:
    """O aviso do formulário de scan (texto do aviso de extração)."""
    if not found.exists:
        return "Sem extração desta pasta: o scan vai extrair tudo (6+ min, precisa de Docker)."
    if not found.reusable:
        return (
            "A extração desta pasta é de outra versão do extrator: o scan vai extrair tudo "
            "(6+ min, precisa de Docker)."
        )
    if found.files is None:
        return "Há extração desta pasta. Só os arquivos novos ou alterados voltam ao extrator."
    count = f"{found.files:,}".replace(",", ".")
    when = ""
    if found.updated_at:
        try:
            when = f" ({datetime.fromisoformat(found.updated_at):%d/%m/%Y %H:%M})"
        except ValueError:
            when = ""
    return (
        f"Há extração desta pasta: {count} arquivos{when}. "
        "Só os arquivos novos ou alterados voltam ao extrator."
    )


def _workspace(ctx: Context) -> Workspace:
    ws = ctx.workspace()
    return Workspace(
        source=ws.source,
        origin=ws.origin,
        exists=ws.exists,
        is_ecidade=ws.is_ecidade,
        graph_source=ws.graph_source,
        marker=ws.marker,
    )


# --------------------------------------------------------------------------- scan


@strawberry.input
class ScanInput:
    source: str | None = strawberry.field(
        default=None, description="Pasta a escanear; sem ela, a do projeto."
    )
    project: str | None = strawberry.field(
        default=None, description="Slug do projeto: perfil, nome e DSN vêm dele."
    )
    schema: list[str] = strawberry.field(default_factory=list)
    schema_source: str = strawberry.field(
        default="files",
        description="db (--schema-db, DSN do .env), files (os caminhos de `schema`) ou none.",
    )
    catalog_from_env: bool = True
    reuse_ast: bool = True
    search: bool = True
    analytics: bool = True


@strawberry.type
class ScanStage:
    stage: str
    label: str
    at: str


@strawberry.type
class ScanJob:
    id: strawberry.ID
    state: str = strawberry.field(description="running, succeeded, failed ou canceled.")
    stage: str | None
    stage_label: str | None
    stages: list[ScanStage]
    started_at: str
    finished_at: str | None
    elapsed_seconds: float
    log_tail: list[str]
    result: JSON | None = strawberry.field(description="O evento `done` do scan (resumo).")
    error: str | None
    project: str | None = strawberry.field(description="Slug do projeto escaneado.")

    @classmethod
    def of(cls, job: jobs.ScanJob) -> "ScanJob":
        return cls(
            id=strawberry.ID(job.id),
            state=job.state,
            stage=job.stage,
            stage_label=job.stage_label,
            stages=[ScanStage(stage=s.stage, label=s.label, at=s.at) for s in job.stages],
            started_at=job.started_at,
            finished_at=job.finished_at,
            elapsed_seconds=job.elapsed_seconds,
            log_tail=list(job.log_tail),
            result=JSON(job.result) if job.result is not None else None,
            error=job.error,
            project=job.slug,
        )


@strawberry.type
class ScanDefaults:
    source: str | None
    schema: list[str]
    schema_source: str = strawberry.field(description="db, files ou none.")
    schema_db_available: bool = strawberry.field(
        description="Há a variável do .env com a conexão do schema do banco. O valor nunca sai."
    )
    catalog_from_env: bool
    catalog_available: bool = strawberry.field(
        description="Há a variável do .env e o perfil lê catálogo. O valor nunca sai da API."
    )
    project: str | None = strawberry.field(description="Slug do projeto ativo.")
    project_name: str | None
    profile: str
    marker: str = strawberry.field(description="Arquivo que o perfil exige na raiz da pasta.")
    dsn_env: str = strawberry.field(description="NOME da variável do .env (nunca o valor).")
    reuse_ast: bool
    docker_available: bool
    docker_message: str | None
    recent_sources: list[str]
    read_only: bool


@strawberry.type
class AstCache:
    exists: bool
    reusable: bool
    files: int | None
    updated_at: str | None
    reason: str = strawberry.field(
        description="reusable, no_cache, schema_version ou extractor_hash."
    )
    message: str


REFUSED_ORIGIN = "operação recusada: origem não permitida"
READ_ONLY = "servidor em modo somente leitura"


def _guard(info: strawberry.Info) -> Context:
    """Mutações: `--read-only` desliga; senão, só `Origin` local com JSON.

    Um site qualquer aberto no navegador consegue mandar um POST "simples" (formulário ou
    `text/plain`) para o `localhost`; exigir `Origin` local e `application/json` recusa isso.
    """
    ctx = _ctx(info)
    if ctx.state.read_only:
        raise QueryError(READ_ONLY)
    request = ctx.request
    origin = request.headers.get("origin") if request is not None else None
    content_type = request.headers.get("content-type", "") if request is not None else ""
    if (
        origin not in ctx.state.allowed_origins
        or content_type.split(";")[0].strip().lower() != "application/json"
    ):
        raise QueryError(REFUSED_ORIGIN)
    return ctx


@strawberry.type
class Mutation(
    AccessMutations,
    TunnelMutations,
):
    @described("Valida a pasta do e-cidade e passa a usá-la (código-fonte e scan).")
    def set_source(self, info: strawberry.Info, path: str) -> Workspace:
        ctx = _guard(info)
        folder = validate_source(Path(path.strip()), hint="").resolve()
        ctx.state.settings.remember_source(str(folder))
        ctx.state.arg_source = None  # a escolha na interface vale por cima do `--source`
        return _workspace(ctx)

    @described("Inicia o scan num subprocesso (um por vez).")
    def start_scan(
        self, info: strawberry.Info, scan: Annotated[ScanInput, strawberry.argument(name="input")]
    ) -> ScanJob:
        ctx = _guard(info)
        if scan.schema_source not in SCHEMA_SOURCES:
            raise QueryError("schemaSource aceita db, files ou none")
        paths = (
            [p.strip() for p in scan.schema if p.strip()] if scan.schema_source == "files" else []
        )
        project = ctx.state.project(scan.project) if scan.project else None
        folder = (scan.source or "").strip() or (project.source if project else "")
        if not folder:
            raise QueryError("informe a pasta do projeto")
        request = jobs.ScanRequest(
            source=Path(folder),
            schema=tuple(Path(p) for p in paths),
            schema_from_db=scan.schema_source == "db",
            catalog_from_env=scan.catalog_from_env,
            reuse_ast=scan.reuse_ast,
            search=scan.search,
            analytics=scan.analytics,
            slug=project.slug if project else None,
            name=project.name if project else None,
            profile=project.profile if project else None,
            dsn_env=project.dsn_env if project else None,
        )
        job = ctx.state.jobs.start(request)
        if project is not None:  # o cadastro acompanha o que foi escaneado
            ctx.state.update_project(
                project.slug,
                source=str(Path(folder).resolve()),
                schema_from_db=scan.schema_source == "db",
            )
        ctx.state.arg_source = None  # a pasta do scan passa a ser a pasta em uso
        return ScanJob.of(job)

    @described(
        "Cadastra um projeto. A pasta é validada pelo marcador do perfil escolhido; "
        "`dsnEnv` é o NOME da variável do .env, nunca a conexão."
    )
    def add_project(
        self,
        info: strawberry.Info,
        project: Annotated[ProjectInput, strawberry.argument(name="input")],
    ) -> Project:
        ctx = _guard(info)
        created = ctx.state.add_project(
            name=project.name,
            profile=project.profile,
            source=project.source,
            slug=project.slug,
            schema_from_db=project.schema_from_db,
            dsn_env=project.dsn_env,
        )
        return Project.of(created, ctx)

    @described("Altera o cadastro de um projeto. Só os campos informados mudam.")
    def update_project(
        self,
        info: strawberry.Info,
        slug: str,
        project: Annotated[ProjectUpdate, strawberry.argument(name="input")],
    ) -> Project:
        ctx = _guard(info)
        changed = ctx.state.update_project(
            slug,
            name=project.name,
            profile=project.profile,
            source=project.source,
            schema_from_db=project.schema_from_db,
            dsn_env=project.dsn_env,
            clear_dsn_env=project.clear_dsn_env,
        )
        return Project.of(changed, ctx)

    @described("Tira um projeto da lista (os arquivos dele ficam em disco). Não remove o ativo.")
    def remove_project(self, info: strawberry.Info, slug: str) -> list[Project]:
        ctx = _guard(info)
        ctx.state.remove_project(slug)
        return [Project.of(p, ctx) for p in ctx.state.projects()]

    @described("Passa a servir o grafo do projeto (outro arquivo, nunca o mesmo trocado).")
    def set_active_project(self, info: strawberry.Info, slug: str) -> Project:
        ctx = _guard(info)
        active = ctx.state.set_active(slug)
        ctx.state.arg_source = None  # a pasta do `--source` era de outro projeto
        return Project.of(active, ctx)

    @described("Cancela o scan em andamento (processo e containers do job).")
    async def cancel_scan(self, info: strawberry.Info) -> ScanJob:
        ctx = _guard(info)
        # Em outra thread: encerrar a árvore e os containers leva alguns segundos.
        return ScanJob.of(await asyncio.to_thread(ctx.state.jobs.cancel))


schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    extensions=[lambda: QueryDepthLimiter(max_depth=MAX_QUERY_DEPTH), AdminOnlyMutations],
)
