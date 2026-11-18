"""Menus por fonte: os itens de menu do e-cidade que chegam a um arquivo.

- O nome digitado vira um nó `File` real (`resolve_file`): caminho exato, nome do arquivo ou
  trecho do caminho; a primeira tentativa com exatamente 1 arquivo resolve, mais de 1 vira a
  lista de candidatos.
- A busca é a reversa do impacto (`walk_sql`, arestas de `TRAVERSED`, sem atravessar
  `is_infra`), a partir do arquivo **e** do que ele declara (fecho de `DECLARES`): `CALLS` e
  `INSTANTIATES` chegam na `Class`/`Function`/`Method`, não no `File`. `DECLARES` só monta as
  raízes, nunca é percorrido.
- Cada arquivo alcançado com `IMPLEMENTS_ACTION` dá um item por menu, com a **distância** (a
  profundidade do arquivo de entrada; 0 = o próprio fonte) e o caminho até o fonte (`via`).
- Módulo do item: o `Module` cujo `name` é a raiz (`nome_modulo`) de algum caminho do menu;
  sem caminho ou sem raiz conhecida, o `BELONGS_TO` do arquivo de entrada.
"""

import json
import sqlite3
import unicodedata
from dataclasses import dataclass, field

from php_map.errors import QueryError
from php_map.web import repo
from php_map.web.subgraph import SqlWalk, batches, placeholders, walk_sql

MENU_MAX_DISTANCE = 10
MENU_DEFAULT_DISTANCE = 5  # tela → JS → rota → controller → service: 4 saltos
MENU_WALK_NODES = 60_000  # teto da BFS; acima disso, `truncated`
FIND_LIMIT = 20
MIN_FIND_CHARS = 2
_REAL_FILE = "label = 'File' AND coalesce(json_extract(attrs, '$.resolved'), 1) != 0"


# --------------------------------------------------------------------------- nome → arquivo


@dataclass(frozen=True, slots=True)
class FileMatch:
    file: str | None  # "File:<caminho>" resolvido, ou None
    candidates: list[str] = field(default_factory=list)  # quando ambíguo


def normalize(text: str, workspace_root: str | None = None) -> str:
    """Caminho relativo à pasta do e-cidade: sem `File:`, com `/`, sem a pasta em uso."""
    path = text.strip().replace("\\", "/")
    if path.lower().startswith("file:"):
        path = path[5:]
    if workspace_root:
        root = workspace_root.strip().replace("\\", "/").rstrip("/") + "/"
        if path.lower().startswith(root.lower()):
            path = path[len(root) :]
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def _like(text: str) -> str:
    return text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _order(ids: list[str], text: str) -> list[str]:
    """Os que casam pelo nome do arquivo primeiro; depois o caminho mais curto, e alfabética."""
    wanted = text.lower()

    def key(node_id: str) -> tuple[int, int, str]:
        path = node_id.partition(":")[2]
        return (0 if wanted in path.rpartition("/")[2].lower() else 1, len(path), path.lower())

    return sorted(dict.fromkeys(ids), key=key)


def _files(conn: sqlite3.Connection, where: str, params: tuple[str, ...]) -> list[str]:
    return [
        r[0] for r in conn.execute(f"SELECT id FROM nodes WHERE {_REAL_FILE} AND {where}", params)
    ]


def _exact(conn: sqlite3.Connection, path: str) -> list[str]:
    return _files(conn, "lower(id) = ?", (f"file:{path.lower()}",))


def _by_name(conn: sqlite3.Connection, path: str) -> list[str]:
    return _files(
        conn,
        "(lower(id) = ? OR lower(id) LIKE ? ESCAPE '\\')",
        (f"file:{path.lower()}", f"%/{_like(path)}"),
    )


def _containing(conn: sqlite3.Connection, path: str) -> list[str]:
    return _files(conn, "lower(id) LIKE ? ESCAPE '\\'", (f"file:%{_like(path)}%",))


def find_files(
    conn: sqlite3.Connection, text: str, limit: int = 10, workspace_root: str | None = None
) -> list[str]:
    """Autocompletar: arquivos cujo caminho contém o texto (mínimo de 2 caracteres)."""
    path = normalize(text, workspace_root)
    if len(path) < MIN_FIND_CHARS or limit <= 0:
        return []
    return _order(_containing(conn, path), path)[:limit]


def resolve_file(
    conn: sqlite3.Connection, text: str, limit: int = FIND_LIMIT, workspace_root: str | None = None
) -> FileMatch:
    path = normalize(text, workspace_root)
    if not path:
        return FileMatch(None)
    for attempt in (_exact, _by_name, _containing):
        found = attempt(conn, path)
        if len(found) == 1:
            return FileMatch(found[0])
        if found:
            return FileMatch(None, _order(found, path)[:limit])
    return FileMatch(None)


# --------------------------------------------------------------------------- menus


@dataclass(frozen=True, slots=True)
class ViaStep:
    """Um passo do caminho, do arquivo de entrada até o fonte.

    `edge_type`/`confidence`/`path`/`line` são da aresta entre o passo anterior e este (o 1º
    passo não tem). `incoming`: a aresta real vai deste nó para o anterior (`DECLARES`, do
    arquivo para a classe ou função que ele declara)."""

    node: str
    label: str
    edge_type: str | None = None
    confidence: float | None = None
    path: str | None = None
    line: int | None = None
    incoming: bool = False


@dataclass(frozen=True, slots=True)
class MenuHit:
    menu: str
    name: str | None
    paths: list[str]
    modules: list[tuple[str, str]]  # (id do Module, nome)
    active: bool
    params: str | None
    entry: str
    distance: int
    via: list[ViaStep]


@dataclass(frozen=True, slots=True)
class FileMenus:
    file: str
    max_distance: int
    items: list[MenuHit]
    total: int
    unfiltered: int
    by_distance: dict[int, int]
    truncated: bool
    clamped: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Declared:
    parent: str
    label: str
    path: str | None
    line: int | None


def _declared(conn: sqlite3.Connection, file: str) -> dict[str, _Declared]:
    """Fecho de `DECLARES` a partir do arquivo (File → Function/Class; Class → Method)."""
    found: dict[str, _Declared] = {}
    frontier = [file]
    while frontier:
        next_frontier: list[str] = []
        for chunk in batches(frontier):
            rows = conn.execute(
                "SELECT e.src, e.dst, n.label, e.evidence FROM edges e JOIN nodes n ON n.id = e.dst"
                f" WHERE e.type = 'DECLARES' AND e.src IN ({placeholders(len(chunk))})"
                " ORDER BY e.src, e.dst, e.id",
                tuple(chunk),
            )
            for src, dst, label, raw in rows:
                if dst == file or dst in found:
                    continue
                evidence = json.loads(raw)
                found[dst] = _Declared(src, label, evidence.get("path"), evidence.get("line"))
                next_frontier.append(dst)
        frontier = next_frontier
    return found


def declared_closure(conn: sqlite3.Connection, node: str) -> list[str]:
    """O que `node` declara, em cascata (`File` → `Class`/`Function` → `Method`), na ordem em
    que a busca os acha. É o conjunto de raízes extras da busca por menus (e das camadas)."""
    return list(_declared(conn, node))


def _via(walked: SqlWalk, entry: str, file: str, declared: dict[str, _Declared]) -> list[ViaStep]:
    steps, labels = walked.walk.steps, walked.labels
    chain = list(reversed(walked.walk.path_to(entry)))  # entrada … raiz alcançada
    via = [ViaStep(entry, labels[entry])]
    for previous, node in zip(chain, chain[1:], strict=False):
        step = steps[previous]  # na busca reversa, o passo de `previous` guarda a aresta até `node`
        via.append(
            ViaStep(
                node,
                labels[node],
                step.edge_type,
                step.confidence,
                step.evidence.get("path"),
                step.evidence.get("line"),
            )
        )
    node = chain[-1]
    while node != file and node in declared:
        info = declared[node]
        parent_label = (
            declared[info.parent].label if info.parent in declared else labels.get(file, "File")
        )
        via.append(ViaStep(info.parent, parent_label, "DECLARES", 1.0, info.path, info.line, True))
        node = info.parent
    return via


def _plain(text: str) -> str:
    """Minúsculas e sem acento, para o filtro de texto."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _menu_paths(attrs: dict[str, object]) -> list[str]:
    paths = attrs.get("menu_paths")
    if isinstance(paths, list) and paths:
        return [str(p) for p in paths]
    single = attrs.get("menu_path")
    return [str(single)] if single else []


def menus_for_file(
    conn: sqlite3.Connection,
    file: str,
    *,
    max_distance: int = MENU_DEFAULT_DISTANCE,
    module: str | None = None,
    text: str | None = None,
    min_confidence: float = 0.0,
) -> FileMenus:
    clamp = repo.Clamp()
    max_distance = clamp("maxDistance", max_distance, MENU_MAX_DISTANCE, 0)
    if not conn.execute(f"SELECT 1 FROM nodes WHERE id = ? AND {_REAL_FILE}", (file,)).fetchone():
        raise QueryError(f"fonte não encontrado: {file.partition(':')[2] or file}")
    return menus_from_node(
        conn,
        file,
        max_distance=max_distance,
        module=module,
        text=text,
        min_confidence=min_confidence,
        clamped=clamp.clamped,
    )


def menus_from_node(
    conn: sqlite3.Connection,
    file: str,
    *,
    max_distance: int,
    module: str | None = None,
    text: str | None = None,
    min_confidence: float = 0.0,
    clamped: list[str] | None = None,
) -> FileMenus:
    """A busca reversa a partir de `file`, que pode ser também uma `Class` ou um `Method`:
    as raízes são o nó e o que ele declara. `file` precisa existir."""
    declared = _declared(conn, file)
    walked = walk_sql(
        conn,
        file,
        depth=max_distance,
        min_confidence=min_confidence,
        include_infra=False,
        reverse=True,
        max_nodes=MENU_WALK_NODES,
        roots=list(declared),
    )
    files = [node for node, label in walked.labels.items() if label == "File"]
    module_ids = {
        name: node_id
        for node_id, name in conn.execute(
            "SELECT id, json_extract(attrs, '$.name') FROM nodes WHERE label = 'Module'"
        )
        if name
    }
    entry_modules: dict[str, list[tuple[str, str]]] = {}
    hits: list[MenuHit] = []
    for chunk in batches(files):
        rows = conn.execute(
            "SELECT DISTINCT e.src, n.id, n.attrs FROM edges e JOIN nodes n ON n.id = e.dst"
            f" WHERE e.type = 'IMPLEMENTS_ACTION' AND e.src IN ({placeholders(len(chunk))})",
            tuple(chunk),
        )
        for entry, menu, raw in rows:
            attrs = json.loads(raw)
            paths = _menu_paths(attrs)
            roots = dict.fromkeys(p.split(" > ")[0].strip() for p in paths)
            modules = [(module_ids[r], r) for r in roots if r in module_ids]
            if not modules:
                if entry not in entry_modules:
                    entry_modules[entry] = [
                        (m["module"], m["name"]) for m in repo.memberships(conn, entry)
                    ]
                modules = entry_modules[entry]
            name = attrs.get("name")
            params = attrs.get("params")
            hits.append(
                MenuHit(
                    menu=menu,
                    name=str(name) if name else None,
                    paths=paths,
                    modules=modules,
                    active=attrs.get("active") is not False,
                    params=str(params) if params else None,
                    entry=entry,
                    distance=walked.walk.steps[entry].depth,
                    via=_via(walked, entry, file, declared),
                )
            )
    hits.sort(key=lambda h: (h.distance, _plain(h.paths[0] if h.paths else h.name or ""), h.menu))
    by_distance: dict[int, int] = {}
    for hit in hits:
        by_distance[hit.distance] = by_distance.get(hit.distance, 0) + 1
    wanted = _plain(text.strip()) if text and text.strip() else None
    items = [
        h
        for h in hits
        if (module is None or any(m == module for m, _ in h.modules))
        and (wanted is None or any(wanted in _plain(t) for t in [*h.paths, h.name or ""]))
    ]
    return FileMenus(
        file=file,
        max_distance=max_distance,
        items=items,
        total=len(items),
        unfiltered=len(hits),
        by_distance=dict(sorted(by_distance.items())),
        truncated=walked.truncated,
        clamped=clamped or [],
    )
