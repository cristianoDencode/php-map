"""A API GraphQL do acesso (tokens, usuários e estado do MCP)."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from formula_graphs import build_db
from login_helpers import sign_in

from php_map.access import tokens, users
from php_map.access.store import Vault, open_vault
from php_map.web.app import create_app

LOCAL = "http://127.0.0.1:8765"
PASSWORD = "uma-senha-longa"
ME = (
    "{ mcpStatus { available reason path localUrl publicUrls exposeSource activeTokens"
    " legacyToken vaultAvailable } }"
)


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return open_vault(tmp_path / "cofre" / "access.db")


def _client(
    tmp_path: Path, vault: Vault | None, *, login: bool = True, **kwargs: Any
) -> TestClient:
    app = create_app(
        build_db(tmp_path), ui_dir=None, out_dir=tmp_path / "out", vault=vault, **kwargs
    )
    client = TestClient(app, base_url=LOCAL)
    if vault is not None and login:
        sign_in(client, vault)
    return client


def _gql(client: TestClient, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    response = client.post(
        "/graphql", json={"query": query, "variables": variables or {}}, headers={"origin": LOCAL}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _error(body: dict[str, Any]) -> str:
    return str(body["errors"][0]["message"])


def _login(client: TestClient, name: str = "ana@exemplo.com") -> None:
    resp = client.post(
        "/auth/login", json={"username": name, "password": PASSWORD}, headers={"origin": LOCAL}
    )
    assert resp.status_code == 204


def test_create_token_shows_the_value_once_and_list_hides_it(tmp_path: Path, vault: Vault) -> None:
    client = _client(tmp_path, vault)
    made = _gql(
        client,
        "mutation($n: String!) { createAccessToken(name: $n, expiresDays: 30)"
        " { token info { id prefix state expiresAt createdBy } } }",
        {"n": "meu-notebook"},
    )
    assert "errors" not in made, made
    data = made["data"]["createAccessToken"]
    raw = data["token"]
    assert raw.startswith("ecm_") and data["info"]["prefix"] == raw[:12]
    assert data["info"]["state"] == "ativo" and data["info"]["expiresAt"] is not None
    assert tokens.verify_token(vault, raw) is not None
    listed = _gql(client, "{ accessTokens { id name prefix state lastUsedAt lastUsedIp } }")
    assert raw not in str(listed) and listed["data"]["accessTokens"][0]["name"] == "meu-notebook"
    schema_fields = _gql(client, '{ __type(name: "AccessToken") { fields { name } } }')
    names = {f["name"] for f in schema_fields["data"]["__type"]["fields"]}
    assert not names & {"token", "tokenHash", "hash"}


def test_token_validity_choices_and_never(tmp_path: Path, vault: Vault) -> None:
    client = _client(tmp_path, vault)
    query = (
        'mutation($d: Int) { createAccessToken(name: "x", expiresDays: $d) { info { expiresAt } } }'
    )
    assert _error(_gql(client, query, {"d": 7})).startswith("validade inválida")
    never = _gql(client, query, {"d": None})
    assert never["data"]["createAccessToken"]["info"]["expiresAt"] is None
    assert "errors" in _gql(client, 'mutation { createAccessToken(name: " ") { token } }')


def test_revoke_token(tmp_path: Path, vault: Vault) -> None:
    client = _client(tmp_path, vault)
    raw, info = tokens.create_token(vault, "a")
    done = _gql(
        client, "mutation($i: Int!) { revokeAccessToken(id: $i) { state } }", {"i": info.id}
    )
    assert done["data"]["revokeAccessToken"]["state"] == "revogado"
    assert tokens.verify_token(vault, raw) is None
    assert "errors" in _gql(client, "mutation { revokeAccessToken(id: 999) { state } }")


def test_users_flow_and_protections(tmp_path: Path, vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    client = _client(tmp_path, vault)
    _login(client)
    add = _gql(
        client,
        'mutation { addAccessUser(username: "bia@exemplo.com", password: "outra-senha-longa")'
        " { username active isCurrent } }",
    )
    assert add["data"]["addAccessUser"] == {
        "username": "bia@exemplo.com",
        "active": True,
        "isCurrent": False,
    }
    short = _gql(
        client, 'mutation { addAccessUser(username: "c", password: "curta") { username } }'
    )
    assert "e-mail inválido" in _error(short)
    no_at = _gql(
        client,
        'mutation { addAccessUser(username: "cris", password: "uma-senha-longa") { username } }',
    )
    assert "e-mail inválido" in _error(no_at)
    weak = _gql(
        client,
        'mutation { addAccessUser(username: "cris@exemplo.com", password: "curta") { username } }',
    )
    assert "10 caracteres" in _error(weak)
    me = _gql(client, "{ accessUsers { username isCurrent } }")["data"]["accessUsers"]
    assert {u["username"]: u["isCurrent"] for u in me} == {
        "ana@exemplo.com": True,
        "bia@exemplo.com": False,
    }
    own = _gql(client, 'mutation { disableAccessUser(username: "ana@exemplo.com") { active } }')
    assert "próprio usuário" in _error(own)
    off = _gql(client, 'mutation { disableAccessUser(username: "bia@exemplo.com") { active } }')
    assert off["data"]["disableAccessUser"]["active"] is False
    back = _gql(client, 'mutation { enableAccessUser(username: "bia@exemplo.com") { active } }')
    assert back["data"]["enableAccessUser"]["active"] is True
    reset = _gql(
        client,
        'mutation { resetAccessUserPassword(username: "bia@exemplo.com", password: "terceira-senha-123")'  # noqa: E501
        " { username } }",
    )
    assert "errors" not in reset
    assert users.authenticate(vault, "bia@exemplo.com", "terceira-senha-123") is not None


def test_mcp_status_and_source_switch(tmp_path: Path, vault: Vault) -> None:
    client = _client(tmp_path, vault, public_hosts=["x.ngrok-free.app"])
    tokens.create_token(vault, "a")
    status = _gql(client, ME)["data"]["mcpStatus"]
    assert status["available"] is True and status["path"] == "/mcp"
    assert status["localUrl"] == "http://127.0.0.1:8765/mcp"
    assert status["publicUrls"] == ["https://x.ngrok-free.app/mcp"]
    assert status["exposeSource"] is False and status["activeTokens"] == 1
    assert status["vaultAvailable"] is True
    on = _gql(client, "mutation { setMcpExposeSource(expose: true) { exposeSource } }")
    assert on["data"]["setMcpExposeSource"]["exposeSource"] is True
    assert client.app.state.ecmap.mcp_expose_source is True  # type: ignore[attr-defined]


def test_mutations_need_local_origin_and_read_only_turns_them_off(
    tmp_path: Path, vault: Vault
) -> None:
    client = _client(tmp_path, vault)
    external = client.post(
        "/graphql",
        json={"query": 'mutation { createAccessToken(name: "x") { token } }'},
        headers={"origin": "https://evil.example"},
    )
    assert "errors" in external.json() and tokens.list_tokens(vault) == []
    ro = _client(tmp_path, vault, read_only=True)
    assert "errors" in _gql(ro, 'mutation { createAccessToken(name: "x") { token } }')
    assert tokens.list_tokens(vault) == []
    assert "errors" not in _gql(ro, "{ accessTokens { id } }")  # consulta continua


def test_without_vault_the_queries_explain(tmp_path: Path) -> None:
    client = _client(tmp_path, None)
    assert "não está ligado" in _error(_gql(client, "{ accessTokens { id } }"))
    status = _gql(client, ME)["data"]["mcpStatus"]
    assert status["vaultAvailable"] is False and status["available"] is False


def test_access_api_needs_the_session_when_login_is_required(tmp_path: Path, vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    client = _client(tmp_path, vault, login=False)
    assert client.post("/graphql", json={"query": "{ accessTokens { id } }"}).status_code == 401
    _login(client)
    assert "errors" not in _gql(client, "{ accessTokens { id } }")
