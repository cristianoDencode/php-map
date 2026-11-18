"""Camadas de um fonte: do menu à tabela.

Para um fonte (`File`, `Class` ou `Method`), mostra:

- **para cima** — quem chega nele até o menu: `menu → tela → JS → rota → controller → (fonte)`.
  É a busca reversa dos "Menus por fonte" (`web/menus.py`), por isso os caminhos, as distâncias
  e os nomes de menu são os mesmos; só vêm em camadas e com a confiança do caminho;
- **para baixo** — até onde ele vai: `(fonte) → service → repository → model → tabela`. É a BFS
  de `TRAVERSED` a partir do fonte e do que ele declara. Quando a busca chega a uma classe
  (`INSTANTIATES`), ela entra pelo `__construct`, que é o que roda de fato (a classe inteira
  traria métodos que ninguém chamou).

A **camada** é um rótulo de apresentação deduzido do nome e do tipo do nó (`layer_of`); não é
aresta e não entra no grafo. A interface avisa disso. Um nó `is_infra` não é percorrido.
"""

import json
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from php_map.errors import QueryError
from php_map.web import menus
from php_map.web.subgraph import batches, edges_of, node_info, placeholders

MENU = "menu"
TELA = "tela"
JS = "js"
ROTA = "rota"
CONTROLLER = "controller"
SERVICE = "service"
REPOSITORY = "repository"
MODEL = "model"
DAO = "dao"
TABELA = "tabela"
OUTRO = "outro"
# Ordem de apresentação, do alto (menu) ao fundo (tabela).
LAYERS = (MENU, TELA, JS, ROTA, CONTROLLER, SERVICE, REPOSITORY, MODEL, DAO, TABELA, OUTRO)

DEFAULT_MIN_CONFIDENCE = 0.5
MAX_UP_DEPTH = 10
MAX_UP_PATHS = 50
MAX_DOWN_DEPTH = 8
MAX_DOWN_NODES = 500
DOWN_WALK_NODES = 5_000  # teto da BFS; acima disso, `down_truncated`
# Pastas da raiz do e-cidade que não têm telas (DAO, bibliotecas, configuração).
NOT_SCREEN_DIRS = frozenset({"classes", "libs", "lib", "model", "std", "config", "vendor"})
# DAO legado: `classes/db_<tabela>_classe.php` (a classe `cl_<tabela>`).
DAO_PATH = re.compile(r"^classes/db_.+_classe\.php$", re.IGNORECASE)
# `evidence.via` da aresta → como o fonte obteve o DAO.
DAO_FACTORIES = {"dao_factory:getdao": "getDao", "dao_factory:usardao": "usarDao"}
CODE_LABELS = ("File", "Class", "Method", "Function")
LEAF_LABELS = frozenset({"Table", "PgFunction", "RpcCall"})


@dataclass(frozen=True, slots=True)
class LayerOptions:
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    max_up_depth: int = MAX_UP_DEPTH
    max_up_paths: int = MAX_UP_PATHS
    max_down_depth: int = MAX_DOWN_DEPTH
    max_down_nodes: int = MAX_DOWN_NODES
    max_down_walk: int = DOWN_WALK_NODES


@dataclass(frozen=True, slots=True)
class LayerStep:
    """Um nó de um caminho. `edge_type`/`confidence` são da aresta (real, no sentido do
    fluxo) entre o passo anterior e este; o primeiro passo não tem."""

    node: str
    label: str
    layer: str
    name: str
    edge_type: str | None = None
    confidence: float | None = None
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class LayerPath:
    """Do menu até o fonte. `steps` vai do menu ao último nó antes do fonte (os passos de
    `DECLARES`, que só ligam o símbolo ao arquivo, não entram)."""

    menu: str
    menu_path: str | None
    distance: int
    confidence: float
    steps: list[LayerStep]


@dataclass(frozen=True, slots=True)
class DaoMember:
    """Um método de um DAO que o fonte chama."""

    name: str
    node: str
    edge_type: str
    confidence: float


@dataclass(frozen=True, slots=True)
class DownNode:
    """Um nó alcançado para baixo. `confidence` é a menor do caminho até ele.

    Na camada `dao` o item é a **classe** (`cl_x`): `members` traz os métodos chamados e
    `factory` como o fonte a obteve (`getDao`/`usarDao`)."""

    node: str
    label: str
    layer: str
    name: str
    depth: int
    parent: str
    edge_type: str
    confidence: float
    path: str | None = None
    line: int | None = None
    members: list[DaoMember] = field(default_factory=list)
    factory: str | None = None


@dataclass(frozen=True, slots=True)
class Layers:
    node: str
    label: str
    layer: str
    name: str
    min_confidence: float
    up: list[LayerPath]
    up_total: int  # caminhos achados; `up` traz os `max_up_paths` primeiros
    up_truncated: bool  # a busca bateu no teto de nós
    down: list[DownNode]
    down_total: int
    down_truncated: bool
    down_by_layer: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def to_json_text(self) -> str:
        return json.dumps(self.to_json(), ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- camada de um nó


def _tail(text: str) -> str:
    return text.rpartition("\\")[2]


def _by_name(name: str, path: str) -> str | None:
    """Controller, service ou repository pelo nome da classe ou pela pasta do arquivo."""
    low, where = name.lower(), "/" + path.lower()
    for layer, suffix, folders in (
        (CONTROLLER, "controller", ("/controller/", "/controllers/")),
        (SERVICE, "service", ("/service/", "/services/")),
        (REPOSITORY, "repository", ("/repository/", "/repositories/")),
    ):
        if _tail(low).endswith(suffix) or any(f in where for f in folders):
            return layer
    return None


def layer_of(label: str, node_id: str, attrs: dict[str, Any], *, has_maps_to: bool = False) -> str:
    """Camada de apresentação, por ordem de regra: `menu`, `tabela`, `rota`, `dao`, `js`,
    `tela`, `model`, `controller`, `service`, `repository`, senão `outro`.

    `has_maps_to`: a classe (ou a classe do método) tem `MAPS_TO` para uma tabela."""
    if label == "BusinessAction":
        return MENU
    if label == "Table":
        return TABELA
    if label == "Route":
        return ROTA
    if label not in CODE_LABELS:
        return OUTRO
    path = str(attrs.get("path") or (node_id.partition(":")[2] if label == "File" else ""))
    declared = attrs.get("declared_in") if label == "Class" else None
    if DAO_PATH.match(path) or any(
        isinstance(p, str) and DAO_PATH.match(p) for p in declared or ()
    ):  # antes de `tela`/`NOT_SCREEN_DIRS`, que mandam `classes/` a `outro`
        return DAO
    if label == "File" and attrs.get("kind") == "js":
        return JS
    in_app = path.startswith(("app/", "src/"))
    if label == "File" and path.lower().endswith(".php") and not in_app:
        first = path.split("/", 1)[0] if "/" in path else ""
        return OUTRO if first in NOT_SCREEN_DIRS else TELA
    if has_maps_to:
        return MODEL
    name = str(attrs.get("class") or attrs.get("name") or node_id.partition(":")[2])
    if label == "File":
        name = path.rpartition("/")[2].removesuffix(".php")
    return _by_name(name, path) or OUTRO


def _display_name(label: str, node_id: str, attrs: dict[str, Any]) -> str:
    key = node_id.partition(":")[2]
    if label == "BusinessAction":
        return str(attrs.get("menu_path") or attrs.get("name") or key)
    if label == "Method":
        cls = _tail(str(attrs.get("class") or key.partition("::")[0]))
        return f"{cls}::{attrs.get('name') or key.rpartition('::')[2]}"
    if label == "Class":
        return _tail(key)
    if label == "Route":
        return key
    return key


def _class_of(label: str, node_id: str, attrs: dict[str, Any]) -> str | None:
    if label == "Class":
        return node_id
    if label == "Method":
        cls = attrs.get("class") or node_id.partition(":")[2].partition("::")[0]
        return f"Class:{cls}" if cls else None
    return None


class _Nodes:
    """Atributos e camada dos nós, com cache (uma consulta por lote)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.attrs: dict[str, dict[str, Any]] = {}
        self.labels: dict[str, str] = {}
        self.mapped: set[str] = set()
        self._seen_maps: set[str] = set()

    def _fetch(self, wanted: list[str]) -> None:
        for chunk in batches(wanted):
            rows = self.conn.execute(
                f"SELECT id, label, attrs FROM nodes WHERE id IN ({placeholders(len(chunk))})",
                tuple(chunk),
            )
            for node_id, label, raw in rows:
                self.labels[node_id] = label
                self.attrs[node_id] = json.loads(raw)

    def load(self, ids: list[str]) -> None:
        self._fetch([i for i in dict.fromkeys(ids) if i not in self.attrs])
        # A classe do método diz se ele é de um DAO (ver `layer`).
        owners = [
            c
            for i in ids
            if self.labels.get(i) == "Method" and (c := _class_of("Method", i, self.attrs[i]))
        ]
        self._fetch([c for c in dict.fromkeys(owners) if c not in self.attrs])
        classes = [
            c for i in ids if i in self.attrs and (c := _class_of(self.labels[i], i, self.attrs[i]))
        ]
        fresh = [c for c in dict.fromkeys(classes) if c not in self._seen_maps]
        for chunk in batches(fresh):
            rows = self.conn.execute(
                "SELECT DISTINCT src FROM edges WHERE type = 'MAPS_TO'"
                f" AND src IN ({placeholders(len(chunk))})",
                tuple(chunk),
            )
            self.mapped.update(r[0] for r in rows)
            self._seen_maps.update(chunk)

    def layer(self, node_id: str) -> str:
        label, attrs = self.labels.get(node_id), self.attrs.get(node_id, {})
        if label is None:
            return OUTRO
        owner = _class_of(label, node_id, attrs)
        found = layer_of(label, node_id, attrs, has_maps_to=owner in self.mapped)
        # `cl_x` declarada em vários arquivos (`db_x_classe.php` e cópias): o método pode ter o
        # caminho de uma cópia, mas a classe é um DAO.
        if (
            found != DAO
            and label == "Method"
            and owner in self.attrs
            and layer_of("Class", owner, self.attrs[owner]) == DAO
        ):
            return DAO
        return found

    def name(self, node_id: str) -> str:
        label = self.labels.get(node_id, node_id.partition(":")[0])
        return _display_name(label, node_id, self.attrs.get(node_id, {}))

    def where(self, node_id: str) -> tuple[str | None, int | None]:
        attrs = self.attrs.get(node_id, {})
        path = attrs.get("path")
        return (str(path) if path else None), attrs.get("line_start") or attrs.get("line")


# --------------------------------------------------------------------------- resolução


def resolve_target(
    conn: sqlite3.Connection, text: str, workspace_root: str | None = None
) -> menus.FileMatch:
    """`File`, `Class` ou `Method` a partir do que o usuário digitou: o id exato de uma
    `Class:`/`Method:`, ou o arquivo (caminho, nome ou trecho, como em "Menus por fonte")."""
    wanted = text.strip()
    if wanted.partition(":")[0] in ("Class", "Method"):
        found = conn.execute(
            "SELECT id FROM nodes WHERE lower(id) = ?", (wanted.lower(),)
        ).fetchone()
        return menus.FileMatch(found[0] if found else None)
    return menus.resolve_file(conn, wanted, workspace_root=workspace_root)


# --------------------------------------------------------------------------- para cima


def _menu_confidences(conn: sqlite3.Connection, entries: list[str]) -> dict[tuple[str, str], float]:
    """Confiança do `IMPLEMENTS_ACTION` de cada (tela de entrada, menu)."""
    found: dict[tuple[str, str], float] = {}
    for chunk in batches(list(dict.fromkeys(entries))):
        rows = conn.execute(
            "SELECT src, dst, max(confidence) FROM edges WHERE type = 'IMPLEMENTS_ACTION'"
            f" AND src IN ({placeholders(len(chunk))}) GROUP BY src, dst",
            tuple(chunk),
        )
        found.update({(src, dst): float(conf) for src, dst, conf in rows})
    return found


def _up(
    conn: sqlite3.Connection, nodes: _Nodes, node: str, options: LayerOptions
) -> tuple[list[LayerPath], int, bool]:
    found = menus.menus_from_node(
        conn,
        node,
        max_distance=options.max_up_depth,
        min_confidence=options.min_confidence,
    )
    menu_confidence = _menu_confidences(conn, [hit.entry for hit in found.items])
    paths: list[LayerPath] = []
    for hit in found.items:
        real = [step for step in hit.via if not step.incoming]
        if real and real[-1].node == node:  # o próprio fonte é a entrada do menu
            real = real[:-1]
        nodes.load([hit.menu, *(s.node for s in real)])
        steps = [
            LayerStep(
                hit.menu,
                "BusinessAction",
                MENU,
                hit.paths[0] if hit.paths else (hit.name or hit.menu),
            )
        ]
        confidence = menu_confidence.get((hit.entry, hit.menu), 1.0)
        for index, step in enumerate(real):
            edge_type = "IMPLEMENTS_ACTION" if index == 0 else step.edge_type
            edge_confidence = confidence if index == 0 else step.confidence
            steps.append(
                LayerStep(
                    step.node,
                    step.label,
                    nodes.layer(step.node),
                    nodes.name(step.node),
                    edge_type,
                    edge_confidence,
                    step.path or nodes.where(step.node)[0],
                    step.line or nodes.where(step.node)[1],
                )
            )
            if index and step.confidence is not None:
                confidence = min(confidence, step.confidence)
        paths.append(
            LayerPath(
                hit.menu, hit.paths[0] if hit.paths else None, hit.distance, confidence, steps
            )
        )
    paths.sort(key=lambda p: (p.distance, -p.confidence, p.menu_path or "", p.menu))
    return paths[: options.max_up_paths], len(paths), found.truncated


# --------------------------------------------------------------------------- para baixo


def _constructors(conn: sqlite3.Connection, classes: list[str]) -> dict[str, str]:
    """Classe → seu `__construct` (se a classe declara um)."""
    found: dict[str, str] = {}
    for chunk in batches(classes):
        rows = conn.execute(
            "SELECT e.src, e.dst FROM edges e WHERE e.type = 'DECLARES'"
            f" AND e.src IN ({placeholders(len(chunk))}) AND e.dst LIKE 'Method:%::__construct'",
            tuple(chunk),
        )
        found.update(rows)
    return found


def _factory(via: str | None) -> str | None:
    return DAO_FACTORIES.get((via or "").lower())


@dataclass(slots=True)
class _Unit:
    """Um item da descida: um nó, ou (camada `dao`) a classe com seus métodos."""

    node: str
    members: list[str]  # nós da BFS que formam o item (só o próprio nó, fora de `dao`)
    parent: str = ""
    edge_type: str = ""
    confidence: float = 1.0
    depth: int = 0
    factory: str | None = None


def _dao_units(
    nodes: _Nodes,
    order: list[str],
    layer: dict[str, str],
    seen: dict[str, tuple[str, str, float, int, str | None]],
) -> dict[str, _Unit]:
    """Agrupa os nós de `dao` por classe: `{id do item: unidade}`. Os campos da unidade são os
    do nó mais raso do grupo (o `__construct` que a BFS acrescenta não conta)."""
    groups: dict[str, list[str]] = {}
    for target in order:
        if layer[target] == DAO:
            key = _class_of(nodes.labels.get(target, ""), target, nodes.attrs.get(target, {}))
            groups.setdefault(key or target, []).append(target)
    units: dict[str, _Unit] = {}
    for key, found in groups.items():
        real = [t for t in found if not (seen[t][1] == "DECLARES" and seen[t][4] is None)]
        ranked = sorted(real or found, key=lambda t: (seen[t][3], nodes.labels.get(t) != "Class"))
        first = seen[ranked[0]]
        units[key] = _Unit(
            key,
            found,
            first[0],
            first[1],
            first[2],
            first[3],
            next((f for t in ranked if (f := _factory(seen[t][4]))), None),
        )
    return units


def _members(
    nodes: _Nodes, unit: _Unit, seen: dict[str, tuple[str, str, float, int, str | None]]
) -> list[DaoMember]:
    found = [
        DaoMember(
            str(nodes.attrs.get(t, {}).get("name") or t.rpartition("::")[2]),
            t,
            seen[t][1],
            seen[t][2],
        )
        for t in unit.members
        if nodes.labels.get(t) == "Method" and nodes.attrs.get(t, {}).get("name") != "__construct"
    ]
    return sorted(found, key=lambda m: (m.name, m.node))


def _down(
    conn: sqlite3.Connection, nodes: _Nodes, node: str, options: LayerOptions
) -> tuple[list[DownNode], int, bool, dict[str, int]]:
    declared = menus.declared_closure(conn, node)
    roots = [node, *declared]
    infra = {n for n, info in node_info(conn, roots).items() if info.is_infra}
    # alvo → (pai, tipo da aresta, confiança do caminho, profundidade, `evidence.via`)
    seen: dict[str, tuple[str, str, float, int, str | None]] = {
        r: ("", "", 1.0, 0, None) for r in roots
    }
    order: list[str] = []
    frontier = [r for r in roots if r not in infra]
    level, truncated = 0, False
    while frontier and level < options.max_down_depth and not truncated:
        edges = edges_of(conn, frontier, False, options.min_confidence)
        info = node_info(conn, {n for rows in edges.values() for n, *_ in rows if n not in seen})
        next_frontier: list[str] = []
        for current in frontier:
            for target, edge_type, confidence, evidence, _id in edges.get(current, ()):
                if target in seen or target not in info or info[target].is_infra:
                    continue
                if len(order) >= options.max_down_walk:
                    truncated = True
                    break
                path_confidence = min(seen[current][2], confidence)
                via = evidence.get("via")
                seen[target] = (
                    current,
                    edge_type,
                    path_confidence,
                    level + 1,
                    via if isinstance(via, str) else None,
                )
                order.append(target)
                if info[target].label not in LEAF_LABELS:
                    next_frontier.append(target)
            if truncated:
                break
        # Uma classe instanciada entra pelo construtor, na mesma profundidade.
        classes = [t for t in next_frontier if info[t].label == "Class"]
        for cls, constructor in _constructors(conn, classes).items():
            if constructor in seen or len(order) >= options.max_down_walk:
                continue
            constructor_info = node_info(conn, [constructor]).get(constructor)
            if constructor_info is None or constructor_info.is_infra:
                continue
            seen[constructor] = (cls, "DECLARES", seen[cls][2], seen[cls][3], None)
            order.append(constructor)
            next_frontier.append(constructor)
        frontier = next_frontier
        level += 1
    nodes.load(order)
    layer = {target: nodes.layer(target) for target in order}
    # Um DAO é um item só (a classe), com os métodos chamados dentro.
    daos = _dao_units(nodes, order, layer, seen)
    nodes.load(list(daos))
    rep = {member: key for key, unit in daos.items() for member in unit.members}
    units: dict[str, _Unit] = dict(daos)
    for target in order:
        if target not in rep:
            parent, edge_type, confidence, depth, _via = seen[target]
            units[target] = _Unit(target, [target], parent, edge_type, confidence, depth)
    for unit in units.values():
        unit.parent = rep.get(unit.parent, unit.parent)
    unit_layer = {key: (DAO if key in daos else layer[key]) for key in units}
    counts = Counter(unit_layer.values())
    # Mostra no máximo `max_down_nodes`: as camadas conhecidas primeiro (e o que as liga ao
    # fonte), depois o `outro`, cada grupo pela profundidade. Os totais contam tudo o que a
    # busca achou.
    ranked = sorted(units, key=lambda t: (unit_layer[t] == OUTRO, units[t].depth, t))
    chosen: dict[str, None] = {}
    for target in ranked:
        chain = [target]
        while (parent := units[chain[-1]].parent) and parent in units and parent not in roots:
            chain.append(parent)
        missing = [n for n in reversed(chain) if n not in chosen]
        if len(chosen) + len(missing) > options.max_down_nodes:
            continue
        chosen.update(dict.fromkeys(missing))
    result = []
    for target in sorted(chosen, key=lambda t: (units[t].depth, t)):
        unit = units[target]
        path, line = nodes.where(target)
        if path is None and unit.members:  # classe que a BFS não alcançou
            path, line = nodes.where(unit.members[0])
        if not DAO_PATH.match(path or ""):  # classe também declarada em cópias (`db_x_antes.php`)
            copies = nodes.attrs.get(target, {}).get("declared_in") or ()
            copy = next((p for p in copies if DAO_PATH.match(str(p))), None)
            if copy:
                path, line = str(copy), None
        result.append(
            DownNode(
                target,
                nodes.labels.get(target)
                or ("Class" if target in daos else target.partition(":")[0]),
                unit_layer[target],
                nodes.name(target),
                unit.depth,
                unit.parent,
                unit.edge_type,
                unit.confidence,
                path,
                line,
                _members(nodes, unit, seen) if target in daos else [],
                unit.factory,
            )
        )
    return result, len(units), truncated, {name: counts[name] for name in LAYERS if counts[name]}


# --------------------------------------------------------------------------- entrada


def up_layers(
    conn: sqlite3.Connection, node_id: str, options: LayerOptions | None = None
) -> tuple[list[LayerPath], int, bool]:
    """Só o «para cima» de `file_layers`: `(caminhos, total, truncado)`. Não faz a
    BFS para baixo, que é a parte cara; os caminhos são os mesmos de `file_layers(...).up`."""
    options = options or LayerOptions()
    row = conn.execute("SELECT label FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None or row[0] not in ("File", "Class", "Method"):
        raise QueryError(f"fonte não encontrado: {node_id.partition(':')[2] or node_id}")
    nodes = _Nodes(conn)
    nodes.load([node_id])
    return _up(conn, nodes, node_id, options)


def layers_of(conn: sqlite3.Connection, node_ids: list[str]) -> dict[str, str]:
    """A camada de apresentação de cada nó (`layer_of`, com a classe do método e o `MAPS_TO`),
    numa consulta por lote. Nó ausente do grafo fica `outro`."""
    nodes = _Nodes(conn)
    nodes.load(node_ids)
    return {node_id: nodes.layer(node_id) for node_id in node_ids}


def file_layers(
    conn: sqlite3.Connection, node_id: str, options: LayerOptions | None = None
) -> Layers:
    """Camadas de `node_id` (`File`, `Class` ou `Method`) para cima e para baixo."""
    options = options or LayerOptions()
    row = conn.execute("SELECT label FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None or row[0] not in ("File", "Class", "Method"):
        raise QueryError(f"fonte não encontrado: {node_id.partition(':')[2] or node_id}")
    nodes = _Nodes(conn)
    nodes.load([node_id])
    up, up_total, up_truncated = _up(conn, nodes, node_id, options)
    down, down_total, down_truncated, down_by_layer = _down(conn, nodes, node_id, options)
    return Layers(
        node=node_id,
        label=row[0],
        layer=nodes.layer(node_id),
        name=nodes.name(node_id),
        min_confidence=options.min_confidence,
        up=up,
        up_total=up_total,
        up_truncated=up_truncated,
        down=down,
        down_total=down_total,
        down_truncated=down_truncated,
        down_by_layer=down_by_layer,
    )
