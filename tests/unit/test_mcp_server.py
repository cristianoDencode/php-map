"""O servidor MCP de verdade (SDK `mcp` 2.x): cliente em memória, transporte HTTP com
bearer e allowlist de `Host`, e as regras de uso do comando `php-map mcp`."""

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from cross_graphs import GET_A, META_CHAMA, META_SERVE, build_chama, build_serve
from cross_graphs import save as _save
from starlette.testclient import TestClient
from typer.testing import CliRunner

from php_map import config
from php_map.cli import app
from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.mcp.catalog import ProjectCatalog
from php_map.search.index import build_documents, write_index

mcp = pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

from php_map.mcp import server as mcp_server  # noqa: E402

TOOLS = {
    "list_projects",
    "search",
    "node",
    "neighbors",
    "trace",
    "impact",
    "communication",
    "endpoint",
    "cross_summary",
    "source",
    "cut_plan",
    "module_boundary",
    "extraction_candidates",
    "routine",
    "routine_origins",
    "routine_rules",
    "column_usage",
    "routine_card",
    "screen_card",
    "briefing_templates",
    "briefing",
    "value_origin",
    "formulas",
    "field_search",
    "formula_search",
    "layers",
}
TOKEN = "t" * 32
runner = CliRunner()


def _indexed(tmp_path: Path, name: str, builder: GraphBuilder, meta: dict[str, str]) -> Path:
    db = _save(tmp_path, name, builder, meta)
    docs, stats = build_documents(lambda: [], builder, Settings())
    write_index(db, docs, stats)
    return db


@pytest.fixture
def dbs(tmp_path: Path) -> list[Path]:
    return [
        _indexed(tmp_path, "serve", build_serve(), META_SERVE),
        _indexed(tmp_path, "chama", build_chama(), META_CHAMA),
    ]


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "ontologia.md").write_text("# Ontologia\n", encoding="utf-8")
    return root


def _payload(result: Any) -> dict[str, Any]:
    if result.structured_content is not None:
        data = result.structured_content
        return data.get("result", data) if isinstance(data, dict) else data
    return json.loads(result.content[0].text)


# --------------------------------------------------------------------------- em memória


def test_in_memory_client_lists_tools_resources_and_calls(dbs: list[Path], docs: Path) -> None:
    server = mcp_server.build_server(ProjectCatalog.from_dbs(dbs), docs_dir=docs)

    async def scenario() -> None:
        async with Client(server) as client:
            listed = await client.list_tools()
            assert {t.name for t in listed.tools} == TOOLS
            assert all(t.annotations and t.annotations.read_only_hint for t in listed.tools)
            uris = {str(r.uri) for r in (await client.list_resources()).resources}
            # só os documentos que existem na pasta, mais o guia
            assert uris == {"php-map://guia", "php-map://docs/ontologia"}
            ontology = await client.read_resource("php-map://docs/ontologia")
            assert ontology.contents[0].text == "# Ontologia\n"  # type: ignore[union-attr]
            guide = await client.read_resource("php-map://guia")
            assert "confidence" in guide.contents[0].text  # type: ignore[union-attr]
            found = await client.call_tool("search", {"query": "getA", "project": "ecidade-t"})
            assert not found.is_error and _payload(found)["hits"][0]["node_id"] == GET_A
            traced = await client.call_tool(
                "trace",
                {"entry": "application/modules/fiscal/views/scripts/a.phtml", "project": "zend-t"},
            )
            assert GET_A in {c["endpoint"] for c in _payload(traced)["remote_calls"]}
            wrong = await client.call_tool("search", {"query": "x", "project": "outro"})
            assert wrong.is_error and "projeto desconhecido" in wrong.content[0].text  # type: ignore[union-attr]

    anyio.run(scenario)


def test_default_docs_dir_is_the_repository_docs(dbs: list[Path]) -> None:
    folder = mcp_server.default_docs_dir()
    assert folder is not None and (folder / "ontologia.md").is_file()
    server = mcp_server.build_server(ProjectCatalog.from_dbs(dbs))

    async def scenario() -> None:
        async with Client(server) as client:
            uris = {str(r.uri) for r in (await client.list_resources()).resources}
            assert "php-map://docs/ontologia" in uris

    anyio.run(scenario)


def test_timeout_becomes_a_tool_error(dbs: list[Path], monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from php_map.mcp import tools

    monkeypatch.setattr(mcp_server, "TOOL_TIMEOUT", 0.05)
    monkeypatch.setattr(tools, "list_projects", lambda catalog: time.sleep(0.5) or {})
    server = mcp_server.build_server(ProjectCatalog.from_dbs(dbs))

    async def scenario() -> None:
        async with Client(server) as client:
            result = await client.call_tool("list_projects", {})
            assert result.is_error and "reduza depth" in result.content[0].text  # type: ignore[union-attr]

    anyio.run(scenario)


# --------------------------------------------------------------------------- HTTP

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "teste", "version": "0"},
    },
}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def _client(dbs: list[Path], token: str | None, hosts: list[str]) -> TestClient:
    server = mcp_server.build_server(ProjectCatalog.from_dbs(dbs))
    return TestClient(
        mcp_server.http_app(server, host="127.0.0.1", allowed_hosts=hosts, token=token),
        base_url="http://testserver",
    )


def test_http_requires_the_bearer(dbs: list[Path]) -> None:
    with _client(dbs, TOKEN, ["testserver"]) as client:
        assert client.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
        wrong = {**HEADERS, "authorization": "Bearer " + "x" * 32}
        assert client.post("/mcp", json=INIT, headers=wrong).status_code == 401
        basic = {**HEADERS, "authorization": "Basic " + TOKEN}
        assert client.post("/mcp", json=INIT, headers=basic).status_code == 401
        ok = client.post("/mcp", json=INIT, headers={**HEADERS, "authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["result"]["serverInfo"]["name"] == "php-map"
        assert TOKEN not in ok.text


def test_http_refuses_unknown_host(dbs: list[Path]) -> None:
    headers = {**HEADERS, "authorization": f"Bearer {TOKEN}"}
    with _client(dbs, TOKEN, []) as client:  # "testserver" fora da lista
        assert client.post("/mcp", json=INIT, headers=headers).status_code in (400, 403, 421)
    with _client(dbs, TOKEN, ["mcp.exemplo.com.br"]) as client:
        public = {**headers, "host": "mcp.exemplo.com.br"}
        assert client.post("/mcp", json=INIT, headers=public).status_code == 200


def test_short_token_is_refused(dbs: list[Path]) -> None:
    with pytest.raises(ValueError, match="16 caracteres"):
        _client(dbs, "curto", [])


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHP_MAP_MCP_TOKEN", raising=False)
    monkeypatch.setattr(config, "DOTENV_PATHS", ())


@pytest.mark.usefixtures("no_token")
@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "--projects ou --db"),
        (["--transport", "sse"], "stdio ou http"),
        (["--transport", "http"], "PHP_MAP_MCP_TOKEN"),
        (["--transport", "http", "--insecure-no-auth", "--host", "0.0.0.0"], "insecure"),
    ],
)
def test_cli_usage_errors_exit_2(dbs: list[Path], args: list[str], message: str) -> None:
    extra = [] if not args else ["--db", str(dbs[0])]
    result = runner.invoke(app, ["mcp", *extra, *args])
    assert result.exit_code == 2, result.output
    assert message in result.output.replace("\n", " ")


@pytest.mark.usefixtures("no_token")
def test_cli_without_valid_database_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(app, ["mcp", "--db", str(tmp_path / "nada.db")])
    assert result.exit_code == 1 and "nenhum projeto" in result.output
