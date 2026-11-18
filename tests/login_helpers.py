"""Entrar na interface nos testes que usam o cofre: sem usuário o `/graphql` só responde `setup`."""

from fastapi.testclient import TestClient

from php_map.access import users
from php_map.access.store import Vault

LOCAL = "http://127.0.0.1:8765"
PASSWORD = "uma-senha-longa"


def sign_in(client: TestClient, vault: Vault, name: str = "ana@exemplo.com") -> None:
    """Cria o usuário (se o cofre não tem nenhum) e abre a sessão no `client`."""
    if not users.list_users(vault):
        users.add_user(vault, name, PASSWORD)
    response = client.post(
        "/auth/login", json={"email": name, "password": PASSWORD}, headers={"origin": LOCAL}
    )
    assert response.status_code == 204, response.text
