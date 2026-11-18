"""Papéis `admin` e `leitor` na API: o leitor consulta tudo e não muda nada."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from formula_graphs import build_db

from php_map.access import tokens, users
from php_map.access.store import Vault, open_vault
from php_map.web.app import create_app
from php_map.web.permissions import ADMIN_ONLY, READER_MUTATIONS
from php_map.web.schema import schema

LOCAL = "http://127.0.0.1:8765"
PASSWORD = "uma-senha-longa"


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    vault = open_vault(tmp_path / "cofre" / "access.db")
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    users.add_user(vault, "bia@exemplo.com", PASSWORD, "leitor")
    return vault


def _client(tmp_path: Path, vault: Vault | None, name: str | None) -> TestClient:
    app = create_app(build_db(tmp_path), ui_dir=None, out_dir=tmp_path / "out", vault=vault)
    client = TestClient(app, base_url=LOCAL)
    if name is not None:
        resp = client.post(
            "/auth/login", json={"username": name, "password": PASSWORD}, headers={"origin": LOCAL}
        )
        assert resp.status_code == 204
    return client


def _gql(client: TestClient, query: str, **extra: Any) -> dict[str, Any]:
    response = client.post("/graphql", json={"query": query, **extra}, headers={"origin": LOCAL})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _refused(body: dict[str, Any]) -> bool:
    errors = body.get("errors") or []
    return bool(errors) and errors[0]["message"] == ADMIN_ONLY


def test_reader_queries_everything_but_cannot_mutate(tmp_path: Path, vault: Vault) -> None:
    client = _client(tmp_path, vault, "bia@exemplo.com")
    assert "errors" not in _gql(client, "{ stats }")
    assert "errors" not in _gql(client, "{ accessUsers { username role } }")
    body = _gql(client, 'mutation { setSource(path: "/tmp") { path } }')
    assert _refused(body)
    assert body["errors"][0]["extensions"]["code"] == "FORBIDDEN"
    assert body.get("data") is None
    me = client.get("/auth/me").json()
    assert me["user"] == {"username": "bia@exemplo.com", "role": "leitor"}


def test_admin_runs_the_same_mutation(tmp_path: Path, vault: Vault) -> None:
    client = _client(tmp_path, vault, "ana@exemplo.com")
    body = _gql(client, 'mutation { setSource(path: "/nao/existe/mesmo") { path } }')
    assert body["errors"] and not _refused(body)  # passou pela permissão e falhou na pasta


def test_every_mutation_of_the_schema_is_refused_to_the_reader(
    tmp_path: Path, vault: Vault
) -> None:
    """Enumera o schema: uma mutação nova nasce restrita sem ninguém lembrar de barrá-la."""
    mutation_type = schema._schema.mutation_type
    assert mutation_type is not None
    names = sorted(mutation_type.fields)
    assert {"setSource", "startScan", "createAccessToken", "addAccessUser"} <= set(names)
    assert set(READER_MUTATIONS) <= set(names)
    reader = _client(tmp_path, vault, "bia@exemplo.com")
    admin = _client(tmp_path, vault, "ana@exemplo.com")
    for name in names:
        if name in READER_MUTATIONS:
            continue
        assert _refused(_gql(reader, f"mutation {{ {name} }}")), name
        assert not _refused(_gql(admin, f"mutation {{ {name} }}")), name  # o controle


@pytest.mark.parametrize(
    "query",
    [
        'mutation { renamed: setSource(path: "/tmp") { path } }',
        'mutation { ...Go } fragment Go on Mutation { setSource(path: "/tmp") { path } }',
        'mutation { ... on Mutation { setSource(path: "/tmp") { path } } }',
        'mutation { __typename setSource(path: "/tmp") { path } }',
        'mutation Go { setSource(path: "/tmp") { path } }',
    ],
)
def test_the_reader_cannot_get_around_the_check(tmp_path: Path, vault: Vault, query: str) -> None:
    client = _client(tmp_path, vault, "bia@exemplo.com")
    assert _refused(_gql(client, query))


def test_a_query_next_to_a_mutation_in_the_same_document_is_refused(
    tmp_path: Path, vault: Vault
) -> None:
    client = _client(tmp_path, vault, "bia@exemplo.com")
    query = 'query Q { stats } mutation M { setSource(path: "/tmp") { path } }'
    assert _refused(_gql(client, query, operationName="Q"))


def test_a_reader_cannot_create_users_or_tokens(tmp_path: Path, vault: Vault) -> None:
    client = _client(tmp_path, vault, "bia@exemplo.com")
    add = 'mutation { addAccessUser(username: "cris@exemplo.com", password: "outra-senha-longa") { role } }'  # noqa: E501
    assert _refused(_gql(client, add))
    assert users.get_user(vault, "cris@exemplo.com") is None
    assert _refused(_gql(client, 'mutation { createAccessToken(name: "x") { token } }'))
    assert tokens.list_tokens(vault) == []
    promote = 'mutation { setAccessUserRole(username: "bia@exemplo.com", role: "admin") { role } }'
    assert _refused(_gql(client, promote))
    user = users.get_user(vault, "bia@exemplo.com")
    assert user is not None and user.role == "leitor"


def test_the_role_change_reaches_the_open_session(tmp_path: Path, vault: Vault) -> None:
    client = _client(tmp_path, vault, "bia@exemplo.com")
    assert _refused(_gql(client, 'mutation { setSource(path: "/nao/existe") { path } }'))
    users.set_role(vault, "bia@exemplo.com", "admin")
    body = _gql(client, 'mutation { setSource(path: "/nao/existe") { path } }')
    assert body["errors"] and not _refused(body)
    users.set_role(vault, "bia@exemplo.com", "leitor")
    assert _refused(_gql(client, 'mutation { setSource(path: "/tmp") { path } }'))


def test_without_a_vault_the_caller_is_admin(tmp_path: Path) -> None:
    client = _client(tmp_path, None, None)
    body = _gql(client, 'mutation { setSource(path: "/nao/existe/mesmo") { path } }')
    assert body["errors"] and not _refused(body)


def test_admin_manages_roles_through_the_api(tmp_path: Path, vault: Vault) -> None:
    client = _client(tmp_path, vault, "ana@exemplo.com")
    listed = _gql(client, "{ accessUsers { username role } }")["data"]["accessUsers"]
    assert {u["username"]: u["role"] for u in listed} == {
        "ana@exemplo.com": "admin",
        "bia@exemplo.com": "leitor",
    }
    created = _gql(
        client,
        'mutation { addAccessUser(username: "cris@exemplo.com", password: "outra-senha-longa") { role } }',  # noqa: E501
    )
    assert created["data"]["addAccessUser"]["role"] == "leitor"  # sem `role`, o mais restrito
    as_admin = _gql(
        client,
        'mutation { addAccessUser(username: "dani@exemplo.com", password: "outra-senha-longa", role: "admin")'  # noqa: E501
        " { role } }",
    )
    assert as_admin["data"]["addAccessUser"]["role"] == "admin"
    promoted = _gql(
        client,
        'mutation { setAccessUserRole(username: "bia@exemplo.com", role: "admin") { role } }',
    )
    assert promoted["data"]["setAccessUserRole"]["role"] == "admin"
    bad = _gql(
        client, 'mutation { setAccessUserRole(username: "bia@exemplo.com", role: "dono") { role } }'
    )
    assert "papel inválido" in bad["errors"][0]["message"]


def test_the_api_keeps_the_last_admin(tmp_path: Path, vault: Vault) -> None:
    client = _client(tmp_path, vault, "ana@exemplo.com")
    demote = _gql(
        client,
        'mutation { setAccessUserRole(username: "ana@exemplo.com", role: "leitor") { role } }',
    )
    assert "último administrador" in demote["errors"][0]["message"]
    own = _gql(client, 'mutation { disableAccessUser(username: "ana@exemplo.com") { active } }')
    assert "errors" in own
    user = users.get_user(vault, "ana@exemplo.com")
    assert user is not None and user.role == "admin" and user.active
    # Outro administrador ativo: aí o primeiro pode sair.
    users.set_role(vault, "bia@exemplo.com", "admin")
    ok = _gql(
        client,
        'mutation { setAccessUserRole(username: "ana@exemplo.com", role: "leitor") { role } }',
    )
    assert ok["data"]["setAccessUserRole"]["role"] == "leitor"
