"""O MCP montado em `/mcp` da interface, o `HostGate` e o catálogo dinâmico."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from formula_graphs import build_db
from login_helpers import sign_in

from php_map.access import sessions, tokens, users
from php_map.access.store import Vault, open_vault
from php_map.mcp.catalog import ProjectCatalog
from php_map.web.app import create_app
from php_map.web.mcp_mount import interface_projects

pytest.importorskip("mcp")

LOCAL = "http://127.0.0.1:8765"
PUBLIC = "exemplo.ngrok-free.app"
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
CALL = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return open_vault(tmp_path / "cofre" / "access.db")


@pytest.fixture
def token(vault: Vault) -> str:
    return tokens.create_token(vault, "teste")[0]


def _app(tmp_path: Path, vault: Vault | None, **kwargs: Any) -> Any:
    return create_app(
        build_db(tmp_path), ui_dir=None, out_dir=tmp_path / "out", vault=vault, **kwargs
    )


def _mcp(client: TestClient, bearer: str | None, **headers: str) -> Any:
    auth = {"authorization": f"Bearer {bearer}"} if bearer else {}
    return client.post("/mcp", json=INIT, headers={**CALL, **auth, **headers})


def _tool(client: TestClient, bearer: str, name: str, arguments: dict[str, Any]) -> Any:
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    headers = {**CALL, "authorization": f"Bearer {bearer}"}
    return client.post("/mcp", json=call, headers=headers).json()["result"]


def test_mcp_answers_with_the_vault_token_and_refuses_the_rest(
    tmp_path: Path, vault: Vault, token: str
) -> None:
    users.add_user(vault, "ana@exemplo.com", "uma-senha-longa")
    with TestClient(_app(tmp_path, vault), base_url=LOCAL) as client:
        ok = _mcp(client, token)
        assert ok.status_code == 200 and ok.json()["result"]["serverInfo"]["name"] == "php-map"
        assert token not in ok.text
        assert _mcp(client, None).status_code == 401
        assert _mcp(client, "ecm_" + "x" * 43).status_code == 401
        # o cookie do login não vale no /mcp
        cookie = sessions.create_session(vault, users.list_users(vault)[0].id)
        client.cookies.set("ecm_session", cookie)
        assert _mcp(client, None).status_code == 401


def test_mcp_path_with_and_without_trailing_slash(tmp_path: Path, vault: Vault, token: str) -> None:
    with TestClient(_app(tmp_path, vault), base_url=LOCAL) as client:
        headers = {**CALL, "authorization": f"Bearer {token}"}
        assert client.post("/mcp", json=INIT, headers=headers).status_code == 200
        assert client.post("/mcp/", json=INIT, headers=headers).status_code == 200


def test_list_projects_shows_the_interface_graph(tmp_path: Path, vault: Vault, token: str) -> None:
    with TestClient(_app(tmp_path, vault), base_url=LOCAL) as client:
        result = _tool(client, token, "list_projects", {})
        assert not result.get("isError")
        text = str(result.get("structuredContent", result))
        assert interface_projects(client.app.state.ecmap)[0].slug in text


def test_prompts_work_on_the_server_the_interface_mounts(
    tmp_path: Path, vault: Vault, token: str
) -> None:
    """Os prompts de relatório (`mcp/prompts.py`) existem no mesmo servidor que
    `/mcp` monta, não só no construído direto por `build_server` nos testes de
    `test_mcp_prompts.py`."""
    with TestClient(_app(tmp_path, vault), base_url=LOCAL) as client:
        headers = {**CALL, "authorization": f"Bearer {token}"}
        listed = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "prompts/list", "params": {}},
            headers=headers,
        ).json()["result"]
        names = {p["name"] for p in listed["prompts"]}
        assert {"relatorio-analise-profunda", "buscar-por-assunto"} <= names
        slug = interface_projects(client.app.state.ecmap)[0].slug
        got = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "prompts/get",
                "params": {
                    "name": "relatorio-analise-profunda",
                    "arguments": {"projeto": slug, "alvo": "fc_issqn"},
                },
            },
            headers=headers,
        ).json()["result"]
        text = got["messages"][0]["content"]["text"]
        assert slug in text and "fc_issqn" in text and "formulas(" in text


def test_revoking_a_token_stops_the_client_at_once(
    tmp_path: Path, vault: Vault, token: str
) -> None:
    info = tokens.list_tokens(vault)[0]
    with TestClient(_app(tmp_path, vault), base_url=LOCAL) as client:
        assert _mcp(client, token).status_code == 200
        tokens.revoke_token(vault, str(info.id))
        assert _mcp(client, token).status_code == 401


def test_legacy_token_also_works(
    tmp_path: Path, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = "legado-" + "z" * 20
    monkeypatch.setenv("PHP_MAP_MCP_TOKEN", legacy)
    with TestClient(_app(tmp_path, vault), base_url=LOCAL) as client:
        assert _mcp(client, legacy).status_code == 200
        assert _mcp(client, "outro-" + "q" * 20).status_code == 401


def test_without_tokens_everything_is_401(tmp_path: Path, vault: Vault) -> None:
    with TestClient(_app(tmp_path, vault), base_url=LOCAL) as client:
        assert _mcp(client, "ecm_" + "x" * 43).status_code == 401


def test_without_vault_the_mcp_is_off(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path, None), base_url=LOCAL) as client:
        off = client.post("/mcp", json=INIT, headers=CALL)
        assert off.status_code == 503 and "desligado" in off.json()["error"]


def test_no_mcp_flag_turns_it_off(tmp_path: Path, vault: Vault, token: str) -> None:
    with TestClient(_app(tmp_path, vault, mcp=False), base_url=LOCAL) as client:
        assert _mcp(client, token).status_code == 503


# --------------------------------------------------------------------------- HostGate


def test_public_host_reaches_only_mcp(tmp_path: Path, vault: Vault, token: str) -> None:
    app = _app(tmp_path, vault, public_hosts=[PUBLIC])
    with TestClient(app, base_url=f"https://{PUBLIC}") as client:
        assert client.post("/graphql", json={"query": "{ stats }"}).status_code == 404
        assert client.get("/").status_code == 404
        assert client.get("/auth/me").status_code == 404
        ok = _mcp(client, token)
        assert ok.status_code == 200, ok.text
        assert _mcp(client, None).status_code == 401


def test_forwarded_headers_make_a_local_request_remote(tmp_path: Path, vault: Vault) -> None:
    with TestClient(_app(tmp_path, vault), base_url=LOCAL) as client:
        sign_in(client, vault)
        for header in ("x-forwarded-for", "x-forwarded-host", "forwarded"):
            denied = client.post(
                "/graphql", json={"query": "{ stats }"}, headers={header: "1.2.3.4"}
            )
            assert denied.status_code == 404, header
        assert client.post("/graphql", json={"query": "{ stats }"}).status_code == 200


def test_unknown_host_is_400_and_public_host_is_dynamic(
    tmp_path: Path, vault: Vault, token: str
) -> None:
    app = _app(tmp_path, vault)
    with TestClient(app, base_url=LOCAL) as client:
        assert _mcp(client, token, host="intruso.example").status_code == 400
        assert _mcp(client, token, host="novo.exemplo.app").status_code == 400
        app.state.ecmap.add_public_host("novo.exemplo.app")  # o túnel sobe depois
        assert _mcp(client, token, host="novo.exemplo.app").status_code == 200
        gone = client.get("/graphql", headers={"host": "novo.exemplo.app"})
        assert gone.status_code == 404


def test_remote_refusal_is_logged_with_the_forwarded_ip(
    tmp_path: Path, vault: Vault, caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(_app(tmp_path, vault), base_url=LOCAL) as client, caplog.at_level("WARNING"):
        client.get("/graphql", headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1"})
    assert "acesso remoto recusado" in caplog.text and "9.9.9.9" in caplog.text
    assert "10.0.0.1" not in caplog.text


# --------------------------------------------------------------------------- source


def test_source_is_off_until_the_analyst_turns_it_on(
    tmp_path: Path, vault: Vault, token: str
) -> None:
    folder = tmp_path / "ecidade"
    folder.mkdir()
    (folder / "a.php").write_text("<?php echo 1;\n", encoding="latin-1")
    db = build_db(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'source'", (str(folder),))
    app = create_app(db, ui_dir=None, out_dir=tmp_path / "out", vault=vault)
    state = app.state.ecmap
    with TestClient(app, base_url=LOCAL) as client:
        slug = interface_projects(state)[0].slug
        args = {"project": slug, "path": "a.php"}
        off = _tool(client, token, "source", args)
        assert off["isError"] and "fonte não disponível" in str(off)
        state.mcp_expose_source = True
        on = _tool(client, token, "source", args)
        assert not on.get("isError"), on
        assert "echo 1" in str(on)
        state.mcp_expose_source = False
        assert _tool(client, token, "source", args)["isError"]


# --------------------------------------------------------------------------- catálogo dinâmico


def test_dynamic_catalog_follows_the_provider(tmp_path: Path) -> None:
    db = build_db(tmp_path)
    current: list[Any] = []
    catalog = ProjectCatalog.dynamic(lambda: current)
    assert catalog.projects == []
    from php_map.graph import store
    from php_map.mcp.catalog import CatalogProject

    meta = store.load_meta(db)
    current.append(CatalogProject("p1", "P1", "ecidade", db, None, meta))
    assert [p.slug for p in catalog.projects] == ["p1"]
    assert catalog.get("p1").db == db
    from php_map.errors import QueryError

    with pytest.raises(QueryError, match="projeto desconhecido"):
        catalog.get("outro")
    current.clear()
    with pytest.raises(QueryError, match="nenhum projeto com grafo"):
        catalog.get("p1")
