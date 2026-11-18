"""API GraphQL do túnel, o host do destino SSH e o *health* de verdade (HTTP local)."""

import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from formula_graphs import build_db
from login_helpers import sign_in

from php_map.access import tokens
from php_map.access.store import Vault, open_vault
from php_map.web.app import create_app
from php_map.web.tunnel import HealthResult, http_health

pytest.importorskip("mcp")

LOCAL = "http://127.0.0.1:8765"
FAKE = Path(__file__).resolve().parents[1] / "fixtures" / "tunnel" / "fake_tunnel.py"


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return open_vault(tmp_path / "cofre" / "access.db")


def _client(tmp_path: Path, vault: Vault, *, login: bool = True, **kwargs: Any) -> TestClient:
    kwargs.setdefault("tunnel_health", lambda url: HealthResult(True, "ok"))
    app = create_app(
        build_db(tmp_path), ui_dir=None, out_dir=tmp_path / "out", vault=vault, **kwargs
    )
    client = TestClient(app, base_url=LOCAL)
    if login:
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


SAVE = (
    "mutation($c: [String!]!, $u: String, $r: String, $e: [String!]) {"
    " saveTunnelConfig(command: $c, publicUrl: $u, urlRegex: $r, envNames: $e)"
    " { configured command publicUrl urlRegex env { name defined } } }"
)


def test_config_examples_and_save(
    tmp_path: Path, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NGROK_AUTHTOKEN", raising=False)
    with _client(tmp_path, vault) as client:
        empty = _gql(
            client, "{ tunnelConfig { configured command examples { id command envNames } } }"
        )
        cfg = empty["data"]["tunnelConfig"]
        assert cfg["configured"] is False and cfg["command"] == []
        ids = {e["id"]: e for e in cfg["examples"]}
        assert ids["ngrok"]["command"][:3] == ["ngrok", "http", "{port}"]
        assert "--log=stdout" in ids["ngrok"]["command"] and ids["ngrok"]["envNames"] == [
            "NGROK_AUTHTOKEN"
        ]
        assert not any(
            "rewrite" in part for part in ids["ngrok"]["command"]
        )  # nunca reescrever o Host
        assert ids["cloudflared"]["command"][:2] == ["cloudflared", "tunnel"]
        saved = _gql(
            client,
            SAVE,
            {
                "c": ["ngrok", "http", "{port}"],
                "u": "https://x.ngrok-free.app",
                "r": None,
                "e": ["NGROK_AUTHTOKEN"],
            },
        )
        data = saved["data"]["saveTunnelConfig"]
        assert data["configured"] and data["env"] == [{"name": "NGROK_AUTHTOKEN", "defined": False}]
        monkeypatch.setenv("NGROK_AUTHTOKEN", "valor-secreto-123456")
        again = client.post("/graphql", json={"query": "{ tunnelConfig { env { name defined } } }"})
        assert '"defined":true' in again.text.replace(" ", "") and "valor-secreto" not in again.text


def test_invalid_config_is_an_error(tmp_path: Path, vault: Vault) -> None:
    with _client(tmp_path, vault) as client:
        assert "expressão regular" in _error(
            _gql(client, SAVE, {"c": ["x"], "u": None, "r": "([", "e": []})
        )
        assert "https" in _error(
            _gql(client, SAVE, {"c": ["x"], "u": "http://a.b", "r": None, "e": []})
        )
        assert "nome de variável" in _error(
            _gql(client, SAVE, {"c": ["x"], "u": None, "r": None, "e": ["A B"]})
        )
        assert "informe o comando" in _error(
            _gql(client, SAVE, {"c": [], "u": None, "r": None, "e": []})
        )


def test_start_status_stop_flow(
    tmp_path: Path, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TUNNEL_MODE", "ok")
    tokens.create_token(vault, "t")
    with _client(tmp_path, vault) as client:
        before = _gql(client, "{ tunnelStatus { state canStart blockedReason } }")["data"][
            "tunnelStatus"
        ]
        assert before["state"] == "parado" and before["canStart"] is False
        assert "configure o comando" in before["blockedReason"]
        _gql(
            client,
            SAVE,
            {"c": [sys.executable, str(FAKE), "{port}"], "u": None, "r": None, "e": []},
        )
        assert (
            _gql(client, "{ tunnelStatus { canStart } }")["data"]["tunnelStatus"]["canStart"]
            is True
        )
        started = _gql(client, "mutation { startTunnel { state stage stageLabel } }")
        assert "errors" not in started, started
        manager = client.app.state.ecmap.tunnel  # type: ignore[attr-defined]
        manager.wait(20)
        up = _gql(
            client,
            "{ tunnelStatus { state url publicHost log canStart } mcpStatus { publicUrls } }",
        )["data"]
        assert up["tunnelStatus"]["state"] == "no-ar"
        assert up["tunnelStatus"]["url"] == "https://teste.exemplo/mcp"
        assert up["mcpStatus"]["publicUrls"] == ["https://teste.exemplo/mcp"]  # cartão do Acesso
        assert up["tunnelStatus"]["canStart"] is False
        again = _gql(client, "mutation { startTunnel { state } }")
        assert "já está" in _error(again)
        stopped = _gql(client, "mutation { stopTunnel { state url } }")["data"]["stopTunnel"]
        assert stopped["state"] == "parado"
        assert _gql(client, "{ mcpStatus { publicUrls } }")["data"]["mcpStatus"]["publicUrls"] == []


def test_failed_prechecks_show_in_status(tmp_path: Path, vault: Vault) -> None:
    with _client(tmp_path, vault) as client:
        _gql(client, SAVE, {"c": [sys.executable, str(FAKE)], "u": None, "r": None, "e": []})
        _gql(client, "mutation { startTunnel { state } }")
        client.app.state.ecmap.tunnel.wait(10)  # type: ignore[attr-defined]
        status = _gql(client, "{ tunnelStatus { state stage error } }")["data"]["tunnelStatus"]
        assert status["state"] == "falhou" and "crie um token em Acesso" in status["error"]


def test_read_only_and_session_rules(tmp_path: Path, vault: Vault) -> None:
    with _client(tmp_path, vault, read_only=True) as client:
        assert "errors" in _gql(client, SAVE, {"c": ["x"], "u": None, "r": None, "e": []})
        status = _gql(client, "{ tunnelStatus { canStart blockedReason } }")["data"]["tunnelStatus"]
        assert status["canStart"] is False
    with _client(tmp_path, vault, login=False) as client:
        assert (
            client.post("/graphql", json={"query": "{ tunnelStatus { state } }"}).status_code == 401
        )


def test_without_vault_there_is_no_tunnel(tmp_path: Path) -> None:
    app = create_app(build_db(tmp_path), ui_dir=None, out_dir=tmp_path / "out")
    with TestClient(app, base_url=LOCAL) as client:
        status = _gql(client, "{ tunnelStatus { state canStart } }")["data"]["tunnelStatus"]
        assert status["canStart"] is False
        assert "cofre" in _error(_gql(client, "mutation { startTunnel { state } }"))


# --------------------------------------------------------------------------- health real


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"

    def _send(self, status: int) -> None:
        self.send_response(status)
        if self.mode == "ngrok":
            self.send_header("ngrok-error-code", "ERR_NGROK_8012")
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        # Lê o corpo antes de responder: fechar o socket com dados não lidos faz o Windows
        # mandar RST e o cliente vê WinError 10053 em vez da resposta.
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self._send(200 if self.mode == "open-mcp" else 401)

    def do_GET(self) -> None:  # noqa: N802
        self._send(200 if self.mode == "leak" else 404)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def remote() -> Iterator[Any]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


@pytest.mark.parametrize(
    ("mode", "ok", "fragment"),
    [
        ("ok", True, "401"),
        ("leak", False, "GET /graphql devolveu 200"),
        ("open-mcp", False, "POST /mcp sem token devolveu 200"),
        ("ngrok", False, "ERR_NGROK_8012"),
    ],
)
def test_http_health_checks_401_and_404(remote: Any, mode: str, ok: bool, fragment: str) -> None:
    _Handler.mode = mode
    result = http_health(f"http://127.0.0.1:{remote.server_address[1]}")
    assert result.ok is ok and fragment in result.detail


def test_http_health_reports_a_dead_endpoint() -> None:
    result = http_health("http://127.0.0.1:1")
    assert not result.ok and "sem resposta" in result.detail
