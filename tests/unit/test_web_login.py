"""Login da interface (`/auth/*`) e o `SessionGate` sobre `/graphql`."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from formula_graphs import build_db

from php_map.access import users
from php_map.access.store import Vault, open_vault
from php_map.web.app import create_app

LOCAL = "http://127.0.0.1:8765"
OK_ORIGIN = {"origin": LOCAL}
PASSWORD = "uma-senha-longa"
QUERY = {"query": "{ stats }"}


class Clock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs: float) -> None:
        self.moment += timedelta(**kwargs)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def vault(tmp_path: Path, clock: Clock) -> Vault:
    return open_vault(tmp_path / "cofre" / "access.db", clock=clock)


@pytest.fixture
def client(tmp_path: Path, vault: Vault) -> TestClient:
    app = create_app(build_db(tmp_path), ui_dir=None, out_dir=tmp_path / "out", vault=vault)
    return TestClient(app, base_url=LOCAL)


def _login(
    client: TestClient, username: str = "ana@exemplo.com", password: str = PASSWORD, **kw: object
):  # type: ignore[no-untyped-def]
    headers = kw.pop("headers", OK_ORIGIN)
    return client.post(
        "/auth/login", json={"email": username, "password": password}, headers=headers
    )


def _graphql(client: TestClient):  # type: ignore[no-untyped-def]
    return client.post("/graphql", json=QUERY)


def _code(client: TestClient) -> str:
    code = client.app.state.access.setup_code  # type: ignore[attr-defined]
    assert isinstance(code, str)
    return code


def _first_user(
    client: TestClient, code: str, username: str = "ana@exemplo.com", password: str = PASSWORD
):  # type: ignore[no-untyped-def]
    return client.post(
        "/auth/first-user",
        json={"email": username, "password": password, "setupCode": code},
        headers=OK_ORIGIN,
    )


def test_without_users_the_server_is_in_setup_mode(client: TestClient) -> None:
    me = client.get("/auth/me").json()
    assert me == {"loginRequired": True, "setupRequired": True, "user": None}
    denied = _graphql(client)
    assert denied.status_code == 401 and denied.json() == {"error": "setup"}
    assert client.get("/graphql").status_code == 401  # GraphiQL também


def test_setup_code_is_random_and_lives_only_in_memory(tmp_path: Path, vault: Vault) -> None:
    def started() -> str:
        app = create_app(build_db(tmp_path), ui_dir=None, out_dir=tmp_path / "out", vault=vault)
        return str(app.state.access.setup_code)

    first, second = started(), started()
    assert len(first) >= 16 and first != second  # a cada reinício, outro código
    assert not any(first in path.read_text(errors="ignore") for path in tmp_path.rglob("*.json"))
    assert first.encode() not in vault.path.read_bytes()


def test_first_user_with_the_code_logs_in_and_the_code_dies(
    client: TestClient, vault: Vault
) -> None:
    code = _code(client)
    created = _first_user(client, code)
    assert created.status_code == 201 and "ecm_session" in created.cookies
    me = client.get("/auth/me").json()
    assert me == {
        "loginRequired": True,
        "setupRequired": False,
        "user": {"username": "ana@exemplo.com", "role": "admin"},
    }
    assert _graphql(client).status_code == 200  # já logado
    again = _first_user(client, code, username="bia@exemplo.com")  # o mesmo código de novo
    assert again.status_code == 409
    assert [u.username for u in users.list_users(vault)] == ["ana@exemplo.com"]
    assert client.app.state.access.setup_code is None  # type: ignore[attr-defined]


def test_wrong_code_is_refused_and_creates_nobody(client: TestClient, vault: Vault) -> None:
    wrong = _first_user(client, "codigo-errado")
    assert wrong.status_code == 403 and wrong.json() == {"error": "setup-code"}
    assert _first_user(client, "").status_code == 403
    assert (
        client.post(
            "/auth/first-user",
            json={"username": "ana@exemplo.com", "password": PASSWORD},
            headers=OK_ORIGIN,
        ).status_code
        == 403
    )  # sem o campo
    assert users.list_users(vault) == []
    assert _graphql(client).status_code == 401
    assert _first_user(client, _code(client)).status_code == 201  # o certo ainda vale


def test_first_user_validates_the_password_without_burning_the_code(client: TestClient) -> None:
    code = _code(client)
    short = _first_user(client, code, password="curta")
    assert short.status_code == 400 and "10 caracteres" in short.json()["error"]
    assert _first_user(client, code).status_code == 201


def test_first_user_needs_an_email_and_keeps_the_code(client: TestClient, vault: Vault) -> None:
    code = _code(client)
    for bad in ("ana", "ana@", "a na@exemplo.com", ""):
        refused = _first_user(client, code, username=bad)
        assert refused.status_code == 400 and "e-mail inválido" in refused.json()["error"]
    assert users.list_users(vault) == [] and client.app.state.access.setup_code == code  # type: ignore[attr-defined]
    created = _first_user(client, code, username="  Admin@Exemplo.COM ")
    assert created.status_code == 201
    assert [u.username for u in users.list_users(vault)] == ["admin@exemplo.com"]
    # o login não diferencia maiúsculas
    assert _login(client, "ADMIN@exemplo.com").status_code == 204
    assert _graphql(client).status_code == 200


def test_old_username_field_is_still_accepted(client: TestClient, vault: Vault) -> None:
    created = client.post(
        "/auth/first-user",
        json={"username": "admin@exemplo.com", "password": PASSWORD, "setupCode": _code(client)},
        headers=OK_ORIGIN,
    )
    assert created.status_code == 201
    legacy = client.post(
        "/auth/login",
        json={"username": "admin@exemplo.com", "password": PASSWORD},
        headers=OK_ORIGIN,
    )
    assert legacy.status_code == 204


def test_setup_attempts_are_throttled_by_ip(client: TestClient, vault: Vault) -> None:
    codes = [_first_user(client, f"chute-{i}").status_code for i in range(22)]
    assert codes[:20] == [403] * 20 and codes[20:] == [429, 429]
    assert _first_user(client, _code(client)).status_code == 429  # nem o código certo passa
    assert users.list_users(vault) == []


def test_setup_requires_a_local_origin(client: TestClient, vault: Vault) -> None:
    external = client.post(
        "/auth/first-user",
        json={"username": "ana@exemplo.com", "password": PASSWORD, "setupCode": _code(client)},
        headers={"origin": "https://evil.example"},
    )
    assert external.status_code == 403 and users.list_users(vault) == []


def test_setup_is_refused_for_non_local_host(client: TestClient, vault: Vault) -> None:
    # O `HostGate` devolve 404 a qualquer pedido remoto fora de `/mcp`: a tela de configuração só
    # abre por um host local ou liberado em `--host`.
    proxied = client.post(
        "/auth/first-user",
        json={"username": "ana@exemplo.com", "password": PASSWORD, "setupCode": _code(client)},
        headers={**OK_ORIGIN, "x-forwarded-for": "1.2.3.4"},
    )
    assert proxied.status_code == 404
    assert client.get("/auth/me", headers={"x-forwarded-host": "x"}).status_code == 404
    assert users.list_users(vault) == []


def test_the_code_never_reaches_the_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    code = _code(client)
    with caplog.at_level("DEBUG"):
        _first_user(client, "chute-qualquer")
        _first_user(client, code)
    assert code not in caplog.text and "chute-qualquer" not in caplog.text


def test_graphql_needs_the_session(client: TestClient, vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    denied = _graphql(client)
    assert denied.status_code == 401 and denied.json() == {"error": "login"}
    assert client.get("/graphql").status_code == 401  # GraphiQL também
    me = client.get("/auth/me").json()
    assert me == {"loginRequired": True, "setupRequired": False, "user": None}
    logged = _login(client)
    assert logged.status_code == 204
    cookie = logged.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
    assert _graphql(client).status_code == 200
    assert client.post("/auth/logout", json={}, headers=OK_ORIGIN).status_code == 204
    assert _graphql(client).status_code == 401


def test_cookie_is_secure_behind_https(client: TestClient, vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    resp = _login(client, headers={**OK_ORIGIN, "x-forwarded-proto": "https"})
    assert resp.status_code == 204 and "secure" in resp.headers["set-cookie"].lower()


def test_tampered_cookie_and_expired_session(
    client: TestClient, vault: Vault, clock: Clock
) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    _login(client)
    good = client.cookies["ecm_session"]
    client.cookies.clear()
    client.cookies.set("ecm_session", good[:-1] + ("0" if good[-1] != "0" else "1"))
    assert _graphql(client).status_code == 401
    client.cookies.clear()
    client.cookies.set("ecm_session", "lixo")
    assert _graphql(client).status_code == 401
    client.cookies.clear()
    client.cookies.set("ecm_session", good)
    assert _graphql(client).status_code == 200
    clock.advance(hours=13)
    assert _graphql(client).status_code == 401


def test_wrong_passwords_lock_with_minutes_message(
    client: TestClient, vault: Vault, clock: Clock
) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    for _ in range(5):
        bad = _login(client, password="errada-errada")
        assert bad.status_code == 401 and bad.json()["error"] == "usuário ou senha inválidos"
    blocked = _login(client, password="errada-errada")
    assert blocked.status_code == 429 and "tente de novo em" in blocked.json()["error"]
    assert "min" in blocked.json()["error"]
    assert _login(client).status_code == 429  # a senha certa no meio do bloqueio também recusa
    clock.advance(minutes=16)
    assert _login(client).status_code == 204


def test_unknown_user_gets_the_same_answers(client: TestClient, vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    ghost = _login(client, username="ninguem")
    wrong = _login(client, password="errada-errada")
    assert ghost.status_code == wrong.status_code == 401 and ghost.json() == wrong.json()
    for _ in range(4):
        _login(client, username="ninguem")
    assert _login(client, username="ninguem").status_code == 429  # igual a um usuário real


def test_ip_throttle(client: TestClient, vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    codes = [_login(client, username=f"u{i}").status_code for i in range(22)]
    assert codes[:20] == [401] * 20 and codes[20:] == [429, 429]


def test_external_origin_and_form_posts_are_refused(client: TestClient, vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    assert _login(client, headers={"origin": "https://evil.example"}).status_code == 403
    assert _login(client, headers={}).status_code == 403
    form = client.post(
        "/auth/login", data={"username": "ana@exemplo.com", "password": PASSWORD}, headers=OK_ORIGIN
    )
    assert form.status_code == 403
    assert (
        client.post("/auth/logout", json={}, headers={"origin": "https://evil.example"}).status_code
        == 403
    )


def test_password_change(client: TestClient, vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    body = {"current": PASSWORD, "new": "outra-senha-longa"}
    assert client.post("/auth/password", json=body, headers=OK_ORIGIN).status_code == 401
    _login(client)
    wrong = client.post(
        "/auth/password", json={"current": "x" * 12, "new": "outra-senha-longa"}, headers=OK_ORIGIN
    )
    assert wrong.status_code == 400 and "não confere" in wrong.json()["error"]
    short = client.post(
        "/auth/password", json={"current": PASSWORD, "new": "curta"}, headers=OK_ORIGIN
    )
    assert short.status_code == 400
    assert client.post("/auth/password", json=body, headers=OK_ORIGIN).status_code == 204
    assert users.authenticate(vault, "ana@exemplo.com", "outra-senha-longa") is not None


def test_disabled_user_loses_the_session_and_the_server_never_opens(
    client: TestClient, vault: Vault
) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    _login(client)
    # A regra do último administrador vale na API e na CLI; aqui o cofre é adulterado de fora.
    vault.run(
        "UPDATE users SET disabled_at = ? WHERE username = ?",
        ("2000-01-01T00:00:00Z", "ana@exemplo.com"),
    )
    denied = _graphql(client)  # sem usuário ativo: nunca abre, volta ao modo configuração
    assert denied.status_code == 401 and denied.json() == {"error": "setup"}
    assert client.get("/auth/me").json()["setupRequired"] is True


def test_read_only_keeps_login_working(tmp_path: Path, vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    app = create_app(
        build_db(tmp_path), ui_dir=None, out_dir=tmp_path / "out", vault=vault, read_only=True
    )
    client = TestClient(app, base_url=LOCAL)
    assert _login(client).status_code == 204
    assert _graphql(client).status_code == 200


def test_static_files_stay_open(tmp_path: Path, vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    app = create_app(build_db(tmp_path), ui_dir=dist, out_dir=tmp_path / "out", vault=vault)
    client = TestClient(app, base_url=LOCAL)
    assert client.get("/").status_code == 200
    assert client.get("/login").status_code == 200  # rota do React


def test_without_a_vault_nothing_changes(tmp_path: Path) -> None:
    app = create_app(build_db(tmp_path), ui_dir=None, out_dir=tmp_path / "out")
    client = TestClient(app, base_url=LOCAL)
    assert client.post("/graphql", json=QUERY).status_code == 200
    assert client.get("/auth/me").status_code == 404


def test_logs_never_hold_password_or_session(
    client: TestClient, vault: Vault, caplog: pytest.LogCaptureFixture
) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    with caplog.at_level("DEBUG"):
        _login(client, password="errada-errada")
        _login(client)
    cookie = client.cookies["ecm_session"]
    assert "errada-errada" not in caplog.text and PASSWORD not in caplog.text
    assert cookie not in caplog.text and cookie.split(".")[0] not in caplog.text
    assert "login ok para ana" in caplog.text and "login falhou para ana" in caplog.text
