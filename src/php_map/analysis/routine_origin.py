"""Origem das funções da base: por onde cada `PgFunction` é usada.

Para cada função, uma BFS **reversa** (de quem é chamado para quem chama) até o menu, a rota, o
endpoint ou o arquivo PHP que chega nela, com o caminho (`via`) e a distância. Quando não há
origem, a função recebe um `status` e um `reason`: nunca um campo vazio. 100% de origem **não é
alcançável** (SQL dinâmico, JavaScript e agendamentos não aparecem no grafo); a meta é a
classificação completa.

Subida (de `node` para quem o alcança):
- `CALLS_RPC` (arquivo, função, método ou outra `PgFunction`), `TRIGGERS` (da tabela) e as
  arestas de código de `TRAVERSED` (`CALLS`, `INSTANTIATES`, `INCLUDES`, `REQUIRES`,
  `REFERENCES`); também `ROUTES_TO` (do controller para a rota) e `CALLS_HTTP`
  (da rota para o JS ou a tela que a chama), então a subida atravessa a rota até a tela e o menu;
- de uma `Table`, só quem **escreve** (`INSERTS`, `UPDATES`, `DELETES`): ler não dispara trigger;
- nunca por nó `is_infra`; `DECLARES` só liga o símbolo ao arquivo que o declara (um símbolo
  declarado em vários arquivos, como as funções homônimas das telas, não vira origem por
  homônimo: a origem vem de quem **chama**) e o método à sua classe (mesma distância).

Pontos de origem, com a distância (1 = chamador direto):
`menu` (`IMPLEMENTS_ACTION`), `route` (`ROUTES_TO`; continua a subida), `endpoint` (`SERVES`) e
`file`. A confiança
da origem é a **menor** das arestas do caminho. Módulo: voto dos arquivos de entrada, com peso
`1 / (1 + distância)`; sem módulos no grafo, nulo.

Menção por texto (hipótese, confiança 0,5): para a função sem origem, arquivos cujos literais ou
comentários citam o nome dela. Não é aresta, não entra no acoplamento.
"""

import json
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

DEFAULT_DEPTH = 8
PER_KIND = 5
MAX_WALK_NODES = 20_000  # teto da subida por função; acima disso, `truncated`
MENTION_FILES = 5
MENTION_MIN_CHARS = 6
MENTION_CONFIDENCE = 0.5
MENTION_SOURCE = "LITERAL_MENTION"
GENERATED_PREFIX = "fc_auditoria_tabela_"

CLIMB_TYPES = frozenset(
    {
        "CALLS_RPC",
        "TRIGGERS",
        "CALLS",
        "INSTANTIATES",
        "INCLUDES",
        "REQUIRES",
        "REFERENCES",
        "ROUTES_TO",  # método -> rota (a rota continua sendo ponto de origem)
        "CALLS_HTTP",  # rota -> JS/tela
    }
)
WRITE_TYPES = frozenset({"INSERTS", "UPDATES", "DELETES"})
SYMBOL_LABELS = frozenset({"Function", "Method", "Class"})
KINDS = ("menu", "route", "endpoint", "file")

COM_ORIGEM = "com_origem"
SO_ARQUIVO = "so_arquivo"
GERADA = "gerada"
SEM_CHAMADOR = "sem_chamador"
CADEIA_SEM_ORIGEM = "cadeia_sem_origem"
STATUSES = (COM_ORIGEM, SO_ARQUIVO, GERADA, SEM_CHAMADOR, CADEIA_SEM_ORIGEM)

REASONS = {
    SEM_CHAMADOR: (
        "ninguém a chama no código mapeado; pode ser usada por SQL dinâmico, JavaScript "
        "ou agendamento"
    ),
    CADEIA_SEM_ORIGEM: (
        "tem chamadores, mas a subida não chega a nenhum arquivo PHP mapeado "
        "(as chamadas vêm de outras funções que ninguém chama, ou de infraestrutura)"
    ),
    SO_ARQUIVO: "chega a arquivos PHP, mas nenhum está no menu, numa rota ou num endpoint",
}

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

NodeRecord = tuple[str, str, Mapping[str, Any]]  # (id, label, attrs)
EdgeRecord = tuple[str, str, str, float]  # (src, dst, tipo, confiança)


@dataclass(frozen=True, slots=True)
class OriginEntry:
    routine_id: str
    kind: str  # menu | route | endpoint | file
    target: str
    label: str
    distance: int
    via: tuple[str, ...]  # da origem até a função
    confidence: float
    file: str | None = None  # arquivo da entrada (para o módulo)


@dataclass(frozen=True, slots=True)
class RoutineRow:
    routine_id: str
    status: str
    reason: str
    module: str | None
    module_confidence: float | None
    generated: bool
    callers_php: int
    callers_routines: int
    triggers: int
    entries: int


@dataclass(frozen=True, slots=True)
class Mention:
    routine_id: str
    file_id: str
    line: int
    source: str = MENTION_SOURCE
    confidence: float = MENTION_CONFIDENCE


@dataclass(slots=True)
class RoutineOrigins:
    routines: tuple[RoutineRow, ...] = ()
    entries: tuple[OriginEntry, ...] = ()
    truncated: int = 0  # funções cuja subida bateu no teto de nós
    seconds: float = 0.0
    mentions: tuple[Mention, ...] = field(default=())

    def summary(self) -> dict[str, Any]:
        counts = Counter(row.status for row in self.routines)
        return {
            "total": len(self.routines),
            **{status: counts.get(status, 0) for status in STATUSES},
            "with_module": sum(1 for row in self.routines if row.module),
            "mentions": len({m.routine_id for m in self.mentions}),
            "truncated": self.truncated,
            "seconds": self.seconds,
        }


# --------------------------------------------------------------------------- cálculo


@dataclass(slots=True)
class _Index:
    labels: dict[str, str] = field(default_factory=dict)
    attrs: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    infra: set[str] = field(default_factory=set)
    up: dict[str, list[tuple[str, str, float]]] = field(default_factory=dict)
    # chamadores de função (`CALLS_RPC`, `TRIGGERS`) de qualquer confiança, só para contar
    callers: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    actions: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    routes: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    serves: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    declarers: dict[str, set[str]] = field(default_factory=dict)  # símbolo → arquivos
    class_of: dict[str, str] = field(default_factory=dict)  # método → classe
    modules: dict[str, list[str]] = field(default_factory=dict)  # arquivo → módulos


def _index(
    nodes: Iterable[NodeRecord], edges: Iterable[EdgeRecord], min_confidence: float
) -> _Index:
    ix = _Index()
    for node_id, label, attrs in nodes:
        ix.labels[node_id] = label
        if label in ("PgFunction", "BusinessAction", "Route", "Endpoint"):
            ix.attrs[node_id] = attrs
        if attrs.get("is_infra"):
            ix.infra.add(node_id)
    for src, dst, kind, confidence in edges:
        if kind == "DECLARES":
            if ix.labels.get(src) == "File":
                ix.declarers.setdefault(dst, set()).add(src)
            elif ix.labels.get(dst) == "Method":
                ix.class_of[dst] = src
            continue
        if kind == "BELONGS_TO":
            if ix.labels.get(src) == "File":
                ix.modules.setdefault(src, []).append(dst)
            continue
        if kind in ("CALLS_RPC", "TRIGGERS"):
            ix.callers.setdefault(dst, []).append((src, kind))
        if confidence < min_confidence:
            continue
        if kind in CLIMB_TYPES or (kind in WRITE_TYPES and ix.labels.get(dst) == "Table"):
            ix.up.setdefault(dst, []).append((src, kind, confidence))
        if kind == "IMPLEMENTS_ACTION":
            ix.actions.setdefault(src, []).append((dst, confidence))
        elif kind == "ROUTES_TO":
            ix.routes.setdefault(dst, []).append((src, confidence))  # método → rotas
        elif kind == "SERVES":
            ix.serves.setdefault(src, []).append((dst, confidence))
    for items in ix.up.values():
        items.sort()
    return ix


def _files_of(ix: _Index, node: str) -> list[str]:
    """Arquivo(s) do nó alcançado: ele mesmo, ou o arquivo que **o único** declarante do símbolo
    (o homônimo declarado em vários arquivos não vira origem)."""
    label = ix.labels.get(node)
    if label == "File":
        return [] if node in ix.infra else [node]
    if label not in SYMBOL_LABELS:
        return []
    symbol = ix.class_of.get(node, node)
    declared = ix.declarers.get(symbol, set())
    if len(declared) != 1:
        return []
    (file,) = declared
    return [] if file in ix.infra else [file]


def _name(ix: _Index, node: str) -> str:
    attrs = ix.attrs.get(node, {})
    for key in ("menu_path", "name", "uri"):
        value = attrs.get(key)
        if isinstance(value, str) and value:
            return value
    return node.partition(":")[2]


def _callers(ix: _Index, routine: str) -> tuple[int, int, int]:
    """`(PHP, outras funções, triggers)`: quantos nós chamam a função, sem filtro de confiança
    (é a contagem que o `routine` do MCP já mostra)."""
    php: set[str] = set()
    functions: set[str] = set()
    triggers: set[str] = set()
    for src, kind in ix.callers.get(routine, ()):
        label = ix.labels.get(src)
        if kind == "TRIGGERS":
            triggers.add(src)
        elif kind == "CALLS_RPC" and label == "PgFunction":
            functions.add(src)
        elif kind == "CALLS_RPC" and label in ("File", "Function", "Method"):
            php.add(src)
    return len(php), len(functions), len(triggers)


def _climb(ix: _Index, routine: str, depth: int, per_kind: int) -> tuple[list[OriginEntry], bool]:
    """Subida de uma função: as entradas (até `per_kind` de cada tipo) e se bateu no teto."""
    parent: dict[str, tuple[str | None, float]] = {routine: (None, 1.0)}
    candidates: dict[str, dict[str, OriginEntry]] = {kind: {} for kind in KINDS}
    full: set[str] = (
        set()
    )  # tipos já completos com níveis anteriores: o resto só estaria mais longe
    frontier = [routine]
    truncated = False
    for level in range(1, depth + 1):
        discovered: list[str] = []
        for node in frontier:
            node_confidence = parent[node][1]
            for src, _kind, confidence in ix.up.get(node, ()):
                if src in parent or src in ix.infra:
                    continue
                if len(parent) >= MAX_WALK_NODES:
                    truncated = True
                    break
                parent[src] = (node, min(node_confidence, confidence))
                discovered.append(src)
            if truncated:
                break
        for node in list(discovered):
            owner = ix.class_of.get(node)
            if owner is not None and owner not in parent and owner not in ix.infra:
                parent[owner] = (node, parent[node][1])  # método → classe, mesma distância
                discovered.append(owner)
        for node in discovered:
            _collect(ix, routine, node, level, parent, candidates, full)
        full.update(kind for kind in KINDS if len(candidates[kind]) >= per_kind)
        frontier = discovered
        if truncated or not frontier or len(full) == len(KINDS):
            break
    entries: list[OriginEntry] = []
    for kind in KINDS:
        found = sorted(candidates[kind].values(), key=lambda e: (e.distance, e.target))
        entries.extend(found[:per_kind])
    return entries, truncated


def _chain(parent: dict[str, tuple[str | None, float]], node: str) -> tuple[str, ...]:
    chain = [node]
    while (previous := parent[chain[-1]][0]) is not None:
        chain.append(previous)
    return tuple(chain)  # da origem até a função


def _collect(
    ix: _Index,
    routine: str,
    node: str,
    level: int,
    parent: dict[str, tuple[str | None, float]],
    candidates: dict[str, dict[str, OriginEntry]],
    full: set[str],
) -> None:
    """Pontos de origem em `node` (e no arquivo que o declara), na distância `level`."""
    files = _files_of(ix, node)
    own_file = ix.labels.get(node) == "File" and node not in ix.infra
    holders = (node, *(f for f in files if f != node))
    wants = [
        ("menu", ix.actions),
        ("route", ix.routes),
        ("endpoint", ix.serves),
    ]
    found = [
        (kind, h, source)
        for kind, source in wants
        if kind not in full
        for h in holders
        if h in source
    ]
    add_file = "file" not in full and bool(files or own_file)
    if not found and not add_file:
        return
    confidence = parent[node][1]
    chain = _chain(parent, node)
    entry_file = files[0] if files else (node if own_file else None)

    def via_of(holder: str) -> tuple[str, ...]:
        return chain if holder == node else (holder, *chain)

    def add(kind: str, target: str, label: str, via: tuple[str, ...], conf: float) -> None:
        if target not in candidates[kind]:
            candidates[kind][target] = OriginEntry(
                routine, kind, target, label, level, via, conf, entry_file
            )

    for kind, holder, source in found:
        for target, edge_confidence in source[holder]:
            add(kind, target, _name(ix, target), via_of(holder), min(confidence, edge_confidence))
    if add_file:
        for file in files or [node]:
            add("file", file, file.partition(":")[2], via_of(file), confidence)


def _module_vote(ix: _Index, entries: Sequence[OriginEntry]) -> tuple[str | None, float | None]:
    """Módulo dos arquivos de entrada, com peso `1 / (1 + distância)` por arquivo."""
    nearest: dict[str, int] = {}
    for entry in entries:
        if entry.file is not None and (
            entry.file not in nearest or entry.distance < nearest[entry.file]
        ):
            nearest[entry.file] = entry.distance
    weights: dict[str, float] = {}
    for file, distance in nearest.items():
        modules = sorted(set(ix.modules.get(file, ())))
        for module in modules:
            weights[module] = weights.get(module, 0.0) + 1.0 / ((1 + distance) * len(modules))
    if not weights:
        return None, None
    total = sum(weights.values())
    winner, weight = sorted(weights.items(), key=lambda item: (-item[1], item[0]))[0]
    return winner, round(weight / total, 4)


def _status(
    ix: _Index, routine: str, entries: Sequence[OriginEntry], callers: tuple[int, int, int]
) -> tuple[str, str]:
    name = str(ix.attrs.get(routine, {}).get("name") or routine.partition(":")[2])
    if name.startswith(GENERATED_PREFIX):
        tables = sorted({src for src, kind in ix.callers.get(routine, ()) if kind == "TRIGGERS"})
        if tables:
            shown = tables[0].partition(":")[2]
            more = f" (e mais {len(tables) - 1})" if len(tables) > 1 else ""
            return GERADA, f"auditoria da tabela {shown}{more}"
        return GERADA, "função gerada pela auditoria, sem tabela ligada por trigger"
    counts = Counter(entry.kind for entry in entries)
    if counts["menu"] or counts["route"] or counts["endpoint"]:
        parts = [
            f"{counts[k]} {label}"
            for k, label in (("menu", "menu"), ("route", "rota"), ("endpoint", "endpoint"))
            if counts[k]
        ]
        return COM_ORIGEM, "chega a " + ", ".join(parts) + " (os mais próximos)"
    if counts["file"]:
        return SO_ARQUIVO, REASONS[SO_ARQUIVO]
    if sum(callers) == 0:
        return SEM_CHAMADOR, REASONS[SEM_CHAMADOR]
    return CADEIA_SEM_ORIGEM, REASONS[CADEIA_SEM_ORIGEM]


def compute_origins(
    nodes: Iterable[NodeRecord],
    edges: Iterable[EdgeRecord],
    *,
    min_confidence: float = 0.5,
    max_depth: int = DEFAULT_DEPTH,
    per_kind: int = PER_KIND,
) -> RoutineOrigins:
    """Origem de cada `PgFunction` real (não placeholder) do grafo."""
    started = time.perf_counter()
    node_list = list(nodes)
    ix = _index(node_list, edges, min_confidence)
    routines = sorted(
        node_id
        for node_id, label, attrs in node_list
        if label == "PgFunction" and attrs.get("resolved") is not False
    )
    rows: list[RoutineRow] = []
    all_entries: list[OriginEntry] = []
    truncated = 0
    for routine in routines:
        entries, cut = _climb(ix, routine, max_depth, per_kind)
        truncated += int(cut)
        callers = _callers(ix, routine)
        status, reason = _status(ix, routine, entries, callers)
        module, confidence = _module_vote(ix, entries)
        name = str(ix.attrs.get(routine, {}).get("name") or "")
        rows.append(
            RoutineRow(
                routine, status, reason, module, confidence, name.startswith(GENERATED_PREFIX),
                callers[0], callers[1], callers[2], len(entries),
            )
        )  # fmt: skip
        all_entries.extend(entries)
    return RoutineOrigins(
        tuple(rows), tuple(all_entries), truncated, round(time.perf_counter() - started, 2)
    )


# --------------------------------------------------------------------------- menções


def find_mentions(
    conn: sqlite3.Connection,
    origins: RoutineOrigins,
    builtin_names: frozenset[str] = frozenset(),
) -> tuple[Mention, ...]:
    """Menções por texto, só para as funções sem origem (`sem_chamador`, `cadeia_sem_origem`).

    Uma passada sobre `search_texts` de **arquivos** (literais e comentários): o nome da função
    (sem o schema) como palavra inteira, sem diferenciar maiúsculas. Nomes com menos de 6
    caracteres e funções nativas do banco são ignorados. Sem a tabela, não há menção.
    """
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'search_texts'"
    ).fetchone()
    if found is None:
        return ()
    wanted: dict[str, list[str]] = {}  # nome → funções com esse nome
    for row in origins.routines:
        if row.status not in (SEM_CHAMADOR, CADEIA_SEM_ORIGEM):
            continue
        key = row.routine_id.partition(":")[2].partition("(")[0].rpartition(".")[2].lower()
        if len(key) >= MENTION_MIN_CHARS and key not in builtin_names:
            wanted.setdefault(key, []).append(row.routine_id)
    hits: dict[str, dict[str, int]] = {}  # função → arquivo → 1ª linha
    for file_id, text, line in conn.execute(
        "SELECT file_id, text, line FROM search_texts "
        "WHERE file_id LIKE 'File:%' AND kind IN ('literal', 'comment') ORDER BY file_id, line"
    ):
        for word in {w.lower() for w in _WORD.findall(text or "")}:
            for routine in wanted.get(word, ()):
                files = hits.setdefault(routine, {})
                if len(files) < MENTION_FILES and file_id not in files:
                    files[file_id] = int(line or 0)
    return tuple(
        Mention(routine, file_id, line)
        for routine in sorted(hits)
        for file_id, line in hits[routine].items()
    )


# --------------------------------------------------------------------------- leitura


def has_origins(conn: sqlite3.Connection) -> bool:
    """O banco tem a origem das funções (grafos antigos não têm)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'analytics_routines'"
    ).fetchone()
    return row is not None


def _entry_dict(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "kind": row[0],
        "target": row[1],
        "label": row[2],
        "distance": row[3],
        "via": json.loads(row[4]),
        "confidence": row[5],
    }


def read_origin(conn: sqlite3.Connection, routine_id: str) -> dict[str, Any] | None:
    """Status, motivo, módulo, entradas e menções de uma função; `None` sem a tabela ou sem a
    função. A menção é **hipótese** (confiança 0,5): marcada em `hypothesis`."""
    if not has_origins(conn):
        return None
    row = conn.execute(
        "SELECT status, reason, module, module_confidence, generated, callers_php, "
        "callers_routines, triggers FROM analytics_routines WHERE routine_id = ?",
        (routine_id,),
    ).fetchone()
    if row is None:
        return None
    order = "CASE kind " + " ".join(f"WHEN '{k}' THEN {i}" for i, k in enumerate(KINDS)) + " END"
    entries = conn.execute(
        "SELECT kind, target, label, distance, via, confidence FROM analytics_routine_entries "
        f"WHERE routine_id = ? ORDER BY {order}, distance, target",
        (routine_id,),
    ).fetchall()
    mentions = conn.execute(
        "SELECT file_id, line, source, confidence FROM analytics_routine_mentions "
        "WHERE routine_id = ? ORDER BY file_id",
        (routine_id,),
    ).fetchall()
    return {
        "status": row[0],
        "reason": row[1],
        "module": row[2],
        "module_confidence": row[3],
        "generated": bool(row[4]),
        "callers": {"php": row[5], "functions": row[6], "triggers": row[7]},
        "entries": [_entry_dict(e) for e in entries],
        "mentions": [
            {
                "file": m[0],
                "line": m[1],
                "source": m[2],
                "confidence": m[3],
                "hypothesis": True,
            }
            for m in mentions
        ],
    }


def list_origins(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    module: str | None = None,
    generated: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any] | None:
    """Relatório de origem: contagem por status (com os filtros de módulo e geradas, sem o de
    status) e a lista paginada. `None` se o banco não tem a tabela."""
    if not has_origins(conn):
        return None
    where: list[str] = ["1 = 1"]
    params: list[Any] = []
    if module:
        where.append("module = ?")
        params.append(module)
    if generated is not None:
        where.append("generated = ?")
        params.append(int(generated))
    scope = " AND ".join(where)
    counts = dict(
        conn.execute(
            f"SELECT status, count(*) FROM analytics_routines WHERE {scope} GROUP BY status",
            params,
        ).fetchall()
    )
    page_where, page_params = scope, list(params)
    if status:
        page_where += " AND status = ?"
        page_params.append(status)
    total = int(
        conn.execute(
            f"SELECT count(*) FROM analytics_routines WHERE {page_where}", page_params
        ).fetchone()[0]
    )
    rows = conn.execute(
        "SELECT routine_id, status, reason, module, module_confidence, generated, callers_php, "
        "callers_routines, triggers, entries FROM analytics_routines "
        f"WHERE {page_where} ORDER BY routine_id LIMIT ? OFFSET ?",
        [*page_params, limit, offset],
    ).fetchall()
    items = []
    for r in rows:
        shown = conn.execute(
            "SELECT kind, label, distance FROM analytics_routine_entries WHERE routine_id = ? "
            "AND kind != 'file' ORDER BY distance, kind, target LIMIT 3",
            (r[0],),
        ).fetchall()
        items.append(
            {
                "id": r[0],
                "status": r[1],
                "reason": r[2],
                "module": r[3],
                "module_confidence": r[4],
                "generated": bool(r[5]),
                "callers": {"php": r[6], "functions": r[7], "triggers": r[8]},
                "entries": r[9],
                "nearest": [{"kind": k, "label": label, "distance": d} for k, label, d in shown],
            }
        )
    return {
        "by_status": {s: int(counts.get(s, 0)) for s in STATUSES},
        "total": total,
        "items": items,
        "truncated": offset + len(items) < total,
    }
