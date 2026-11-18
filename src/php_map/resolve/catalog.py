"""Catálogo do e-cidade no grafo: `Module`, `BusinessAction` e as arestas
`BELONGS_TO`/`IMPLEMENTS_ACTION` com `source=ECIDADE_CATALOG` e confiança 1.0.

Nenhum nó fantasma: só recebe aresta a `Table` ou o `File` que já está no grafo. O que o
catálogo cita e o scan não tem vai para as estatísticas.

Módulos: os do dicionário e os do menu se fundem pelo nome normalizado ou
pela tabela de equivalência (`config.MODULE_ALIASES`). O módulo presente no menu
usa o ID e o nome do menu.
"""

import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from php_map.config import MODULE_ALIASES
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    business_action_id,
    file_id,
    key_of,
    module_id,
    normalize_module_name,
)
from php_map.ingest.catalog_sql import CatalogData, MenuEdge, MenuItem, TableModule

MAX_MENU_DEPTH = 15
MAX_MENU_PATHS = 5
MAX_MISSING_EXAMPLES = 20
PATH_SEPARATOR = " > "


@dataclass(slots=True)
class CatalogStats:
    modules_dictionary: int = 0  # linhas de db_sysmodulo com nome válido
    modules_menu: int = 0  # linhas de db_modulos com nome válido
    modules: int = 0  # nós Module (os dois cadastros fundidos)
    modules_merged: int = 0  # Module presente nos dois cadastros (pelo nome ou pela equivalência)
    modules_aliased: int = 0  # pares de MODULE_ALIASES aplicados
    aliases_unused: list[str] = field(default_factory=list)  # `dicionário→menu` sem um dos lados
    modules_dictionary_only: list[str] = field(default_factory=list)  # chaves de Module sem menu
    modules_menu_only: list[str] = field(default_factory=list)  # chaves de Module sem dicionário
    modules_invalid: int = 0  # nome que fica vazio depois de normalizado
    tables_catalog: int = 0  # linhas de db_sysarquivo
    tables_annotated: int = 0  # nós Table que receberam `catalog`
    tables_with_module: int = 0  # nós Table com BELONGS_TO
    tables_unreferenced: int = 0  # db_sysarquivo sem nó Table no grafo
    table_modules_unknown: int = 0  # db_sysarqmod para módulo ou tabela que o catálogo não tem
    menu_items: int = 0
    menu_items_with_file: int = 0  # com `funcao`
    menu_items_inactive: int = 0
    menu_actions: int = 0  # nós BusinessAction
    menu_missing_files: int = 0
    menu_missing_examples: list[str] = field(default_factory=list)
    menu_without_path: int = 0  # item com arquivo que não está em nenhum db_menu
    menu_unknown_modules: int = 0  # caminho cujo db_menu.modulo não está em db_modulos
    menu_truncated: int = 0  # subida interrompida por ciclo ou profundidade
    edges: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class _MenuPath:
    text: str
    module: int  # db_menu.modulo
    first: MenuEdge  # a linha de db_menu que pendura o item (evidência do BELONGS_TO)


def apply_catalog(
    data: CatalogData, builder: GraphBuilder, aliases: Mapping[str, str] = MODULE_ALIASES
) -> CatalogStats:
    """`aliases`: nome normalizado do dicionário → nome normalizado do menu."""
    started = time.perf_counter()
    stats = CatalogStats()
    by_codmod, by_menu_id, menu_names = _modules(data, builder, stats, aliases)
    _tables(data, builder, stats, by_codmod)
    _menus(data, builder, stats, by_menu_id, menu_names)
    stats.edges = dict(sorted(stats.edges.items()))
    stats.seconds = round(time.perf_counter() - started, 2)
    return stats


def _edge(
    builder: GraphBuilder,
    stats: CatalogStats,
    src: str,
    dst: str,
    type_: EdgeType,
    evidence: Mapping[str, Any],
) -> None:
    builder.add_edge(
        Edge(src, dst, type_, EdgeSource.ECIDADE_CATALOG, 1.0, dict(evidence)),
    )
    stats.edges[type_.value] = stats.edges.get(type_.value, 0) + 1


# --------------------------------------------------------------------------- módulos


def _modules(
    data: CatalogData, builder: GraphBuilder, stats: CatalogStats, aliases: Mapping[str, str]
) -> tuple[dict[int, str], dict[int, str], dict[int, str]]:
    """Cria os `Module`. Devolve `codmod → id`, `db_modulos.id_item → id` e o nome do menu.

    O menu registra primeiro: o ID e o `name` do módulo presente no menu vêm dele. O módulo do
    dicionário cai no mesmo nó pelo nome normalizado ou por `aliases` (só se o lado do menu
    existir: nenhum nó fantasma). A `description` é a do dicionário, com a do menu de reserva
    (no menu, ela quase sempre repete o nome). Nome repetido no cadastro: fica o menor código.
    """
    attrs: dict[str, dict[str, Any]] = {}
    sources: dict[str, set[str]] = {}
    by_codmod: dict[int, str] = {}
    by_menu_id: dict[int, str] = {}
    menu_names: dict[int, str] = {}
    applied: set[str] = set()

    def register(node_id: str, name: str, origin: str, code: int, desc: str | None) -> None:
        item = attrs.setdefault(node_id, {"names": set(), "aliases": set()})
        item["names"].add(name)
        item.setdefault(f"{origin}_name", name)
        item.setdefault("codmod" if origin == "dictionary" else "menu_id", code)
        if desc and not item.get(f"{origin}_description"):
            item[f"{origin}_description"] = desc
        sources.setdefault(node_id, set()).add(origin)

    for menu in data.menu_modules:
        if not normalize_module_name(menu.name):
            stats.modules_invalid += 1
            continue
        node_id = module_id(menu.name)
        register(node_id, menu.name, "menu", menu.id_item, menu.description)
        by_menu_id[menu.id_item] = node_id
        menu_names[menu.id_item] = menu.name
        stats.modules_menu += 1
    in_menu = set(attrs)

    for module in data.modules:
        key = normalize_module_name(module.name)
        if not key:
            stats.modules_invalid += 1
            continue
        node_id = module_id(module.name)
        target = aliases.get(key)
        if target is not None and module_id(target) in in_menu:
            node_id = module_id(target)
            attrs[node_id]["aliases"].add(key)
            applied.add(key)
        register(node_id, module.name, "dictionary", module.codmod, module.description)
        by_codmod[module.codmod] = node_id
        stats.modules_dictionary += 1

    for node_id, item in attrs.items():
        node_attrs = {
            "name": item.get("menu_name") or item["dictionary_name"],
            "names": sorted(item["names"]),
            "aliases": sorted(item["aliases"]),
            "codmod": item.get("codmod"),
            "menu_id": item.get("menu_id"),
            "description": item.get("dictionary_description") or item.get("menu_description"),
        }
        builder.add_node(Node(node_id, NodeLabel.MODULE, node_attrs))
    stats.modules = len(attrs)
    stats.modules_merged = sum(1 for origins in sources.values() if len(origins) > 1)
    stats.modules_aliased = len(applied)
    stats.aliases_unused = sorted(f"{k}→{v}" for k, v in aliases.items() if k not in applied)
    stats.modules_dictionary_only = sorted(
        key_of(node_id) for node_id, origins in sources.items() if origins == {"dictionary"}
    )
    stats.modules_menu_only = sorted(
        key_of(node_id) for node_id, origins in sources.items() if origins == {"menu"}
    )
    return by_codmod, by_menu_id, menu_names


# --------------------------------------------------------------------------- tabelas


def _table_index(builder: GraphBuilder) -> dict[str, list[str]]:
    """Nome da tabela (sem schema) → IDs dos nós `Table`, reais ou placeholders."""
    index: dict[str, list[str]] = {}
    for node in builder.iter_nodes():
        if node.label == NodeLabel.TABLE:
            name = key_of(node.id).partition(".")[2]
            index.setdefault(name, []).append(node.id)
    return {name: sorted(ids) for name, ids in index.items()}


def _tables(
    data: CatalogData, builder: GraphBuilder, stats: CatalogStats, by_codmod: Mapping[int, str]
) -> None:
    index = _table_index(builder)
    columns = {column.codcam: column for column in data.columns}
    table_columns: dict[int, list[tuple[int, int]]] = {}
    for column_link in data.table_columns:
        order = column_link.sequence if column_link.sequence is not None else column_link.codcam
        table_columns.setdefault(column_link.codarq, []).append((order, column_link.codcam))
    modules_of: dict[int, list[TableModule]] = {}
    for link in data.table_modules:
        modules_of.setdefault(link.codarq, []).append(link)

    known = {table.codarq for table in data.tables}
    stats.table_modules_unknown += sum(
        1 for link in data.table_modules if link.codarq not in known or link.codmod not in by_codmod
    )
    with_module: set[str] = set()
    for table in data.tables:
        stats.tables_catalog += 1
        node_ids = index.get(table.name, [])
        if not node_ids:
            stats.tables_unreferenced += 1
            continue
        catalog = {
            "codarq": table.codarq,
            "description": table.description,
            "prefix": table.prefix,
            "label": table.label,
        }
        columns_catalog = {
            columns[codcam].name: {
                "label": columns[codcam].label,
                "description": columns[codcam].description,
            }
            for _order, codcam in sorted(table_columns.get(table.codarq, []))
            if codcam in columns
        }
        for node_id in node_ids:
            if "catalog" not in builder.node_attrs(node_id):
                stats.tables_annotated += 1
            # `ensure_node` só preenche o que falta e não mexe em `resolved` do placeholder.
            builder.ensure_node(
                node_id, NodeLabel.TABLE, catalog=catalog, columns_catalog=columns_catalog
            )
            for link in modules_of.get(table.codarq, []):
                target = by_codmod.get(link.codmod)
                if target is None:
                    continue
                _edge(builder, stats, node_id, target, EdgeType.BELONGS_TO, link.evidence)
                with_module.add(node_id)
    stats.tables_with_module = len(with_module)


# --------------------------------------------------------------------------- menus


def split_funcao(funcao: str) -> tuple[str, str | None]:
    """`'com4_x001.php?formacontrole=2'` → `('com4_x001.php', 'formacontrole=2')`.

    Normaliza como `file_id` (barra, `./`), sem mudar a caixa.
    """
    path, _sep, params = funcao.strip().partition("?")
    path = key_of(file_id(path.strip())).lstrip("/")
    return path, (params.strip() or None)


def _menu_paths(
    item: MenuItem,
    parents: Mapping[int, Sequence[MenuEdge]],
    descriptions: Mapping[int, str | None],
    menu_names: Mapping[int, str],
    stats: CatalogStats,
) -> list[_MenuPath]:
    """Caminhos do item até a raiz, um por linha de `db_menu` que o pendura.

    A subida só segue linhas do mesmo `modulo` e para no item raiz do módulo
    (`id_item == modulo`): as raízes "Cadastros", "Procedimentos"… são compartilhadas por
    todos os módulos, e subir sem o filtro levaria a todos eles. O prefixo é o `nome_modulo`.
    """
    found: list[_MenuPath] = []
    truncated = False
    for first in sorted(parents.get(item.id_item, ()), key=_edge_order):
        module = first.module
        stack: list[tuple[int, tuple[int, ...]]] = [(first.parent, (item.id_item,))]
        while stack:
            node, chain = stack.pop()
            ups = [edge for edge in parents.get(node, ()) if edge.module == module]
            if node == module:
                ups = []  # raiz do módulo: o nome dele vira o prefixo
            elif node in chain or len(chain) >= MAX_MENU_DEPTH:
                truncated, ups = True, []
            else:
                chain = (*chain, node)
            if ups:
                stack.extend((edge.parent, chain) for edge in sorted(ups, key=_edge_order))
                continue
            names = [descriptions.get(i) or f"[{i}]" for i in reversed(chain)]
            prefix = menu_names.get(module) or f"[{module}]"
            found.append(_MenuPath(PATH_SEPARATOR.join([prefix, *names]), module, first))
    stats.menu_truncated += truncated
    unique = {path.text: path for path in reversed(found)}  # mesmo texto: fica o 1º achado
    return sorted(unique.values(), key=lambda path: path.text)


def _edge_order(edge: MenuEdge) -> tuple[int, int, int]:
    return edge.module, edge.parent, edge.child


def _menus(
    data: CatalogData,
    builder: GraphBuilder,
    stats: CatalogStats,
    by_menu_id: Mapping[int, str],
    menu_names: Mapping[int, str],
) -> None:
    parents: dict[int, list[MenuEdge]] = {}
    for edge in data.menu_edges:
        parents.setdefault(edge.child, []).append(edge)
    descriptions = {item.id_item: item.description for item in data.menu_items}
    for item in data.menu_items:
        stats.menu_items += 1
        stats.menu_items_inactive += not item.active
        if not item.funcao:
            continue
        stats.menu_items_with_file += 1
        path, params = split_funcao(item.funcao)
        source = file_id(path)
        if not path or source not in builder:
            stats.menu_missing_files += 1
            if len(stats.menu_missing_examples) < MAX_MISSING_EXAMPLES:
                stats.menu_missing_examples.append(f"{item.id_item}: {item.funcao}")
            continue
        paths = _menu_paths(item, parents, descriptions, menu_names, stats)
        stats.menu_without_path += not paths
        action = business_action_id(f"menu:{item.id_item}")
        builder.add_node(
            Node(
                action,
                NodeLabel.BUSINESS_ACTION,
                {
                    "name": item.description,
                    "menu_path": paths[0].text if paths else None,
                    "menu_paths": [p.text for p in paths[:MAX_MENU_PATHS]],
                    "help": item.help,
                    "desctec": item.desctec,
                    "active": item.active,
                    "params": params,
                },
            )
        )
        stats.menu_actions += 1
        _edge(builder, stats, source, action, EdgeType.IMPLEMENTS_ACTION, item.evidence)
        for module, first in _modules_of_paths(paths):
            target = by_menu_id.get(module)
            if target is None:
                stats.menu_unknown_modules += 1
                continue
            _edge(builder, stats, source, target, EdgeType.BELONGS_TO, first.evidence)


def _modules_of_paths(paths: Sequence[_MenuPath]) -> Iterator[tuple[int, MenuEdge]]:
    """Um par `(modulo, linha de db_menu)` por módulo distinto, na ordem do código do módulo."""
    seen: dict[int, MenuEdge] = {}
    for path in sorted(paths, key=lambda p: _edge_order(p.first)):
        seen.setdefault(path.module, path.first)
    yield from sorted(seen.items())
