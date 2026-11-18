"""`php-map ui`: a interface nunca sobe sem login por acidente."""

from pathlib import Path
from typing import Any

import pytest
from formula_graphs import build_db
from typer.testing import CliRunner

from php_map.access import users
from php_map.access.store import open_vault
from php_map.cli import app

PASSWORD = "uma-senha-longa"


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Troca o `uvicorn.run` por um que só guarda o app (nenhuma porta é aberta)."""
    apps: list[Any] = []
    monkeypatch.setattr("uvicorn.run", lambda web, **kw: apps.append((web, kw)))
    return apps


def _ui(tmp_path: Path, *args: str) -> Any:
    db = build_db(tmp_path)
    return CliRunner().invoke(app, ["ui", "--db", str(db), *args])


def test_empty_vault_prints_the_setup_url_and_code(tmp_path: Path, served: list[Any]) -> None:
    result = _ui(tmp_path, "--port", "8799")
    assert result.exit_code == 0, result.output
    web, _ = served[0]
    code = web.state.access.setup_code
    assert code and "Configuração inicial" in result.output
    assert "http://127.0.0.1:8799/" in result.output and code in result.output


def test_vault_is_created_when_it_does_not_exist(tmp_path: Path, served: list[Any]) -> None:
    target = tmp_path / "novo" / "access.db"
    result = _ui(tmp_path, "--access-db", str(target))
    assert result.exit_code == 0, result.output
    assert target.is_file() and served


def test_with_users_the_code_is_not_printed(tmp_path: Path, served: list[Any]) -> None:
    target = tmp_path / "access.db"
    users.add_user(open_vault(target), "ana@exemplo.com", PASSWORD)
    result = _ui(tmp_path, "--access-db", str(target))
    assert result.exit_code == 0, result.output
    web, _ = served[0]
    assert web.state.access.setup_code is None
    assert "Configuração inicial" not in result.output and "Login ativo" in result.output


def test_unreadable_vault_refuses_to_start(tmp_path: Path, served: list[Any]) -> None:
    broken = tmp_path / "access.db"
    broken.write_text("isto não é um banco sqlite " * 20)
    result = _ui(tmp_path, "--access-db", str(broken))
    assert result.exit_code == 1 and not served
    assert "cofre" in result.output and "sem login" not in result.output.lower()


def test_no_auth_is_refused_outside_loopback(tmp_path: Path, served: list[Any]) -> None:
    result = _ui(tmp_path, "--no-auth", "--host", "0.0.0.0")
    assert result.exit_code == 1 and not served
    assert "--no-auth só é permitido com --host 127.0.0.1" in result.output


def test_no_auth_in_loopback_runs_without_login_or_mcp(tmp_path: Path, served: list[Any]) -> None:
    result = _ui(tmp_path, "--no-auth", "--host", "127.0.0.1")
    assert result.exit_code == 0, result.output
    web, _ = served[0]
    assert not hasattr(web.state, "access") and not web.state.ecmap.mcp_available
    assert "SEM LOGIN" in result.output and "Configuração inicial" not in result.output
