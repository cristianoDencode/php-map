"""Os diagramas de impacto e trace mostram os mesmos números da CLI.

A interface lê o `summary` de `trace`/`impact` (BFS em SQL). Aqui ele é comparado
com a saída de `php-map trace`/`impact` (BFS no NetworkX) sobre o banco das
fixtures, com os mesmos filtros.
"""

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from php_map.cli import app as cli_app
from php_map.web.app import create_app

LOCAL = "http://127.0.0.1:8765"
OPS = ("INSERT", "UPDATE", "DELETE", "SELECT")

TRACE = """query($entry: String!, $depth: Int!, $conf: Float!, $infra: Boolean!, $pg: Boolean!) {
  trace(entry: $entry, depth: $depth, minConfidence: $conf, includeInfra: $infra,
        expandPgFunctions: $pg, maxNodes: 2000) { summary truncated parentOf depthOf } }"""
IMPACT = """query($table: String!, $depth: Int!, $conf: Float!, $infra: Boolean!) {
  impact(table: $table, depth: $depth, minConfidence: $conf, includeInfra: $infra,
         maxNodes: 2000) { summary truncated parentOf depthOf } }"""


@pytest.fixture(scope="module")
def client(shop_db: Path) -> TestClient:
    return TestClient(create_app(shop_db, ui_dir=None), base_url=LOCAL)


def api(client: TestClient, query: str, **variables: Any) -> dict[str, Any]:
    response = client.post("/graphql", json={"query": query, "variables": variables})
    body = response.json()
    assert "errors" not in body, body.get("errors")
    return dict(next(iter(body["data"].values())))


def cli(shop_db: Path, tmp_path: Path, *args: str) -> str:
    result = CliRunner().invoke(
        cli_app, [*args, "--db", str(shop_db), "--output", str(tmp_path / "saida.md")]
    )
    assert result.exit_code == 0, result.output
    return " ".join(result.output.split())  # o rich quebra linhas longas


def _flags(depth: int, conf: float, infra: bool) -> list[str]:
    flags = ["--depth", str(depth), "--min-confidence", str(conf)]
    return flags + (["--include-infra"] if infra else [])


@pytest.mark.parametrize(
    ("depth", "conf", "infra", "pg"),
    [(8, 0.0, False, False), (4, 0.7, True, False), (8, 0.5, False, True), (2, 0.0, True, False)],
)
def test_trace_numbers_match_the_cli(
    client: TestClient,
    shop_db: Path,
    tmp_path: Path,
    depth: int,
    conf: float,
    infra: bool,
    pg: bool,
) -> None:
    entry = "ped4_cancelarpedido001.php"
    args = ["trace", "--entry", entry, *_flags(depth, conf, infra)]
    text = cli(shop_db, tmp_path, *args, *(["--expand-pgfunctions"] if pg else []))
    match = re.search(r"(\d+) nós · (\d+) arquivos · tabelas: (.*?) · RPC: (\d+)", text)
    assert match, text
    tables = {}
    if match.group(3) != "nenhuma":
        tables = {op: int(n) for op, n in (part.split() for part in match.group(3).split(", "))}
    result = api(client, TRACE, entry=entry, depth=depth, conf=conf, infra=infra, pg=pg)
    summary = result["summary"]
    assert not result["truncated"]
    assert (summary["nodes"], summary["files"], summary["rpc"]) == (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(4)),
    )
    assert summary["tables"] == tables
    assert set(result["parentOf"]) == set(result["depthOf"]) - {f"File:{entry}"}


@pytest.mark.parametrize(
    ("depth", "conf", "infra"), [(10, 0.0, False), (3, 0.5, True), (10, 0.7, False)]
)
def test_impact_numbers_match_the_cli(
    client: TestClient, shop_db: Path, tmp_path: Path, depth: int, conf: float, infra: bool
) -> None:
    text = cli(shop_db, tmp_path, "impact", "--table", "pedido", *_flags(depth, conf, infra))
    match = re.search(
        r"(\d+) tocam direto · (\d+) usam pelo DAO · (\d+) arquivos afetados \((\d+) telas\)"
        r" · (\d+) métodos/funções",
        text,
    )
    assert match, text
    result = api(client, IMPACT, table="pedido", depth=depth, conf=conf, infra=infra)
    summary = result["summary"]
    assert not result["truncated"]
    assert (
        summary["direct"],
        summary["dao_users"],
        summary["files"],
        summary["screens"],
        summary["code"],
    ) == tuple(int(g) for g in match.groups())
    # A raiz não tem pai, e todo pai está um nível acima do filho.
    depth_of, parent_of = result["depthOf"], result["parentOf"]
    assert "Table:public.pedido" not in parent_of
    assert all(depth_of[parent] == depth_of[node] - 1 for node, parent in parent_of.items())


def test_mermaid_follows_the_filters(client: TestClient) -> None:
    query = """query($infra: Boolean!) { mermaid(kind: "trace", root: "ped4_cancelarpedido001.php",
        depth: 8, maxNodes: 500, includeInfra: $infra) }"""

    def mermaid(infra: bool) -> str:
        body = client.post("/graphql", json={"query": query, "variables": {"infra": infra}}).json()
        assert "errors" not in body, body.get("errors")
        return str(body["data"]["mermaid"])

    assert "db_stdlib.php" not in mermaid(False)
    assert "db_stdlib.php" in mermaid(True)
