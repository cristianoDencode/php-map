"""Impacto por tabela e DAO: CLI, interface (SQL) e MCP dizem o mesmo.

`web/repo.py:impact` (BFS em SQL) e `analysis/impact.py:impact` (grafo em memória) são duas
implementações; aqui elas são comparadas na fixture, com a MCP por cima da segunda.
"""

from pathlib import Path
from typing import Any

import pytest

from php_map.analysis.impact import ImpactOptions, impact
from php_map.graph import store
from php_map.mcp import tools
from php_map.mcp.catalog import ProjectCatalog
from php_map.web import repo


@pytest.fixture(scope="module")
def cli_result(shop_db: Path) -> Any:
    return impact(store.load(shop_db), ImpactOptions("pedidocancelado", "public"))


@pytest.fixture(scope="module")
def web_result(shop_db: Path) -> Any:
    with store.connect_readonly(shop_db) as conn:
        return repo.impact(conn, "pedidocancelado", "public")


def _rows(users: Any) -> list[tuple[str, str, str, float]]:
    return [(u.origin, u.operation, u.dao_method, u.confidence) for u in users]


def test_method_files_enter_affected_files(cli_result: Any) -> None:
    by_path = {f.path: f for f in cli_result.files}
    # `pedido::cancelar` chama `cl_pedidocancelado::incluir`: o arquivo dele agora conta
    assert by_path["classes/pedido.php"].via == "método"
    assert by_path["classes/db_pedidocancelado_classe.php"].via == "método"
    assert by_path["ped4_cancelarpedido001.php"].via is None  # alcançado pela busca
    assert (
        by_path["classes/pedido.php"].depth
        == cli_result.walk.steps["Method:pedido::cancelar"].depth
    )


def test_dao_users_are_callers_of_the_dao_methods(cli_result: Any) -> None:
    users = {(u.origin, u.operation): u for u in cli_result.dao_users}
    caller = users[("Method:pedido::cancelar", "INSERT")]
    assert caller.dao_method == "incluir" and caller.label == "Method"
    assert caller.confidence == 1.0 and caller.evidence["path"] == "classes/pedido.php"
    direct = {d.origin for d in cli_result.direct}
    assert not direct & {u.origin for u in cli_result.dao_users}  # a lista direta é só SQL
    assert [(u.label, u.origin, u.operation) for u in cli_result.dao_users] == sorted(
        (u.label, u.origin, u.operation) for u in cli_result.dao_users
    )


def test_min_confidence_filters_dao_edges_too(shop_db: Path) -> None:
    graph = store.load(shop_db)
    result = impact(graph, ImpactOptions("pedidocancelado", "public", min_confidence=1.0))
    assert all(u.confidence >= 1.0 for u in result.dao_users)


def test_web_has_the_same_files_and_dao_users_as_the_cli(cli_result: Any, web_result: Any) -> None:
    assert _rows(web_result.dao_users) == _rows(cli_result.dao_users)
    summary = web_result.summary
    assert summary["files"] == len(cli_result.files)
    assert summary["files_via_method"] == sum(1 for f in cli_result.files if f.via)
    assert summary["screens"] == sum(1 for f in cli_result.files if f.is_screen)
    assert summary["rpc_files"] == sum(1 for f in cli_result.files if f.is_rpc)
    assert summary["direct"] == len(cli_result.direct)
    assert summary["code"] == len(cli_result.code)
    assert summary["dao_users"] == len(cli_result.dao_users)
    # os da busca: o arquivo e a classe acrescentados ao desenho não contam
    assert summary["nodes"] == len(cli_result.walk.steps)


def test_web_subgraph_stays_connected_with_declares_edges(web_result: Any) -> None:
    ids = {n.id for n in web_result.nodes}
    assert {"File:classes/pedido.php", "Class:pedido"} <= ids
    declares = [e for e in web_result.edges if e.type == "DECLARES"]
    assert {(e.src, e.dst) for e in declares} >= {
        ("File:classes/pedido.php", "Class:pedido"),
        ("Class:pedido", "Method:pedido::cancelar"),
    }
    assert all(e.evidence.get("via") == "declares" for e in declares)
    assert all(e.src in ids and e.dst in ids for e in web_result.edges)
    # cada nó acrescentado tem pai na busca (o desenho e o `parentOf` usam isso)
    steps = web_result.walk.walk.steps
    for node in ("File:classes/pedido.php", "Class:pedido"):
        assert steps[node].parent in steps and steps[node].edge_type == "DECLARES"


def test_web_truncation_keeps_the_count_but_not_the_drawing(shop_db: Path) -> None:
    with store.connect_readonly(shop_db) as conn:
        small = repo.impact(conn, "pedidocancelado", "public", max_nodes=8)
        full = repo.impact(conn, "pedidocancelado", "public")
    assert small.truncated
    assert len(small.nodes) <= 8  # o desenho respeita o limite...
    ids = {n.id for n in small.nodes}
    assert all(e.src in ids and e.dst in ids for e in small.edges if e.type == "DECLARES")
    assert not full.truncated  # ...e o resumo conta os arquivos que ficaram de fora
    assert small.summary["files_via_method"] >= 1


def test_mcp_returns_dao_users(shop_db: Path, web_result: Any) -> None:
    catalog = ProjectCatalog.from_dbs([shop_db])
    slug = catalog.projects[0].slug
    out = tools.impact(catalog, "pedidocancelado", slug, schema="public")
    assert out["dao_users_total"] == len(web_result.dao_users)
    first = next(u for u in out["dao_users"] if u["origin"] == "Method:pedido::cancelar")
    assert first == {
        "origin": "Method:pedido::cancelar",
        "label": "Method",
        "operation": "INSERT",
        "dao_method": "incluir",
        "confidence": 1.0,
        "path": "classes/pedido.php",
        "line": 40,
    }
    assert out["summary"]["files_via_method"] == web_result.summary["files_via_method"]
