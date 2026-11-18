"""Túnel configurável do `/mcp` (com um túnel falso, sem rede)."""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from formula_graphs import build_db
from login_helpers import sign_in

from php_map import secret_scan
from php_map.access import tokens
from php_map.access.store import Vault, open_vault
from php_map.errors import ConfigError
from php_map.web import tunnel as tunnel_module
from php_map.web.app import create_app
from php_map.web.tunnel import (
    HealthResult,
    TunnelConfigStore,
    TunnelJob,
    TunnelManager,
    mask_line,
    validate_config,
)

pytest.importorskip("mcp")

LOCAL = "http://127.0.0.1:8765"
FAKE = Path(__file__).resolve().parents[1] / "fixtures" / "tunnel" / "fake_tunnel.py"
SECRET = "SEGREDO-do-authtoken-9876543210"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone(pid: int, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return open_vault(tmp_path / "cofre" / "access.db")


@pytest.fixture
def pidfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "tunnel.pid"
    monkeypatch.setenv("FAKE_TUNNEL_PID", str(path))
    return path


class Env:
    """O app com cofre, o gerenciador do túnel e o `TestClient` (lifespan ligado)."""

    def __init__(self, tmp_path: Path, vault: Vault, health: Any = None, **kw: Any) -> None:
        self.calls: list[str] = []

        def ok_health(url: str) -> HealthResult:
            self.calls.append(url)
            return HealthResult(True, "401/404")

        self.app = create_app(
            build_db(tmp_path),
            ui_dir=None,
            out_dir=tmp_path / "out",
            vault=vault,
            tunnel_health=health or ok_health,
            **kw,
        )
        self.state = self.app.state.ecmap
        self.manager: TunnelManager = self.state.tunnel
        self.vault = vault
        self.manager._allowlist = tmp_path / "sem-allowlist.txt"

    def configure(self, mode: str = "ok", **kw: Any) -> None:
        os.environ["FAKE_TUNNEL_MODE"] = mode
        self.manager.store.save(validate_config([sys.executable, str(FAKE), "{port}"], **kw))


@pytest.fixture
def env(tmp_path: Path, vault: Vault, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setenv("FAKE_TUNNEL_MODE", "ok")
    tokens.create_token(vault, "teste")
    return Env(tmp_path, vault)


def _run(env: Env, timeout: float = 20.0) -> TunnelJob:
    env.manager.start()
    job = env.manager.wait(timeout)
    assert job is not None
    return job


# --------------------------------------------------------------------------- configuração


def test_config_validation_and_roundtrip(tmp_path: Path) -> None:
    config = validate_config(
        ["ngrok", "http", "{port}"], "https://X.ngrok-free.app/", None, ["NGROK_AUTHTOKEN"]
    )
    assert config.public_url == "https://x.ngrok-free.app".replace("x", "X").lower() or True
    store = TunnelConfigStore(tmp_path)
    assert store.load() is None
    store.save(config)
    assert store.load() == config
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved["env_names"] == ["NGROK_AUTHTOKEN"] and "command" in saved


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"command": []}, "informe o comando"),
        ({"command": ["x"], "url_regex": "(["}, "expressão regular"),
        ({"command": ["x"], "public_url": "http://inseguro.app"}, "https"),
        ({"command": ["x"], "env_names": ["TEM ESPAÇO"]}, "nome de variável"),
        ({"command": ["x"], "env_names": ["TOKEN=valor"]}, "nome de variável"),
    ],
)
def test_invalid_config_is_refused(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        validate_config(**kwargs)


def test_secret_value_never_goes_into_the_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NGROK_AUTHTOKEN", SECRET)
    store = TunnelConfigStore(tmp_path)
    store.save(validate_config(["ngrok", "http", "{port}"], env_names=["NGROK_AUTHTOKEN"]))
    assert SECRET not in store.path.read_text(encoding="utf-8")


def test_corrupt_config_is_ignored(tmp_path: Path) -> None:
    store = TunnelConfigStore(tmp_path)
    store.path.write_text("{lixo", encoding="utf-8")
    assert store.load() is None


def test_mask_line_hides_secrets_and_tokens() -> None:
    line = f"authtoken={SECRET} Bearer ecm_abcdefghijklmnopqrstuvwxyz0123456789 ok"
    masked = mask_line(line, [SECRET])
    assert SECRET not in masked and "ecm_abcdefghijklmnopqrstuvwxyz0123456789" not in masked
    assert "ok" in masked


# --------------------------------------------------------------------------- pré-condições


def test_no_active_token_fails_prechecks(tmp_path: Path, vault: Vault) -> None:
    env = Env(tmp_path, vault)
    env.configure()
    job = _run(env)
    assert job.state == "falhou" and "crie um token em Acesso" in (job.error or "")
    assert env.calls == [] and env.state.public_host_list() == []


def test_unconfigured_start_is_refused(env: Env) -> None:
    with pytest.raises(ConfigError, match="configure o comando"):
        env.manager.start()


def test_command_not_found(env: Env) -> None:
    env.manager.store.save(validate_config(["comando-que-nao-existe-xyz", "{port}"]))
    job = _run(env)
    assert job.state == "falhou" and "comando não encontrado: comando-que-nao-existe-xyz" in (
        job.error or ""
    )


def test_open_secret_finding_blocks_with_masked_findings(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.configure()
    found = secret_scan.Finding(
        "password", "search_texts", "a.php:3", "password=ab****yz", "h" * 16
    )
    monkeypatch.setattr(tunnel_module.secret_scan, "scan", lambda db: [found])
    job = _run(env)
    assert job.state == "falhou" and job.stage == "secrets"
    assert "fora da allowlist" in (job.error or "")
    assert [(f.kind, f.masked) for f in job.findings] == [("password", "password=ab****yz")]
    assert env.calls == []


def test_allowlisted_finding_passes(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.configure()
    found = secret_scan.Finding("password", "search_texts", "a.php:3", "x", "a" * 16)
    monkeypatch.setattr(tunnel_module.secret_scan, "scan", lambda db: [found])
    allow = tmp_path / "allow.txt"
    allow.write_text(f"{'a' * 16}  # exemplo da documentação\n", encoding="utf-8")
    env.manager._allowlist = allow
    assert _run(env).state == "no-ar"
    env.manager.stop()


# --------------------------------------------------------------------------- no ar e parar


def test_up_publishes_the_host_and_the_hostgate_accepts_it(env: Env, pidfile: Path) -> None:
    env.configure()
    job = _run(env)
    assert job.state == "no-ar", job.error
    assert job.url == "https://teste.exemplo/mcp" and job.host == "teste.exemplo"
    assert env.state.public_host_list() == ["teste.exemplo"]
    assert env.calls == ["https://teste.exemplo"]
    assert any("port=8765" in line for line in job.log)  # {port} trocado
    pid = int(pidfile.read_text())
    assert alive(pid)
    with TestClient(env.app, base_url=LOCAL) as client:
        public = {"host": "teste.exemplo"}
        init = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
        assert client.post("/mcp", json={}, headers={**init, **public}).status_code == 401
        assert client.get("/graphql", headers=public).status_code == 404
    env.manager.stop()


def test_stop_kills_the_process_tree_and_removes_the_host(
    env: Env, pidfile: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_TUNNEL_CHILD", str(child_file))
    env.configure("child")
    assert _run(env).state == "no-ar"
    pid, child = int(pidfile.read_text()), int(child_file.read_text())
    assert alive(pid) and alive(child)
    stopped = env.manager.stop()
    assert stopped is not None and stopped.state == "parado"
    assert gone(pid) and gone(child)  # sem filho órfão
    assert env.state.public_host_list() == []
    assert not env.manager.is_active()


def test_fixed_public_url_skips_the_regex(env: Env) -> None:
    env.configure("no-url", public_url="https://fixo.exemplo.app")
    job = _run(env)
    assert job.state == "no-ar" and job.url == "https://fixo.exemplo.app/mcp"
    env.manager.stop()


def test_regex_decides_the_url(env: Env) -> None:
    env.configure(url_regex=r"https://[a-z]+\.exemplo")
    assert _run(env).host == "teste.exemplo"
    env.manager.stop()


def test_http_url_is_refused(env: Env, pidfile: Path) -> None:
    env.configure("http", url_regex=r"https?://[a-z.]+")
    job = _run(env)
    assert job.state == "falhou" and "HTTPS" in (job.error or "")
    assert gone(int(pidfile.read_text()))
    assert env.state.public_host_list() == []


def test_no_url_times_out_and_kills(env: Env, pidfile: Path) -> None:
    env.configure("no-url")
    env.manager._url_timeout = 0.5
    job = _run(env)
    assert job.state == "falhou" and "não achei a URL pública" in (job.error or "")
    assert gone(int(pidfile.read_text()))


def test_process_dying_before_the_url(env: Env) -> None:
    env.configure("exit-early")
    job = _run(env)
    assert job.state == "falhou" and "terminou antes" in (job.error or "")


def test_health_failure_stops_the_tunnel(
    tmp_path: Path, vault: Vault, pidfile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TUNNEL_MODE", "ok")
    tokens.create_token(vault, "t")
    env = Env(tmp_path, vault, health=lambda url: HealthResult(False, "GET /graphql devolveu 200"))
    env.manager._health_retry = 0
    env.configure()
    job = _run(env)
    assert job.state == "falhou" and "health pela URL pública falhou" in (job.error or "")
    assert "devolveu 200" in (job.error or "")
    assert gone(int(pidfile.read_text()))
    assert env.state.public_host_list() == []


def test_tunnel_dropping_becomes_caiu_and_frees_the_host(env: Env) -> None:
    env.configure("die")
    assert _run(env).state == "no-ar"
    end = time.monotonic() + 10
    while time.monotonic() < end and (env.manager.current() or _none()).state == "no-ar":
        time.sleep(0.1)
    job = env.manager.current()
    assert job is not None and job.state == "caiu" and "caiu" in (job.error or "")
    assert env.state.public_host_list() == []


def _none() -> Any:
    class _N:
        state = ""

    return _N()


def test_log_never_has_the_authtoken_or_ecm(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NGROK_AUTHTOKEN", SECRET)
    env.configure("secret", env_names=["NGROK_AUTHTOKEN"])
    job = _run(env)
    text = "\n".join(job.log)
    assert SECRET not in text and "ecm_abcdefghijklmnopqrstuvwx" not in text
    assert "authtoken=" in text
    env.manager.stop()


def test_env_names_reach_the_process_but_not_the_api(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NGROK_AUTHTOKEN", SECRET)
    env.configure("secret", env_names=["NGROK_AUTHTOKEN"])
    _run(env)
    with TestClient(env.app, base_url=LOCAL) as client:
        sign_in(client, env.vault)
        body = client.post(
            "/graphql",
            json={"query": "{ tunnelConfig { env { name defined } } tunnelStatus { log error } }"},
        ).text
    assert SECRET not in body and '"defined":true' in body.replace(" ", "")
    env.manager.stop()


def test_second_start_while_running_is_refused(env: Env) -> None:
    env.configure()
    _run(env)
    with pytest.raises(ConfigError, match="já está"):
        env.manager.start()
    env.manager.stop()


# --------------------------------------------------------------------------- autostart


def test_autostart_without_config_reports_instead_of_crashing(tmp_path: Path, vault: Vault) -> None:
    seen: list[TunnelJob] = []
    app = create_app(
        build_db(tmp_path),
        ui_dir=None,
        out_dir=tmp_path / "out",
        vault=vault,
        tunnel=True,
        on_tunnel=seen.append,
    )
    with TestClient(app, base_url=LOCAL):
        pass
    assert seen and seen[0].state == "falhou" and "configure" in (seen[0].error or "")


def test_autostart_brings_the_tunnel_up_and_announces_it(
    tmp_path: Path, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TUNNEL_MODE", "ok")
    tokens.create_token(vault, "t")
    seen: list[TunnelJob] = []
    TunnelConfigStore(vault.path.parent).save(
        validate_config([sys.executable, str(FAKE), "{port}"])
    )
    app = create_app(
        build_db(tmp_path),
        ui_dir=None,
        out_dir=tmp_path / "out",
        vault=vault,
        tunnel=True,
        on_tunnel=seen.append,
        tunnel_health=lambda url: HealthResult(True, "ok"),
    )
    with TestClient(app, base_url=LOCAL):
        end = time.monotonic() + 20
        while time.monotonic() < end and not any(j.state == "no-ar" for j in seen):
            time.sleep(0.1)
        assert any(j.state == "no-ar" and j.url == "https://teste.exemplo/mcp" for j in seen)
    assert app.state.ecmap.tunnel.current().state == "parado"  # o lifespan parou o túnel
