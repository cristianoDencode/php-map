"""Menus por fonte (`web.menus`) e BFS em SQL (`web.subgraph`) sobre o banco do legado fictício.

Menus da fixture: 7001 (cancelar pedido, `ped4_cancelarpedido001.php`), 7002 (inativo,
`ped4_baixaestoque004.php`) e 7003 (dois caminhos, Pedidos e Estoque, com `params`,
`func_pedido.php`)."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from php_map.analysis import LEAF_LABELS, walk
from php_map.errors import QueryError
from php_map.graph import store
from php_map.web.menus import (
    FileMatch,
    FileMenus,
    find_files,
    menus_for_file,
    normalize,
    resolve_file,
)
from php_map.web.subgraph import walk_sql

DAO = "File:classes/db_pedido_classe.php"
ENTRY = "File:ped4_cancelarpedido001.php"
TABLE = "Table:public.pedido"


@pytest.fixture
def conn(shop_db: Path) -> Iterator[sqlite3.Connection]:
    with closing(store.connect_readonly(shop_db)) as connection:
        yield connection


@pytest.fixture(scope="module")
def graph(shop_db: Path) -> Any:
    return store.load(shop_db)


def _summary(result: FileMenus) -> list[tuple[str, int]]:
    return [(h.menu.rpartition(":")[2], h.distance) for h in result.items]


# --------------------------------------------------------------------------- menus: distâncias


@pytest.mark.parametrize(
    ("file", "max_distance", "expected"),
    [
        (ENTRY, 3, [("7001", 0)]),
        ("File:forms/db_frmcancelarpedido.php", 3, [("7001", 1)]),
        (DAO, 3, [("7003", 1), ("7001", 1), ("7002", 2)]),
        (DAO, 1, [("7003", 1), ("7001", 1)]),
        ("File:func_pedido.php", 3, [("7003", 0), ("7001", 2)]),
        ("File:model/pedido/PedidoCliente.model.php", 3, [("7002", 2)]),
        ("File:libs/db_stdlib.php", 3, [("7003", 1), ("7002", 1), ("7001", 1)]),  # infra
        (ENTRY, 0, [("7001", 0)]),
        ("File:forms/db_frmcancelarpedido.php", 0, []),
        ("File:ped4_liberarpedidos.RPC.php", 5, []),  # nenhum menu leva ao RPC
    ],
)
def test_menus_by_distance(
    conn: sqlite3.Connection, file: str, max_distance: int, expected: list[tuple[str, int]]
) -> None:
    result = menus_for_file(conn, file, max_distance=max_distance)

    assert _summary(result) == expected
    assert result.total == result.unfiltered == len(expected)
    assert not result.truncated


def test_counts_by_distance_and_menu_attributes(conn: sqlite3.Connection) -> None:
    result = menus_for_file(conn, DAO)

    assert result.by_distance == {1: 2, 2: 1}
    by_id = {h.menu: h for h in result.items}
    inactive = by_id["BusinessAction:menu:7002"]
    assert inactive.active is False and inactive.entry == "File:ped4_baixaestoque004.php"
    both = by_id["BusinessAction:menu:7003"]
    assert both.paths == ["Estoque > Consultas > Pedidos", "Pedidos > Consultas > Pedidos"]
    assert both.modules == [("Module:estoque", "Estoque"), ("Module:pedidos", "Pedidos")]
    assert both.params == "funcao_js=parent.js_mostra"


def test_via_goes_from_the_entry_to_the_source(conn: sqlite3.Connection) -> None:
    for file in (DAO, "File:model/pedido/PedidoCliente.model.php", "File:libs/JSON.php"):
        for hit in menus_for_file(conn, file, max_distance=10).items:
            assert hit.via[0].node == hit.entry and hit.via[0].edge_type is None
            assert hit.via[-1].node == file
            walked = [s for s in hit.via[1:] if not s.incoming]
            assert len(walked) == hit.distance  # um passo por salto; DECLARES não conta
            assert all(s.edge_type for s in hit.via[1:])


def test_via_ends_with_declares_when_reached_through_a_declaration(
    conn: sqlite3.Connection,
) -> None:
    """Sem o fecho de `DECLARES`, nenhum menu chegaria: a tela chama `db_query`, não o arquivo."""
    (hit,) = (
        h
        for h in menus_for_file(conn, "File:libs/db_stdlibalt.php", max_distance=1).items
        if h.menu.endswith(":7002")
    )
    assert [(s.node, s.edge_type, s.incoming) for s in hit.via] == [
        ("File:ped4_baixaestoque004.php", None, False),
        ("Function:db_query", "CALLS", False),
        ("File:libs/db_stdlibalt.php", "DECLARES", True),
    ]
    assert hit.via[1].path == "ped4_baixaestoque004.php" and hit.via[1].line
    assert hit.via[2].path == "libs/db_stdlibalt.php" and hit.via[2].line


# --------------------------------------------------------------------------- menus: filtros


def test_module_filter_matches_any_menu_path(conn: sqlite3.Connection) -> None:
    estoque = menus_for_file(conn, DAO, module="Module:estoque")
    pedidos = menus_for_file(conn, DAO, module="Module:pedidos")

    assert _summary(estoque) == [("7003", 1)]
    assert estoque.total == 1 and estoque.unfiltered == 3 and estoque.by_distance == {1: 2, 2: 1}
    assert len(pedidos.items) == 3


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("consultas", ["7003"]),
        ("cancelar", ["7001"]),
        ("CANCELAR", ["7001"]),
        ("baixa (antiga)", ["7002"]),
        ("  ", ["7003", "7001", "7002"]),
        ("nada disso", []),
    ],
)
def test_text_filter_ignores_case_and_accents(
    conn: sqlite3.Connection, text: str, expected: list[str]
) -> None:
    result = menus_for_file(conn, DAO, text=text)

    assert [m for m, _ in _summary(result)] == expected
    assert result.unfiltered == 3


def test_distance_is_clamped(conn: sqlite3.Connection) -> None:
    result = menus_for_file(conn, DAO, max_distance=99)

    assert result.max_distance == 10 and result.clamped == ["maxDistance: 99 → 10"]
    assert menus_for_file(conn, DAO, max_distance=-3).max_distance == 0


def test_unknown_file_is_an_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(QueryError, match="fonte não encontrado: naoexiste.php"):
        menus_for_file(conn, "File:naoexiste.php")


# --------------------------------------------------------------------------- nome → arquivo


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("forms/db_frmcancelarpedido.php", FileMatch("File:forms/db_frmcancelarpedido.php")),
        ("FILE:Forms\\DB_frmcancelarpedido.php", FileMatch("File:forms/db_frmcancelarpedido.php")),
        ("pedido.php", FileMatch("File:classes/pedido.php")),  # nome do arquivo
        ("frmcancelar", FileMatch("File:forms/db_frmcancelarpedido.php")),  # trecho único
        (
            "cancelarpedido",
            FileMatch(None, [ENTRY, "File:forms/db_frmcancelarpedido.php"]),
        ),
        ("naoexiste.php", FileMatch(None, [])),
        ("   ", FileMatch(None, [])),
        ("100%_", FileMatch(None, [])),  # % e _ não viram curinga
    ],
)
def test_resolve_file(conn: sqlite3.Connection, text: str, expected: FileMatch) -> None:
    assert resolve_file(conn, text) == expected


def test_resolve_file_strips_the_workspace_folder(conn: sqlite3.Connection) -> None:
    root = "C:\\caminho\\loja"
    found = resolve_file(conn, f"{root}\\func_pedido.php", workspace_root=root)

    assert found == FileMatch("File:func_pedido.php")
    assert normalize("./libs/db_utils.php") == "libs/db_utils.php"
    assert normalize("c:/caminho/loja/x.php", root + "\\") == "x.php"


def test_resolve_file_limits_candidates(conn: sqlite3.Connection) -> None:
    found = resolve_file(conn, "classes/db_", limit=3)

    assert found.file is None and len(found.candidates) == 3


def test_find_files_for_autocomplete(conn: sqlite3.Connection) -> None:
    assert find_files(conn, "a") == []
    assert find_files(conn, "cancelarpedido") == [ENTRY, "File:forms/db_frmcancelarpedido.php"]
    names_first = find_files(conn, "pedido", limit=50)
    assert {"File:classes/pedido.php", "File:model/pedido/PedidoCliente.model.php"} <= set(
        names_first
    )
    assert names_first[0].rpartition("/")[2].find("pedido") >= 0  # nome do arquivo antes do resto
    assert len(find_files(conn, "pedido", limit=2)) == 2


# --------------------------------------------------------------------------- BFS em SQL


def _trace_expand(label: str) -> bool:
    return label not in LEAF_LABELS


def _steps(steps: Any) -> dict[str, tuple[Any, ...]]:
    return {
        node: (s.parent, s.edge_type, s.confidence, s.depth, s.evidence)
        for node, s in steps.items()
    }


CASES = [
    pytest.param(ENTRY, False, False, 0.0, id="trace"),
    pytest.param(ENTRY, False, True, 0.0, id="trace-com-infra"),
    pytest.param(ENTRY, False, False, 0.7, id="trace-confianca-0.7"),
    pytest.param(TABLE, True, False, 0.0, id="impacto"),
    pytest.param(TABLE, True, True, 0.0, id="impacto-com-infra"),
    pytest.param(TABLE, True, False, 0.7, id="impacto-confianca-0.7"),
]


@pytest.mark.parametrize(("root", "reverse", "include_infra", "min_confidence"), CASES)
def test_walk_sql_is_the_same_bfs(
    graph: Any,
    conn: sqlite3.Connection,
    root: str,
    reverse: bool,
    include_infra: bool,
    min_confidence: float,
) -> None:
    depth = 10 if reverse else 8

    def expand(node: str) -> bool:
        return _trace_expand(str(graph.nodes[node]["label"]))

    expected = walk(
        graph,
        root,
        depth=depth,
        min_confidence=min_confidence,
        include_infra=include_infra,
        reverse=reverse,
        expand=None if reverse else expand,
    )
    got = walk_sql(
        conn,
        root,
        depth=depth,
        min_confidence=min_confidence,
        include_infra=include_infra,
        reverse=reverse,
        expand=None if reverse else _trace_expand,
    )

    assert list(got.walk.steps) == list(expected.steps)  # mesma ordem de visita
    assert _steps(got.walk.steps) == _steps(expected.steps)
    assert not got.truncated and len(got.walk.steps) > 5
    assert got.labels[root] == ("Table" if reverse else "File")


def test_depth_limit_matches(graph: Any, conn: sqlite3.Connection) -> None:
    for depth in (1, 2, 3):
        expected = walk(graph, ENTRY, depth=depth, min_confidence=0, include_infra=False)
        got = walk_sql(conn, ENTRY, depth=depth, min_confidence=0, include_infra=False)
        assert _steps(got.walk.steps) == _steps(expected.steps)


def test_max_nodes_truncates_in_bfs_order(conn: sqlite3.Connection) -> None:
    full = walk_sql(conn, ENTRY, depth=8, min_confidence=0, include_infra=False)
    cut = walk_sql(conn, ENTRY, depth=8, min_confidence=0, include_infra=False, max_nodes=5)
    assert cut.truncated and len(cut.walk.steps) == 5
    assert list(cut.walk.steps) == list(full.walk.steps)[:5]
    exact = walk_sql(
        conn, ENTRY, depth=8, min_confidence=0, include_infra=False, max_nodes=len(full.walk.steps)
    )
    assert not exact.truncated


def test_edge_ids_point_to_the_discovering_edge(conn: sqlite3.Connection) -> None:
    got = walk_sql(conn, ENTRY, depth=2, min_confidence=0, include_infra=False)
    for node, edge_id in got.edge_ids.items():
        src, dst, type_ = conn.execute(
            "SELECT src, dst, type FROM edges WHERE id = ?", (edge_id,)
        ).fetchone()
        step = got.walk.steps[node]
        assert (src, dst, type_) == (step.parent, node, step.edge_type)


def test_extra_roots_start_at_depth_zero(conn: sqlite3.Connection) -> None:
    """`roots` entram na profundidade 0, sem pai, e o `Walk.root` segue o `root`."""
    root, extra = "File:libs/db_stdlibalt.php", "Function:db_query"
    alone = walk_sql(conn, root, depth=3, min_confidence=0, include_infra=False, reverse=True)
    both = walk_sql(
        conn, root, depth=3, min_confidence=0, include_infra=False, reverse=True, roots=[extra]
    )

    assert both.walk.root == root
    assert both.walk.steps[extra].depth == 0 and both.walk.steps[extra].parent is None
    assert both.labels[extra] == "Function"
    assert "File:ped4_baixaestoque004.php" not in alone.walk.steps  # só chega pela função
    assert "File:ped4_baixaestoque004.php" in both.walk.steps
