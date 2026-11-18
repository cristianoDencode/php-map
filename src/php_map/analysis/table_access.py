"""Quem grava e quem lê a tabela no código PHP.

O dossiê de campo mostrava 1 tela quando o mapa conhece 5 fontes gravando em produção
(`protocolo.cgmtipoempresa`). O `climb_to_screen` lê só 3 escritores e 2 chamadores de cada
um, e só aceita chamador `File:`. Este módulo lista **todos**, na hora e por SQL sobre as arestas
do grafo (sem rescan e sem o `walk` do `impact`, que anda milhares de arquivos):

- **SQL direto** — o fonte tem aresta `INSERTS`/`UPDATES`/`DELETES`/`QUERIES` para a tabela e não é
  método de um DAO (`cl_*`);
- **DAO** — o fonte chama um método `cl_x::m` que toca a tabela. A aresta `CALLS` guarda
  `occurrences` e `sample_lines` (até 5 locais), somados por fonte;
- **Eloquent** — o fonte usa o model que tem `MAPS_TO` (`ELOQUENT_MODEL`) para a tabela. O mapa
  não diz se lê ou grava: a operação é «uso», nunca `INSERT`;
- **Migração** — caminho `db/migrations/` ou `database/migrations/` (Laravel). Fica à parte e
  **fora** da contagem de produção.

O menu de cada gravador vem do `IMPLEMENTS_ACTION` do arquivo; senão, da subida das camadas
(`layers.up_layers`, a mesma busca reversa dos «Menus por fonte»), limitada a `max_menu_climbs`
fontes por consulta. O que não foi tentado ou não achou vai dito em `menu_reason` e em `notes`.
"""

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from php_map.analysis import TABLE_OPS, layers
from php_map.errors import QueryError
from php_map.graph import field_writes as fw
from php_map.graph.model import file_id, key_of
from php_map.web.subgraph import batches, placeholders

HOW_SQL = "sql_direto"
HOW_DAO = "dao"
HOW_ELOQUENT = "eloquent"
USE = "uso"  # Eloquent: o mapa não diz se lê ou grava
MIGRATION_LAYER = "migracao"
# Ruckusing/Phinx (`db/migrations/`, 1.033 fontes no scan60) e Laravel (`database/migrations/`, 332)
MIGRATIONS_PREFIXES = ("db/migrations/", "database/migrations/")

DEFAULT_MAX_READERS = 15
MAX_READERS = 500
DEFAULT_MAX_MENU_CLIMBS = 60
MAX_LINES = 5  # `MAX_SAMPLE_LINES` do builder: o que a aresta guarda
MAX_MENUS = 3
MAX_WRITERS_SHOWN = 60  # linhas da tabela de gravadores no Markdown (o JSON traz todas)

_PHP_LABELS = frozenset({"File", "Method", "Function"})
_WRITE_TYPES = ("INSERTS", "UPDATES", "DELETES")
_ACCESS_TYPES = (*_WRITE_TYPES, "QUERIES")
_OP_ORDER = ("INSERT", "UPDATE", "DELETE", "SELECT", USE)
_LAYER_ORDER = {name: index for index, name in enumerate((*layers.LAYERS, MIGRATION_LAYER))}
_DAO_PREFIX = "cl_"
_HOW_TEXT = {HOW_SQL: "SQL direto", HOW_DAO: "DAO", HOW_ELOQUENT: "model Eloquent"}

NO_MENU = "nenhum menu chega neste fonte pelo mapa"
NO_CALLER = (
    "nenhum fonte chama este método pelo mapa (pode ser chamado por `$this->…` sem injeção "
    "resolvida)"
)
NO_INCLUDER = "nenhum fonte chama nem inclui este arquivo pelo mapa"
NO_CATALOG = "mapa sem --catalog-db (item sem caminho legível)"
NO_ITEMS = "mapa sem itens de menu (scan sem --catalog-db)"


@dataclass(frozen=True, slots=True)
class Spot:
    """Um local no fonte: `{path, line}`."""

    path: str | None
    line: int


@dataclass(frozen=True, slots=True)
class FieldFill:
    """Um campo que o código PHP preenche antes de gravar pelo DAO (`php_field_writes`).

    `how` é `atribuicao` (`$oDao->campo = expr`) ou `formulario` (o campo está no formulário da
    tela e o DAO o lê do `$_POST`; só vale se a atribuição não preencheu). `value` é o texto do
    código, não o valor resolvido. `spot` é onde o valor aparece; `method` é `incluir`/`alterar`."""

    column: str
    label: str | None
    how: str
    value: str
    value_kind: str
    spot: Spot
    confidence: float
    method: str = ""


@dataclass(frozen=True, slots=True)
class Toucher:
    """Um fonte PHP que toca a tabela, de um jeito (`how`). `confidence` é a maior entre as
    chamadas agrupadas (cada uma já é a menor entre a chamada e a do DAO com a tabela)."""

    node: str  # File: | Method: | Function:
    path: str | None
    symbol: str | None  # `Classe::método` ou a função; None no arquivo
    layer: str
    how: str
    operations: tuple[str, ...]
    dao_methods: tuple[str, ...]
    occurrences: int
    lines: tuple[Spot, ...]
    confidence: float
    menus: tuple[str, ...] = ()
    menu_reason: str | None = None
    # só nos gravadores pelo DAO; vazio também quando o mapa é antigo.
    fills: tuple[FieldFill, ...] = ()


@dataclass(frozen=True, slots=True)
class TableAccess:
    table_id: str
    writers: tuple[Toucher, ...]  # produção: SQL direto, DAO e model Eloquent
    migrations: tuple[Toucher, ...]  # fora da contagem de produção
    writers_total: int
    writers_by_how: dict[str, int]
    readers_total: int
    readers: tuple[Toucher, ...]  # os `max_readers` primeiros
    readers_by_layer: dict[str, int]
    unresolved_menu: int  # gravadores sem menu
    checked_menus: int  # gravadores com a subida até o menu tentada
    notes: tuple[str, ...] = ()
    # o mapa tem `php_field_writes` (falso = mapa anterior: refazer o scan) e quantas
    # linhas ele tem para esta tabela.
    fills_available: bool = False
    fills_total: int = 0


# --------------------------------------------------------------------------- arestas


@dataclass(frozen=True, slots=True)
class _Edge:
    src: str
    dst: str
    type: str
    source: str
    confidence: float
    evidence: dict[str, Any]
    attrs: dict[str, Any]

    def spots(self) -> list[Spot]:
        found = [
            Spot(s.get("path"), int(s["line"]))
            for s in self.attrs.get("sample_lines") or ()
            if isinstance(s, dict) and s.get("line") is not None
        ]
        if not found and self.evidence.get("line") is not None:
            found = [Spot(self.evidence.get("path"), int(self.evidence["line"]))]
        return found

    @property
    def occurrences(self) -> int:
        value = self.attrs.get("occurrences")
        return int(value) if isinstance(value, int) and value > 0 else 1


def _edges(
    conn: sqlite3.Connection, column: str, ids: Sequence[str], types: Sequence[str]
) -> list[_Edge]:
    """Arestas dos tipos `types` cuja ponta `column` (`dst` ou `src`) está em `ids`."""
    found: list[_Edge] = []
    for chunk in batches(list(dict.fromkeys(ids))):
        rows = conn.execute(
            "SELECT src, dst, type, source, confidence, evidence, attrs FROM edges"  # noqa: S608
            f" WHERE {column} IN ({placeholders(len(chunk))})"
            f" AND type IN ({placeholders(len(types))}) ORDER BY id",
            (*chunk, *types),
        )
        found += [
            _Edge(src, dst, kind, source, float(conf), json.loads(ev), json.loads(at))
            for src, dst, kind, source, conf, ev, at in rows
        ]
    return found


@dataclass(slots=True)
class _Acc:
    """Soma das arestas de um mesmo fonte e do mesmo jeito (`how`)."""

    node: str
    how: str
    path_hint: str | None
    operations: set[str] = field(default_factory=set)
    dao_methods: list[str] = field(default_factory=list)
    occurrences: int = 0
    lines: list[Spot] = field(default_factory=list)
    confidence: float = 0.0

    def add(self, operation: str, dao_method: str | None, edge: _Edge, confidence: float) -> None:
        self.operations.add(operation)
        if dao_method and dao_method not in self.dao_methods:
            self.dao_methods.append(dao_method)
        self.occurrences += edge.occurrences
        for spot in edge.spots():
            if spot not in self.lines and len(self.lines) < MAX_LINES:
                self.lines.append(spot)
        self.confidence = max(self.confidence, confidence)


class _Bucket:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], _Acc] = {}

    def add(
        self,
        node: str,
        how: str,
        operation: str,
        dao_method: str | None,
        edge: _Edge,
        confidence: float,
        path_hint: str | None,
    ) -> None:
        acc = self.items.get((node, how))
        if acc is None:
            acc = self.items[(node, how)] = _Acc(node, how, path_hint)
        acc.add(operation, dao_method, edge, confidence)

    def __len__(self) -> int:
        return len(self.items)


# --------------------------------------------------------------------------- nós


class _Nodes:
    """Atributos dos nós (uma consulta por lote)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.attrs: dict[str, dict[str, Any]] = {}

    def load(self, ids: Iterable[str]) -> None:
        wanted = [i for i in dict.fromkeys(ids) if i not in self.attrs]
        for chunk in batches(wanted):
            rows = self.conn.execute(
                f"SELECT id, attrs FROM nodes WHERE id IN ({placeholders(len(chunk))})",
                tuple(chunk),
            )
            for node_id, raw in rows:
                self.attrs[node_id] = json.loads(raw)

    def path_of(self, node_id: str, hint: str | None) -> str | None:
        if node_id.startswith("File:"):
            return node_id.partition(":")[2]
        declared = self.attrs.get(node_id, {}).get("path")
        return str(declared) if declared else hint

    def exists(self, node_id: str) -> bool:
        return node_id in self.attrs


def _is_dao_method(node_id: str) -> bool:
    return node_id.startswith("Method:") and key_of(node_id).startswith(_DAO_PREFIX)


def _is_migration(path: str | None) -> bool:
    return bool(path) and str(path).startswith(MIGRATIONS_PREFIXES)


def _is_dao_file(path: str | None) -> bool:
    return bool(path) and bool(layers.DAO_PATH.match(str(path)))


def _symbol(node_id: str, path: str | None, attrs: dict[str, Any]) -> str | None:
    """`Classe::método` (a classe com a caixa do arquivo quando o nome bate) ou a função."""
    if node_id.startswith("Method:"):
        key = key_of(node_id)
        cls = str(attrs.get("class") or key.partition("::")[0]).rpartition("\\")[2]
        stem = (path or "").rpartition("/")[2].partition(".")[0]
        if stem and stem.lower() == cls.lower():
            cls = stem
        return f"{cls}::{attrs.get('name') or key.rpartition('::')[2]}"
    if node_id.startswith("Function:"):
        return str(attrs.get("name") or key_of(node_id))
    return None


def _order_ops(operations: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(operations, key=lambda op: _OP_ORDER.index(op) if op in _OP_ORDER else 9))


# --------------------------------------------------------------------------- coleta


def _collect(
    conn: sqlite3.Connection, table_id: str
) -> tuple[_Bucket, _Bucket, _Bucket, list[str]]:
    """`(gravadores, migrações, leitores, avisos)` ainda sem camada nem menu."""
    writers, migrations, readers = _Bucket(), _Bucket(), _Bucket()
    notes: list[str] = []
    nodes = _Nodes(conn)

    def place(
        origin: str,
        how: str,
        operation: str,
        dao_method: str | None,
        edge: _Edge,
        confidence: float,
    ) -> None:
        path = nodes.path_of(origin, str(edge.evidence.get("path") or "") or None)
        bucket = writers if operation != "SELECT" else readers
        if _is_migration(path):
            if operation == "SELECT":
                return  # leitura em migração não é código de produção nem entra em nada
            bucket = migrations
        bucket.add(origin, how, operation, dao_method, edge, confidence, path)

    direct = _edges(conn, "dst", [table_id], _ACCESS_TYPES)
    nodes.load(e.src for e in direct)
    dao_edges: list[tuple[_Edge, str]] = []
    for edge in direct:
        if edge.src.partition(":")[0] not in _PHP_LABELS:
            continue  # PgFunction, Trigger…: não é código PHP
        operation = TABLE_OPS[edge.type]
        if _is_dao_method(edge.src):
            dao_edges.append((edge, operation))
            continue
        if _is_dao_file(nodes.path_of(edge.src, None)):
            continue  # a classe `classes/db_*_classe.php` só implementa
        place(edge.src, HOW_SQL, operation, None, edge, edge.confidence)

    dao_edges.sort(key=lambda item: (_OP_ORDER.index(item[1]), item[0].src))
    calls = _edges(conn, "dst", [e.src for e, _ in dao_edges], ("CALLS",))
    by_dao: dict[str, list[_Edge]] = {}
    for call in calls:
        by_dao.setdefault(call.dst, []).append(call)
    nodes.load(c.src for c in calls)
    for dao_edge, operation in dao_edges:
        dao_class, _sep, method = key_of(dao_edge.src).partition("::")
        for call in by_dao.get(dao_edge.src, ()):
            if call.src.partition(":")[0] not in _PHP_LABELS:
                continue
            if _is_dao_method(call.src) and key_of(call.src).startswith(f"{dao_class}::"):
                continue  # o DAO chamando a si mesmo
            if _is_dao_file(nodes.path_of(call.src, None)):
                continue
            place(
                call.src,
                HOW_DAO,
                operation,
                method,
                call,
                min(call.confidence, dao_edge.confidence),
            )

    _eloquent(conn, table_id, nodes, writers, readers, notes)
    return writers, migrations, readers, notes


def _eloquent(
    conn: sqlite3.Connection,
    table_id: str,
    nodes: _Nodes,
    writers: _Bucket,
    readers: _Bucket,
    notes: list[str],
) -> None:
    """Quem usa o model Eloquent da tabela: `uso`, em gravadores e em leitores."""
    models = {
        e.src: e.confidence
        for e in _edges(conn, "dst", [table_id], ("MAPS_TO",))
        if e.source == "ELOQUENT_MODEL"
    }
    if not models:
        return
    members = _edges(conn, "src", list(models), ("DECLARES",))
    owner_of = {m.dst: m.src for m in members if m.dst.startswith("Method:")}
    declaring = {e.src for e in _edges(conn, "dst", list(models), ("DECLARES",))}
    targets = [*models, *owner_of]
    for call in _edges(conn, "dst", targets, ("INSTANTIATES", "CALLS")):
        model = call.dst if call.dst in models else owner_of.get(call.dst)
        if model is None or call.src in declaring:
            continue
        if call.src.partition(":")[0] not in _PHP_LABELS:
            continue
        if call.src.startswith(f"Method:{key_of(model)}::"):
            continue  # o model chamando a si mesmo
        nodes.load([call.src])
        path = nodes.path_of(call.src, str(call.evidence.get("path") or "") or None)
        if _is_migration(path):
            continue
        confidence = min(call.confidence, models[model])
        for bucket in (writers, readers):
            bucket.add(call.src, HOW_ELOQUENT, USE, None, call, confidence, path)


# --------------------------------------------------------------------------- menu


def _direct_menus(
    conn: sqlite3.Connection, files: Sequence[str]
) -> dict[str, tuple[list[str], bool]]:
    """`File:` → (caminhos de menu, achou item de menu sem caminho legível)."""
    found: dict[str, tuple[list[str], bool]] = {}
    for chunk in batches(list(dict.fromkeys(files))):
        rows = conn.execute(
            "SELECT e.src, n.attrs FROM edges e JOIN nodes n ON n.id = e.dst"
            f" WHERE e.type = 'IMPLEMENTS_ACTION' AND e.src IN ({placeholders(len(chunk))})"
            " ORDER BY e.id",
            tuple(chunk),
        )
        for src, raw in rows:
            paths, _unnamed = found.setdefault(src, ([], False))
            attrs = json.loads(raw)
            menu_paths = attrs.get("menu_paths")
            names = (
                [str(p) for p in menu_paths]
                if isinstance(menu_paths, list) and menu_paths
                else ([str(attrs["menu_path"])] if attrs.get("menu_path") else [])
            )
            if not names:
                found[src] = (paths, True)
            for name in names:
                if name not in paths and len(paths) < MAX_MENUS:
                    paths.append(name)
    return found


def _has_callers(conn: sqlite3.Connection, nodes: Sequence[str]) -> set[str]:
    """Dos `nodes`, os que alguém aponta (qualquer aresta que chega, menos a que declara)."""
    pointed: set[str] = set()
    for chunk in batches(list(dict.fromkeys(nodes))):
        rows = conn.execute(
            f"SELECT DISTINCT dst FROM edges WHERE dst IN ({placeholders(len(chunk))})"
            " AND type NOT IN ('DECLARES', 'MAPS_TO', 'IMPLEMENTS_ACTION')",
            tuple(chunk),
        )
        pointed.update(r[0] for r in rows)
    return pointed


def _climb_node(node: str, path: str | None) -> str | None:
    """O nó de onde subir até o menu: `File`/`Method`; a função vira o arquivo dela."""
    if node.startswith(("File:", "Method:")):
        return node
    return file_id(path) if path else None


@dataclass(slots=True)
class _Row:
    """Um fonte pronto para a camada e o menu (mutável durante a montagem)."""

    acc: _Acc
    path: str | None
    symbol: str | None
    layer: str
    menus: list[str] = field(default_factory=list)
    reason: str | None = None
    tried: bool = False
    unnamed: bool = False  # achou item de menu sem caminho legível

    def toucher(self) -> Toucher:
        return Toucher(
            node=self.acc.node,
            path=self.path,
            symbol=self.symbol,
            layer=self.layer,
            how=self.acc.how,
            operations=_order_ops(self.acc.operations),
            dao_methods=tuple(self.acc.dao_methods),
            occurrences=self.acc.occurrences,
            lines=tuple(self.acc.lines),
            confidence=self.acc.confidence,
            menus=tuple(self.menus),
            menu_reason=self.reason if not self.menus else None,
        )


def _rows(conn: sqlite3.Connection, nodes: _Nodes, bucket: _Bucket) -> list[_Row]:
    accs = list(bucket.items.values())
    nodes.load(a.node for a in accs)
    layer_of = layers.layers_of(conn, list(dict.fromkeys(a.node for a in accs)))
    rows = []
    for acc in accs:
        path = nodes.path_of(acc.node, acc.path_hint)
        layer = MIGRATION_LAYER if _is_migration(path) else layer_of.get(acc.node, layers.OUTRO)
        rows.append(_Row(acc, path, _symbol(acc.node, path, nodes.attrs.get(acc.node, {})), layer))
    return rows


def _rank(row: _Row) -> tuple[int, int, str, str]:
    return (
        0 if row.menus else 1,
        _LAYER_ORDER.get(row.layer, len(_LAYER_ORDER)),
        row.path or "",
        row.symbol or "",
    )


def _direct_menu_pass(conn: sqlite3.Connection, nodes: _Nodes, rows: Sequence[_Row]) -> None:
    files = {row.acc.node: _climb_file(row) for row in rows}
    wanted = [f for f in files.values() if f]
    nodes.load(wanted)
    menus = _direct_menus(conn, [f for f in wanted if nodes.exists(f)])
    for row in rows:
        file = files[row.acc.node]
        paths, unnamed = menus.get(file or "", ([], False))
        row.menus = list(paths)
        row.unnamed = unnamed and not paths


def _climb_file(row: _Row) -> str | None:
    if row.acc.node.startswith("File:"):
        return row.acc.node
    return file_id(row.path) if row.path else None


def _climb(conn: sqlite3.Connection, rows: Sequence[_Row], budget: int) -> tuple[int, int]:
    """Sobe até o menu os `budget` primeiros sem menu. Devolve `(tentados, sem tentativa)`."""
    todo = [r for r in rows if not r.menus]
    todo.sort(key=_rank)
    tried = 0
    options = layers.LayerOptions()
    for row in todo:
        if tried >= budget:
            break
        start = _climb_node(row.acc.node, row.path)
        row.tried = True
        tried += 1
        if start is None:
            continue
        try:
            paths, _total, _cut = layers.up_layers(conn, start, options)
        except QueryError:
            continue
        for path in paths:
            if path.menu_path:
                if path.menu_path not in row.menus and len(row.menus) < MAX_MENUS:
                    row.menus.append(path.menu_path)
            elif not row.menus:
                row.unnamed = True
    return tried, len(todo) - tried


def _reasons(conn: sqlite3.Connection, nodes: _Nodes, rows: Sequence[_Row], budget: int) -> None:
    """Dá o motivo de cada fonte sem menu (nada de silêncio)."""
    missing = [r for r in rows if not r.menus]
    if not missing:
        return
    has_items = bool(
        conn.execute("SELECT 1 FROM nodes WHERE label = 'BusinessAction' LIMIT 1").fetchone()
    )
    probe: list[str] = []
    for row in missing:
        probe.append(row.acc.node)
        probe.append(_climb_file(row) or "")
        if row.acc.node.startswith("Method:"):
            cls = nodes.attrs.get(row.acc.node, {}).get("class")
            if cls:
                probe.append(f"Class:{cls}")
    pointed = _has_callers(conn, [p for p in probe if p])
    for row in missing:
        if not has_items:
            row.reason = NO_ITEMS
        elif row.unnamed:
            row.reason = NO_CATALOG
        elif not row.tried:
            row.reason = f"a subida até o menu não foi tentada (limite de {budget} por consulta)"
        else:
            related = [row.acc.node, _climb_file(row) or ""]
            if row.acc.node.startswith("Method:"):
                cls = nodes.attrs.get(row.acc.node, {}).get("class")
                related.append(f"Class:{cls}" if cls else "")
            if any(r in pointed for r in related if r):
                row.reason = NO_MENU
            else:
                row.reason = NO_CALLER if row.acc.node.startswith("Method:") else NO_INCLUDER


# --------------------------------------------------------------------------- preenche

_HOW_FILL_ORDER = {fw.HOW_ASSIGN: 0, fw.HOW_FORM: 1}
FILL_COLUMN_CAP = 6  # campos citados na coluna «Preenche» antes do «+N»


def _fills_by_source(
    rows: Sequence[fw.FieldWriteRow], labels: dict[str, str | None]
) -> dict[str, tuple[FieldFill, ...]]:
    """Linhas de `php_field_writes` agrupadas por quem chama o DAO (`Toucher.node`)."""
    grouped: dict[str, list[FieldFill]] = {}
    for row in rows:
        grouped.setdefault(row.src, []).append(
            FieldFill(
                column=row.column_name,
                label=labels.get(row.column_name),
                how=row.how,
                value=row.value,
                value_kind=row.value_kind,
                spot=Spot(row.path, row.line),
                confidence=row.confidence,
                method=key_of(row.dao_method).rpartition("::")[2],
            )
        )
    return {
        src: tuple(
            sorted(
                fills,
                key=lambda f: (_HOW_FILL_ORDER.get(f.how, 9), f.spot.line, f.column, f.method),
            )
        )
        for src, fills in grouped.items()
    }


def _column_labels(nodes: _Nodes, table_id: str) -> dict[str, str | None]:
    nodes.load([table_id])
    catalog = nodes.attrs.get(table_id, {}).get("columns_catalog") or {}
    return {
        str(column): (
            str(info.get("label")) if isinstance(info, dict) and info.get("label") else None
        )
        for column, info in catalog.items()
    }


# --------------------------------------------------------------------------- entrada


def table_access(
    conn: sqlite3.Connection,
    table_id: str,
    *,
    max_readers: int = DEFAULT_MAX_READERS,
    max_menu_climbs: int = DEFAULT_MAX_MENU_CLIMBS,
) -> TableAccess:
    """Todo o código PHP que grava e lê `table_id` (um `Table:schema.tabela` que exista).

    `max_readers` (até 500) corta a lista de leitores; a contagem e `readers_by_layer` são sempre
    completas. `max_menu_climbs` limita as subidas até o menu (as caras) nos gravadores sem menu
    direto e nos leitores mostrados; o que ficou de fora vai em `notes` e em `menu_reason`."""
    exists = conn.execute("SELECT 1 FROM nodes WHERE id = ? AND label = 'Table'", (table_id,))
    if exists.fetchone() is None:
        raise QueryError(f"tabela não encontrada: {key_of(table_id)}")
    max_readers = max(0, min(max_readers, MAX_READERS))
    budget = max(0, max_menu_climbs)
    writers_b, migrations_b, readers_b, notes = _collect(conn, table_id)
    nodes = _Nodes(conn)
    writer_rows = _rows(conn, nodes, writers_b)
    migration_rows = _rows(conn, nodes, migrations_b)
    reader_rows = _rows(conn, nodes, readers_b)

    _direct_menu_pass(conn, nodes, writer_rows)
    _direct_menu_pass(conn, nodes, reader_rows)
    reader_rows.sort(
        key=lambda r: (0 if r.menus else 1, -r.acc.confidence, r.path or "", r.symbol or "")
    )
    shown_rows = reader_rows[:max_readers]

    tried, left = _climb(conn, writer_rows, budget)
    reader_tried, reader_left = _climb(conn, shown_rows, max(0, budget - tried))
    if left:
        notes.append(
            f"{left} gravador(es) sem menu direto ficaram sem a subida até o menu "
            f"(limite de {budget} subidas por consulta; aumente `max_menu_climbs`)"
        )
    if reader_left:
        notes.append(
            f"{reader_left} leitor(es) mostrado(s) sem menu direto ficaram sem a subida até o menu"
        )
    _reasons(conn, nodes, writer_rows, budget)
    _reasons(conn, nodes, shown_rows, budget)
    shown_rows.sort(
        key=lambda r: (0 if r.menus else 1, -r.acc.confidence, r.path or "", r.symbol or "")
    )
    writer_rows.sort(key=_rank)
    migration_rows.sort(key=lambda r: (r.path or "", r.symbol or ""))

    by_how = Counter(r.acc.how for r in writer_rows)
    fill_rows = fw.field_writes(conn, table_id)
    fills = _fills_by_source(fill_rows or [], _column_labels(nodes, table_id))

    def with_fills(rows: Sequence[_Row]) -> tuple[Toucher, ...]:
        found = (r.toucher() for r in rows)
        return tuple(
            replace(t, fills=fills.get(t.node, ())) if t.how == HOW_DAO else t for t in found
        )

    if not (writer_rows or migration_rows or reader_rows):
        notes.append("nenhum fonte PHP grava ou lê esta tabela pelo mapa")
    if len(reader_rows) > len(shown_rows):
        notes.append(
            f"{len(reader_rows) - len(shown_rows)} leitor(es) ficaram de fora da lista "
            f"(mostrados: {len(shown_rows)}; use --leitores N, até {MAX_READERS})"
        )
    return TableAccess(
        table_id=table_id,
        writers=with_fills(writer_rows),
        migrations=tuple(r.toucher() for r in migration_rows),
        writers_total=len(writer_rows),
        writers_by_how=dict(sorted(by_how.items())),
        readers_total=len(reader_rows),
        readers=tuple(r.toucher() for r in shown_rows),
        readers_by_layer=dict(
            sorted(
                Counter(r.layer for r in reader_rows).items(),
                key=lambda item: _LAYER_ORDER.get(item[0], 99),
            )
        ),
        unresolved_menu=sum(1 for r in writer_rows if not r.menus),
        checked_menus=tried,
        notes=tuple(notes),
        fills_available=fill_rows is not None,
        fills_total=len(fill_rows or ()),
    )


# --------------------------------------------------------------------------- saída


def table_access_json(access: TableAccess) -> dict[str, Any]:
    """Todos os campos; `lines` como lista de `{path, line}`."""
    return dict(json.loads(json.dumps(asdict(access), ensure_ascii=False)))


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _source(t: Toucher) -> str:
    where = f"`{t.path}`" if t.path else f"`{key_of(t.node)}`"
    return f"{where} `{t.symbol}`" if t.symbol else where


def _how(t: Toucher) -> str:
    text = _HOW_TEXT.get(t.how, t.how)
    return f"{text} `{'`, `'.join(t.dao_methods)}`" if t.dao_methods else text


def _lines(t: Toucher) -> str:
    if not t.lines:
        return "—"
    shown = ", ".join(
        str(s.line) if s.path in (None, t.path) else f"{s.path}:{s.line}" for s in t.lines
    )
    if t.occurrences > len(t.lines):
        return f"{shown} +{t.occurrences - len(t.lines)} chamadas"
    return f"{shown} ({t.occurrences} chamadas)" if t.occurrences > 1 else shown


def _menu(t: Toucher) -> str:
    if t.menus:
        return "; ".join(t.menus)
    return f"— (sem menu: {t.menu_reason})" if t.menu_reason else "—"


def _ops(t: Toucher) -> str:
    return (
        "uso do model (o mapa não diz se lê ou grava)"
        if t.how == HOW_ELOQUENT
        else ", ".join(t.operations)
    )


def _fill_cell(t: Toucher) -> str:
    """Coluna «Preenche»: até 6 campos; o literal sai com o valor (`z03_tipoempresa = 32`)."""
    if not t.fills:
        return "—"
    named: dict[str, str] = {}
    for fill in t.fills:
        literal = fill.value_kind == "literal"
        if fill.column not in named or (literal and named[fill.column] == fill.column):
            named[fill.column] = f"{fill.column} = {fill.value}" if literal else fill.column
    items = list(named.values())
    text = ", ".join(f"`{item}`" for item in items[:FILL_COLUMN_CAP])
    return text + (f" +{len(items) - FILL_COLUMN_CAP}" if len(items) > FILL_COLUMN_CAP else "")


def _table(items: Sequence[Toucher], *, operation: bool, fills: bool = False) -> list[str]:
    head = "| Fonte | Camada | Como |" + (" Operação |" if operation else "")
    head += (" Preenche |" if fills else "") + " Linhas | Menu |"
    rule = "|---|---|---|" + ("---|" if operation else "") + ("---|" if fills else "") + "---|---|"
    lines = [head, rule]
    for t in items:
        cells = [_source(t), t.layer, _how(t)]
        if operation:
            cells.append(_ops(t))
        if fills:
            cells.append(_fill_cell(t))
        cells += [_lines(t), _menu(t)]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return lines


def _summary(a: TableAccess) -> str:
    parts = []
    for how, text in ((HOW_DAO, "pelo DAO"), (HOW_SQL, "por SQL direto")):
        if a.writers_by_how.get(how):
            parts.append(f"{a.writers_by_how[how]} {text}")
    if a.writers_by_how.get(HOW_ELOQUENT):
        parts.append(f"{a.writers_by_how[HOW_ELOQUENT]} usam o model Eloquent")
    gravam = f"{a.writers_total} fonte(s) gravam em produção" + (
        f" ({', '.join(parts)})" if parts else ""
    )
    extra = (
        f"; {len(a.migrations)} migração(ões) gravam à parte, fora da contagem"
        if a.migrations
        else ""
    )
    return f"{gravam}{extra}; {a.readers_total} fonte(s) leem."


def render_table_access(
    access: TableAccess,
    *,
    heading: str = "##",
    max_writers: int | None = MAX_WRITERS_SHOWN,
    max_words: int | None = None,
    full: bool = False,
) -> str:
    """A seção «Quem grava e quem lê no código PHP» em Markdown. `max_writers` corta a tabela de
    gravadores (o JSON traz todos); `None` mostra todos. Com `max_words`, tira primeiro linhas de
    leitores (até 3) e depois de gravadores (até 5) até caber, dizendo quantas ficaram de fora.
    Com `full=True`: mostra todos os gravadores e leitores, sem cortar por
    `max_words` nem por `max_writers`/`MAX_WRITERS_SHOWN` — quem pagina o dossiê inteiro é
    `report/paging.py`."""
    if full:
        max_writers = None
        max_words = None
    writers_cap = len(access.writers) if max_writers is None else max_writers
    readers_cap = len(access.readers)
    text = _render(access, heading, writers_cap, readers_cap)
    while max_words is not None and len(text.split()) > max_words:
        if readers_cap > 3:
            readers_cap = max(3, readers_cap // 2)
        elif min(writers_cap, len(access.writers)) > 5:
            writers_cap = max(5, min(writers_cap, len(access.writers)) // 2)
        else:
            break
        text = _render(access, heading, writers_cap, readers_cap)
    return text


OLD_MAP = "O mapa é antigo: refaça o scan para ver quem preenche cada campo."
FILL_EXPLANATION = (
    "A coluna «Preenche» mostra os campos que o código atribui antes de `incluir`/`alterar` "
    "(o valor é o texto do código, não o valor resolvido) e os do formulário da tela, que o DAO "
    "lê do `$_POST` e só valem quando a atribuição não preencheu."
)
NO_FILLS = (
    "Nenhum fonte atribui campos antes de gravar pelo DAO nem os tem no formulário: o valor pode "
    "vir do `$_POST` de outro jeito, de um padrão do banco ou de um `alterar` que não o muda."
)


def _fills_note(access: TableAccess) -> str | None:
    """A frase sobre «coluna por coluna»: só o mapa antigo a mantém."""
    if not access.fills_available:
        return (
            "A gravação pelo DAO grava a tabela inteira: o mapa não diz coluna por coluna. "
            + OLD_MAP
        )
    if access.fills_total:
        return FILL_EXPLANATION
    if any(w.how == HOW_DAO for w in access.writers):
        return NO_FILLS
    return None


def _render(access: TableAccess, heading: str, writers_cap: int, readers_cap: int) -> str:
    out = [f"{heading} Quem grava e quem lê no código PHP", ""]
    if not (access.writers or access.migrations or access.readers_total):
        out += ["Nenhum fonte PHP grava ou lê esta tabela pelo mapa.", ""]
        return "\n".join(out)
    sub = heading + "#"
    out.append(_summary(access))
    note = _fills_note(access)
    if note:
        out.append(note)
    out += ["", f"{sub} Quem grava", ""]
    if access.writers:
        shown = access.writers[:writers_cap]
        out += _table(shown, operation=True, fills=access.fills_total > 0)
        if len(shown) < len(access.writers):
            out += [
                "",
                f"_e mais {len(access.writers) - len(shown)} fonte(s) que gravam "
                "(a lista completa está no JSON)_",
            ]
    else:
        out.append("Nenhum fonte PHP grava esta tabela pelo mapa.")
    if access.migrations:
        out += ["", f"{sub} Migrações (fora da contagem)", ""]
        out += _table(access.migrations, operation=True)
    out += ["", f"{sub} Quem lê", ""]
    if access.readers_total:
        layers_text = ", ".join(f"{n} {name}" for name, n in access.readers_by_layer.items())
        shown_readers = access.readers[:readers_cap]
        out.append(
            f"{access.readers_total} fonte(s): {layers_text}. "
            f"Os {len(shown_readers)} principais (telas com menu primeiro)"
            + (
                f" (cortado: {len(shown_readers)} de {access.readers_total}):"
                if len(shown_readers) < access.readers_total
                else ":"
            )
        )
        out.append("")
        out += _table(shown_readers, operation=False)
    else:
        out.append("Nenhum fonte PHP lê esta tabela pelo mapa.")
    if access.notes:
        out += ["", *(f"- _{note}_" for note in access.notes)]
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- um campo

_KIND_TEXT = {
    "literal": "valor fixo",
    "null": "nulo",
    "variavel": "variável",
    "parametro": "parâmetro",
    "chamada": "chamada",
    "expressao": "expressão",
    "formulario": "campo do formulário",
}
_HOW_FILL_TEXT = {fw.HOW_ASSIGN: "atribuição no código", fw.HOW_FORM: "pelo formulário da tela"}
_FIXED = ("literal", "null")
MAX_FILL_ROWS = 15  # linhas da seção do campo no Markdown (o JSON traz todas)


def field_fills(
    access: TableAccess, column: str
) -> tuple[tuple[Toucher, tuple[FieldFill, ...]], ...]:
    """Dos gravadores pelo DAO, só as preenchidas de `column`: `(fonte, preenchidas)`."""
    name = column.lower()
    found = []
    for toucher in access.writers:
        mine = tuple(f for f in toucher.fills if f.column == name)
        if mine:
            found.append((toucher, mine))
    return tuple(found)


def fill_summary(
    fills: Sequence[tuple[Toucher, tuple[FieldFill, ...]]], access: TableAccess
) -> str:
    """«N fontes preenchem este campo: X por atribuição (Y com valor fixo), Z pelo formulário».
    Sem linha nenhuma, mas com gravador pelo DAO, diz o que isso quer dizer."""
    if not access.fills_available:
        return OLD_MAP
    if not fills:
        if any(w.how == HOW_DAO for w in access.writers):
            return (
                "Nenhum fonte atribui este campo nem o tem no formulário: o valor pode vir do "
                "`$_POST` de outro jeito, de um padrão do banco ou de um `alterar` que não o muda."
            )
        return "Nenhum fonte grava esta tabela pelo DAO pelo mapa."
    assigned = [(t, f) for t, items in fills for f in items if f.how == fw.HOW_ASSIGN]
    formed = {t.node for t, items in fills for f in items if f.how == fw.HOW_FORM}
    fixed = {t.node for t, f in assigned if f.value_kind in _FIXED}
    by_assignment = {t.node for t, _f in assigned}
    return (
        f"{len(fills)} fonte(s) preenchem este campo: {len(by_assignment)} por atribuição "
        f"({len(fixed)} com valor fixo), {len(formed)} pelo formulário da tela."
    )


def _fill_rank(item: tuple[Toucher, FieldFill]) -> tuple[int, int, float, str, int]:
    toucher, fill = item
    return (
        0 if fill.value_kind in _FIXED else 1,
        0 if fill.how == fw.HOW_ASSIGN else 1,
        -fill.confidence,
        toucher.path or "",
        fill.spot.line,
    )


def render_field_fills(
    access: TableAccess,
    column: str,
    *,
    heading: str = "##",
    max_rows: int | None = MAX_FILL_ROWS,
) -> str:
    """A seção «Quem preenche este campo no código PHP». Uma linha por (fonte, valor); o valor
    fixo vem antes, depois a atribuição e por fim o formulário, cada grupo pela confiança.
    `max_rows=None` (usado com `render_field_dossier(..., full=True)`) mostra todas."""
    fills = field_fills(access, column)
    out = [f"{heading} Quem preenche este campo no código PHP", "", fill_summary(fills, access)]
    if not fills:
        out.append("")
        return "\n".join(out)
    out += [
        "O valor é o texto do código, não o valor resolvido; o formulário só vale quando a "
        "atribuição não preencheu.",
        "",
    ]
    rows: dict[tuple[str, str, str, str], tuple[Toucher, FieldFill, list[Spot]]] = {}
    for toucher, items in fills:
        for fill in items:
            key = (toucher.node, fill.how, fill.value, fill.method)
            if key in rows:
                rows[key][2].append(fill.spot)
            else:
                rows[key] = (toucher, fill, [fill.spot])
    ordered = sorted(rows.values(), key=lambda r: _fill_rank((r[0], r[1])))
    out += ["| Fonte | Como | Valor | Tipo | Linha | Menu |", "|---|---|---|---|---|---|"]
    for toucher, fill, spots in ordered[:max_rows]:
        how = _HOW_FILL_TEXT.get(fill.how, fill.how)
        if fill.how == fw.HOW_ASSIGN and fill.method:
            how += f" antes de `{fill.method}`"
        where = ", ".join(
            str(s.line) if s.path in (None, toucher.path) else f"{s.path}:{s.line}" for s in spots
        )
        cells = [
            _source(toucher),
            how,
            f"`{fill.value}`",
            _KIND_TEXT.get(fill.value_kind, fill.value_kind),
            where,
            _menu(toucher),
        ]
        out.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    if max_rows is not None and len(ordered) > max_rows:
        out += ["", f"_e mais {len(ordered) - max_rows} linha(s) (a lista completa está no JSON)_"]
    out.append("")
    return "\n".join(out)


__all__ = [
    "FieldFill",
    "DEFAULT_MAX_MENU_CLIMBS",
    "DEFAULT_MAX_READERS",
    "HOW_DAO",
    "HOW_ELOQUENT",
    "HOW_SQL",
    "MAX_READERS",
    "MIGRATIONS_PREFIXES",
    "MIGRATION_LAYER",
    "Spot",
    "TableAccess",
    "Toucher",
    "field_fills",
    "fill_summary",
    "render_field_fills",
    "render_table_access",
    "table_access",
    "table_access_json",
]
