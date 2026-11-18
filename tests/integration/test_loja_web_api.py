"""API GraphQL e servidor da interface sobre o banco do legado fictício (`loja-mini`)."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from php_map.cli import app as cli_app
from php_map.web.app import create_app
from php_map.web.schema import schema

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
LOCAL = "http://127.0.0.1:8765"
ENTRY = "ped4_cancelarpedido001.php"
TRACE = """query($entry: String!, $max: Int!) { trace(entry: $entry, maxNodes: $max) {
    root nodes { id label } edges { src dst type } depthOf truncated summary } }"""
MENUS_QUERY = """query($file: String!, $d: Int!, $module: String, $text: String) {
    menusForFile(file: $file, maxDistance: $d, module: $module, text: $text) {
        file candidates maxDistance total unfiltered truncated clamped
        byDistance { distance count }
        items { menu name paths active params entry distance
                modules { module name }
                via { node label edgeType confidence path line incoming } } } }"""


@pytest.fixture(scope="module")
def client(shop_db: Path) -> TestClient:
    return TestClient(create_app(shop_db, ui_dir=None), base_url=LOCAL)


def gql(client: TestClient, query: str, **variables: Any) -> dict[str, Any]:
    response = client.post("/graphql", json={"query": query, "variables": variables})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def data(client: TestClient, query: str, **variables: Any) -> dict[str, Any]:
    body = gql(client, query, **variables)
    assert "errors" not in body, body.get("errors")
    result: dict[str, Any] = body["data"]
    return result


# --------------------------------------------------------------------------- nós e vizinhos


def test_node_and_paginated_neighbors(client: TestClient) -> None:
    query = """query($id: ID!, $offset: Int!) { node(id: $id) {
        id label name path modules memberships { module name source confidence }
        neighbors(limit: 3, offset: $offset) { total items { src dst type confidence evidence
          occurrences sampleLines } nodes { id label } } } }"""
    first = data(client, query, id=f"File:{ENTRY}", offset=0)["node"]

    assert first["label"] == "File" and first["path"] == ENTRY
    assert first["modules"] == ["Module:pedidos"]
    assert first["memberships"][0] == {
        "module": "Module:pedidos",
        "name": "Pedidos",
        "source": "ECIDADE_CATALOG",
        "confidence": 1.0,
    }
    page = first["neighbors"]
    assert page["total"] > 3 and len(page["items"]) == 3
    assert {n["id"] for n in page["nodes"]} == {e["dst"] for e in page["items"]}
    second = data(client, query, id=f"File:{ENTRY}", offset=3)["node"]
    assert second["neighbors"]["items"][0] != page["items"][0]
    assert data(client, 'query { node(id: "File:nao-existe.php") { id } }')["node"] is None


def test_incoming_neighbors_and_infra_filter(client: TestClient) -> None:
    query = """query($infra: Boolean!) { node(id: "Table:public.pedido") {
        neighbors(direction: IN, includeInfra: $infra, limit: 500) { total } } }"""
    without = data(client, query, infra=False)["node"]["neighbors"]["total"]
    with_infra = data(client, query, infra=True)["node"]["neighbors"]["total"]
    assert 0 < without <= with_infra


# --------------------------------------------------------------------------- busca


def test_search_and_suggest(client: TestClient) -> None:
    hits = data(
        client,
        'query { search(text: "cancelamento de pedido") { nodeId label snippet nextCommand } }',
    )["search"]
    assert "BusinessAction:menu:7001" in [h["nodeId"] for h in hits]
    suggest = data(client, 'query { suggest(prefix: "cancelar") { nodeId } }')["suggest"]
    assert f"File:{ENTRY}" in [h["nodeId"] for h in suggest]


# --------------------------------------------------------------------------- trace e impacto


def test_trace_reaches_the_gabarito(client: TestClient) -> None:
    result = data(client, TRACE, entry=ENTRY, max=2000)["trace"]
    expected = json.loads((FIXTURES / "expected/loja_trace.json").read_text(encoding="utf-8"))
    reached = {n["id"] for n in result["nodes"]}

    assert [i["node"] for i in expected["must_reach"] if i["node"] not in reached] == []
    for item in expected["must_not_reach"]:
        assert item["node"] not in reached
    summary = result["summary"]
    assert summary["files"] > 0 and summary["tables"]["INSERT"] >= 1 and not result["truncated"]
    assert result["depthOf"][f"File:{ENTRY}"] == 0
    assert any(e["type"] == "INSERTS" for e in result["edges"])


def test_trace_clamps_and_truncates(client: TestClient) -> None:
    result = data(client, TRACE, entry=ENTRY, max=5)["trace"]
    assert result["truncated"] and len(result["nodes"]) == 5
    clamped = data(client, TRACE, entry=ENTRY, max=99_999)["trace"]
    assert clamped["summary"]["clamped"] == ["maxNodes: 99999 → 2000"]


def test_unknown_entry_has_suggestions(client: TestClient) -> None:
    body = gql(client, TRACE, entry="cancelarpedido.php", max=10)
    message = body["errors"][0]["message"]
    assert "arquivo de entrada não encontrado no grafo" in message
    assert ENTRY in message


def test_impact(client: TestClient) -> None:
    result = data(
        client, 'query { impact(table: "pedido") { root nodes { id } summary truncated } }'
    )["impact"]
    assert result["root"] == "Table:public.pedido"
    assert result["summary"]["direct"] > 0 and result["summary"]["files"] > 0
    assert f"File:{ENTRY}" in {n["id"] for n in result["nodes"]}


def test_mermaid(client: TestClient) -> None:
    text = data(client, f'query {{ mermaid(kind: "trace", root: "{ENTRY}", maxNodes: 20) }}')
    assert text["mermaid"].startswith("flowchart LR") and ENTRY in text["mermaid"]
    impact = data(client, 'query { mermaid(kind: "impact", root: "public.pedido") }')
    assert impact["mermaid"].startswith("flowchart LR")
    body = gql(client, 'query { mermaid(kind: "outro", root: "x") }')
    assert body["errors"][0]["message"] == "kind aceita trace ou impact"


# --------------------------------------------------------------------------- agregados e stats


def test_modules_tables_screens_and_hotspots(client: TestClient) -> None:
    result = data(
        client,
        """query { modules { module name files ca ce instability }
          module(id: "Module:pedidos") { module { name } outgoing { dst kind samples }
            tables { tableId ownerModules } screens { fileId inMenu } }
          moduleEdges { src dst kind weight }
          tables(shared: false, limit: 500) { tableId shared }
          hotspots(label: "Table", limit: 3) { nodeId rank }
          screens(inMenu: true) { fileId module menuItems } stats }""",
    )
    assert {"Module:pedidos", "Module:estoque"} <= {m["module"] for m in result["modules"]}
    detail = result["module"]
    assert detail["module"]["name"] == "Pedidos"
    assert "Table:public.pedido" in {t["tableId"] for t in detail["tables"]}
    screens = {s["fileId"]: s for s in result["screens"]}
    assert screens[f"File:{ENTRY}"]["module"] == "Module:pedidos"
    assert len(result["hotspots"]) <= 3
    stats = result["stats"]
    assert stats["meta"]["source"].endswith("loja-mini") and stats["nodes"]
    assert stats["analytics"]["modules"] == 3  # Pedidos, Estoque e "(sem módulo)"
    assert data(client, 'query { module(id: "Module:nao-existe") { module { name } } }') == {
        "module": None
    }


def test_limits_are_clamped(client: TestClient) -> None:
    result = data(client, "query { tables(limit: 100000) { tableId } }")
    assert len(result["tables"]) <= 500


def test_query_depth_is_limited(client: TestClient) -> None:
    deep = (
        f'query {{ node(id: "File:{ENTRY}") {{ neighbors {{ nodes {{'
        " neighbors { nodes { neighbors { nodes { neighbors { nodes { id } } } } } } } } } }"
    )
    body = gql(client, deep)
    assert "exceeds maximum operation depth" in body["errors"][0]["message"]


# --------------------------------------------------------------------------- código-fonte


def test_workspace_and_source_file(client: TestClient) -> None:
    result = data(
        client,
        f"""query {{ workspace {{ source origin exists isEcidade graphSource }}
          sourceFile(path: "{ENTRY}", line: 3) {{ path totalLines startLine
            highlight truncated lines }} }}""",
    )
    workspace = result["workspace"]
    assert workspace["origin"] == "grafo" and workspace["exists"]
    source = result["sourceFile"]
    assert (source["startLine"], source["highlight"], source["truncated"]) == (1, 3, False)
    assert len(source["lines"]) == source["totalLines"] > 3
    body = gql(client, 'query { sourceFile(path: "../pyproject.toml") { path } }')
    assert "fora da pasta" in body["errors"][0]["message"]


def test_source_argument_wins(shop_db: Path, tmp_path: Path) -> None:
    other = tmp_path / "outra"
    other.mkdir()
    client = TestClient(create_app(shop_db, ui_dir=None, source=other), base_url=LOCAL)
    workspace = data(client, "query { workspace { origin isEcidade } }")["workspace"]
    assert workspace == {"origin": "argumento", "isEcidade": False}


# --------------------------------------------------------------------------- servidor


def test_host_must_be_local(shop_db: Path) -> None:
    app = create_app(shop_db, ui_dir=None)
    for base in ("http://exemplo.com", "http://192.168.0.10:8765"):
        response = TestClient(app, base_url=base).post("/graphql", json={"query": "{ stats }"})
        assert response.status_code == 400
        assert TestClient(app, base_url=base).get("/").status_code == 400
    for base in (LOCAL, "http://localhost:8765", "http://localhost"):
        assert TestClient(app, base_url=base).get("/").status_code == 200


def test_without_ui_dist_shows_build_instructions(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200 and "npm run build" in response.text
    assert client.post("/graphql", json={"query": "{ stats }"}).status_code == 200


def test_serves_ui_dist_with_spa_fallback(shop_db: Path, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    (dist / "assets/app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "segredo.txt").write_text("fora", encoding="utf-8")
    client = TestClient(create_app(shop_db, ui_dir=dist), base_url=LOCAL)

    assert client.get("/assets/app.js").text == "console.log(1)"
    assert "id=root" in client.get(f"/no/File%3A{ENTRY}").text
    assert "fora" not in client.get("/..%2fsegredo.txt").text
    assert client.post("/graphql", json={"query": "{ stats }"}).status_code == 200


def test_cors_only_for_vite(client: TestClient) -> None:
    preflight = {"Access-Control-Request-Method": "POST"}
    vite = client.options("/graphql", headers={**preflight, "Origin": "http://localhost:5173"})
    other = client.options("/graphql", headers={**preflight, "Origin": "http://exemplo.com"})
    assert vite.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in other.headers


def test_print_schema_matches_the_served_one() -> None:
    result = CliRunner().invoke(cli_app, ["ui", "--print-schema"])
    assert result.exit_code == 0
    committed = (Path(__file__).resolve().parents[2] / "ui/schema.graphql").read_text("utf-8")
    assert committed.strip() == schema.as_str().strip()


# --------------------------------------------------------------------------- arquivos e menus


def test_find_files(client: TestClient) -> None:
    query = "query($t: String!) { findFiles(text: $t) }"
    assert data(client, query, t="cancelarpedido")["findFiles"] == [
        f"File:{ENTRY}",
        "File:forms/db_frmcancelarpedido.php",
    ]
    assert data(client, query, t="a")["findFiles"] == []


def test_menus_for_file(client: TestClient) -> None:
    got = data(client, MENUS_QUERY, file="classes/db_pedido_classe.php", d=3)["menusForFile"]

    assert got["file"] == "File:classes/db_pedido_classe.php" and got["candidates"] == []
    assert got["total"] == got["unfiltered"] and not got["truncated"]
    assert got["total"] >= 1 and got["byDistance"][0]["distance"] >= 1
    menus = {i["menu"]: i for i in got["items"]}
    assert "BusinessAction:menu:7001" in menus
    assert (
        menus["BusinessAction:menu:7001"]["via"][-1]["node"] == "File:classes/db_pedido_classe.php"
    )
    inactive = menus["BusinessAction:menu:7002"]
    assert inactive["active"] is False
    assert inactive["via"][0]["node"] == "File:ped4_baixaestoque004.php"
    text = data(client, MENUS_QUERY, file="classes/db_pedido_classe.php", d=3, text="cancelar")
    assert [i["menu"] for i in text["menusForFile"]["items"]] == ["BusinessAction:menu:7001"]
    estoque = data(
        client, MENUS_QUERY, file="classes/db_pedido_classe.php", d=3, module="Module:estoque"
    )["menusForFile"]
    assert [i["menu"] for i in estoque["items"]] == ["BusinessAction:menu:7003"]
    assert estoque["items"][0]["modules"] == [
        {"module": "Module:estoque", "name": "Estoque"},
        {"module": "Module:pedidos", "name": "Pedidos"},
    ]


def test_menus_for_file_ambiguous_missing_and_clamped(client: TestClient) -> None:
    ambiguous = data(client, MENUS_QUERY, file="cancelarpedido", d=3)["menusForFile"]
    assert ambiguous["file"] is None and ambiguous["items"] == []
    assert ambiguous["candidates"] == [f"File:{ENTRY}", "File:forms/db_frmcancelarpedido.php"]
    missing = data(client, MENUS_QUERY, file="naoexiste.php", d=3)["menusForFile"]
    assert missing["file"] is None and missing["candidates"] == []
    clamped = data(client, MENUS_QUERY, file="classes/db_pedido_classe.php", d=99)
    assert clamped["menusForFile"]["maxDistance"] == 10
    assert clamped["menusForFile"]["clamped"] == ["maxDistance: 99 → 10"]
