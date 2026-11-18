"""Comandos `user` e `token` e o bearer do cofre no transporte HTTP do MCP."""

import getpass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cross_graphs import META_CHAMA, META_SERVE, build_chama, build_serve
from cross_graphs import save as _save
from typer.testing import CliRunner

from php_map.access import tokens
from php_map.access.store import open_vault
from php_map.cli import _mcp_verifier, app
from php_map.errors import AccessError
from php_map.mcp.auth import VaultTokenVerifier

runner = CliRunner()
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


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    return tmp_path / "cofre" / "access.db"


def _passwords(monkeypatch: pytest.MonkeyPatch, *answers: str) -> None:
    given = iter(answers)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(given))


def _run(vault_path: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--access-db", str(vault_path)])
    return result.exit_code, result.output.replace("\n", " ")


def test_user_add_and_list(monkeypatch: pytest.MonkeyPatch, vault_path: Path) -> None:
    code, out = _run(vault_path, "user", "list")
    assert code == 0 and "Nenhum usuário" in out
    _passwords(monkeypatch, "senha-bem-longa", "senha-bem-longa")
    code, out = _run(vault_path, "user", "add", "ana@exemplo.com")
    assert code == 0 and "criado" in out
    code, out = _run(vault_path, "user", "list")
    assert code == 0 and "ana@exemplo.com" in out and "ativa" in out
    assert "senha-bem-longa" not in out and "scrypt" not in out


def test_user_add_refuses_a_bad_email_before_asking_for_the_password(
    monkeypatch: pytest.MonkeyPatch, vault_path: Path
) -> None:
    def never_asked(prompt: str = "") -> str:
        raise AssertionError("a senha não deve ser pedida com e-mail inválido")

    monkeypatch.setattr(getpass, "getpass", never_asked)
    code, out = _run(vault_path, "user", "add", "ana")
    assert code == 1 and "e-mail inválido" in out
    code, out = _run(vault_path, "user", "list")
    assert "Nenhum usuário" in out
    _passwords(monkeypatch, "senha-bem-longa", "senha-bem-longa")
    code, out = _run(vault_path, "user", "add", "Ana@Exemplo.com")
    assert code == 0 and "ana@exemplo.com" in out


def test_user_add_rejects_short_and_mismatched_passwords(
    monkeypatch: pytest.MonkeyPatch, vault_path: Path
) -> None:
    _passwords(monkeypatch, "curta", "curta")
    code, out = _run(vault_path, "user", "add", "ana@exemplo.com")
    assert code == 1 and "10 caracteres" in out
    _passwords(monkeypatch, "senha-bem-longa", "outra-senha-longa")
    code, out = _run(vault_path, "user", "add", "ana@exemplo.com")
    assert code == 2 and "não conferem" in out


def test_user_passwd_disable_enable(monkeypatch: pytest.MonkeyPatch, vault_path: Path) -> None:
    _passwords(monkeypatch, "senha-bem-longa", "senha-bem-longa")
    _run(vault_path, "user", "add", "ana@exemplo.com")
    _passwords(monkeypatch, "senha-bem-longa", "senha-bem-longa")
    _run(vault_path, "user", "add", "bia@exemplo.com", "--role", "admin")
    _passwords(monkeypatch, "outra-senha-longa", "outra-senha-longa")
    assert _run(vault_path, "user", "passwd", "ana@exemplo.com")[0] == 0
    assert "desativado" in _run(vault_path, "user", "disable", "ana@exemplo.com")[1]
    assert "desativada" in _run(vault_path, "user", "list")[1]
    assert "reativado" in _run(vault_path, "user", "enable", "ana@exemplo.com")[1]
    code, out = _run(vault_path, "user", "disable", "ninguem")
    assert code == 1 and "não encontrado" in out


def test_token_create_shows_once_and_list_hides_it(vault_path: Path) -> None:
    code, out = _run(vault_path, "token", "create", "--name", "meu-notebook")
    assert code == 0, out
    raw = next(w for w in out.split() if w.startswith("ecm_"))
    assert (
        "Configure no seu cliente MCP:" in out and "/mcp e cabeçalho `Authorization: Bearer " in out
    )
    code, listed = _run(vault_path, "token", "list")
    assert code == 0 and "meu-notebook" in listed and raw[:12] in listed and "ativo" in listed
    assert raw not in listed and tokens.hash_token(raw) not in listed


def test_token_revoke_and_errors(vault_path: Path) -> None:
    _run(vault_path, "token", "create", "--name", "a")
    prefix = open_vault(vault_path).query("SELECT prefix FROM tokens")[0]["prefix"]
    code, out = _run(vault_path, "token", "revoke", prefix)
    assert code == 0 and "revogado" in out
    assert "revogado" in _run(vault_path, "token", "list")[1]
    assert _run(vault_path, "token", "revoke", "ecm_nada")[0] == 1
    code, out = _run(vault_path, "token", "create", "--name", "b", "--expires", "xx")
    assert code == 1 and "validade" in out


def test_token_never_warns(vault_path: Path) -> None:
    code, out = _run(vault_path, "token", "create", "--name", "n", "--expires", "never")
    assert code == 0 and "sem validade" in out
    assert "nunca" in _run(vault_path, "token", "list")[1]


# --------------------------------------------------------------------------- MCP + cofre


def test_mcp_verifier_needs_a_token(vault_path: Path) -> None:
    assert _mcp_verifier(None, vault_path) is None
    vault = open_vault(vault_path)
    raw, info = tokens.create_token(vault, "a")
    verify = _mcp_verifier(None, vault_path)
    assert verify(raw.encode()) and not verify(b"ecm_" + b"x" * 43)
    tokens.revoke_token(vault, str(info.id))
    assert not verify(raw.encode())  # revogação vale sem reiniciar
    assert _mcp_verifier(None, vault_path) is None


def test_mcp_verifier_legacy_only_keeps_working(vault_path: Path) -> None:
    legacy = "legado-" + "z" * 20
    verify = _mcp_verifier(legacy, vault_path)
    assert verify(legacy.encode()) and not verify(b"x" * 20)


def test_mcp_verifier_legacy_survives_a_broken_vault(tmp_path: Path) -> None:
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"isto nao e sqlite" * 100)
    legacy = "legado-" + "z" * 20
    assert _mcp_verifier(legacy, broken)(legacy.encode())
    with pytest.raises(AccessError):
        _mcp_verifier(None, broken)


def test_mcp_command_error_mentions_token_create(tmp_path: Path, vault_path: Path) -> None:
    pytest.importorskip("mcp")
    db = _save(tmp_path, "serve", build_serve(), META_SERVE)
    result = runner.invoke(
        app, ["mcp", "--db", str(db), "--transport", "http", "--access-db", str(vault_path)]
    )
    assert result.exit_code == 2
    assert "php-map token create" in result.output.replace("\n", " ")


# --------------------------------------------------------------------------- HTTP com o cofre


def test_http_accepts_vault_tokens_revokes_and_expires(tmp_path: Path, vault_path: Path) -> None:
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient

    from php_map.mcp import server as mcp_server
    from php_map.mcp.catalog import ProjectCatalog

    db = _save(tmp_path, "chama", build_chama(), META_CHAMA)
    moment = [datetime(2026, 11, 9, 12, 0, tzinfo=UTC)]
    vault = open_vault(vault_path, clock=lambda: moment[0])
    raw, info = tokens.create_token(vault, "http")
    short, _ = tokens.create_token(vault, "curto", expires="30d")
    server = mcp_server.build_server(ProjectCatalog.from_dbs([db]))
    verifier = VaultTokenVerifier(vault)
    app_ = mcp_server.http_app(
        server, host="127.0.0.1", allowed_hosts=["testserver"], token=verifier
    )
    with TestClient(app_, base_url="http://testserver") as client:

        def call(token: str) -> object:
            return client.post(
                "/mcp", json=INIT, headers={**HEADERS, "authorization": f"Bearer {token}"}
            )

        ok = call(raw)
        assert ok.status_code == 200 and raw not in ok.text  # type: ignore[attr-defined]
        assert call(short).status_code == 200  # type: ignore[attr-defined]
        tokens.revoke_token(vault, str(info.id))
        denied = call(raw)
        assert denied.status_code == 401  # type: ignore[attr-defined]
        assert denied.headers["www-authenticate"] == 'Bearer realm="php-map"'  # type: ignore[attr-defined]
        moment[0] += timedelta(days=31)
        assert call(short).status_code == 401  # type: ignore[attr-defined]


def test_user_add_role_defaults_and_role_command(
    monkeypatch: pytest.MonkeyPatch, vault_path: Path
) -> None:
    same = ("senha-bem-longa", "senha-bem-longa")
    _passwords(monkeypatch, *same)
    code, out = _run(vault_path, "user", "add", "ana@exemplo.com")  # o primeiro é sempre admin
    assert code == 0 and "admin" in out
    _passwords(monkeypatch, *same)
    code, out = _run(vault_path, "user", "add", "bia@exemplo.com")  # depois, o padrão é leitor
    assert code == 0 and "leitor" in out
    _passwords(monkeypatch, *same)
    assert _run(vault_path, "user", "add", "cris@exemplo.com", "--role", "dono")[0] == 1
    _passwords(monkeypatch, *same)
    assert _run(vault_path, "user", "add", "dani@exemplo.com", "--role", "admin")[0] == 0
    listed = runner.invoke(app, ["user", "list", "--access-db", str(vault_path)]).output
    assert "Papel" in listed and "leitor" in listed and "admin" in listed

    code, out = _run(vault_path, "user", "role", "bia@exemplo.com", "admin")
    assert code == 0 and "agora é admin" in out
    assert _run(vault_path, "user", "role", "bia@exemplo.com", "dono")[0] == 1
    assert _run(vault_path, "user", "role", "ninguem", "admin")[0] == 1


def test_the_cli_refuses_to_drop_the_last_admin(
    monkeypatch: pytest.MonkeyPatch, vault_path: Path
) -> None:
    _passwords(monkeypatch, "senha-bem-longa", "senha-bem-longa")
    _run(vault_path, "user", "add", "ana@exemplo.com")
    code, out = _run(vault_path, "user", "disable", "ana@exemplo.com")
    assert code == 1 and "último administrador" in out
    code, out = _run(vault_path, "user", "role", "ana@exemplo.com", "leitor")
    assert code == 1 and "último administrador" in out
    assert "ativa" in _run(vault_path, "user", "list")[1]
