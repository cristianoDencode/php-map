"""Túnel para expor o `/mcp`.

O mapeador sobe um **comando configurável** (ngrok, cloudflared… qualquer um que entregue HTTPS),
sem shell, com `{port}` e `{host}` trocados. Antes de subir: pelo menos um token ativo no cofre
e a varredura de segredos dos grafos sem achado fora da allowlist. Depois de subir e achar a
URL pública: o *health* pela própria URL espera **401** em
`/mcp` sem token e **404** em `/graphql` (prova o caminho, a autenticação e a regra «remoto só
`/mcp`», sem precisar do token em claro).

A configuração fica em `<pasta do cofre>/tunnel.json`: só **nomes** de variáveis de ambiente
(`env_names`, ex.: `NGROK_AUTHTOKEN`); o valor nunca entra no JSON nem em nenhuma resposta, e o
log do túnel é mascarado antes de ser guardado.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from php_map import fsperm, secret_scan
from php_map.access import tokens as access_tokens
from php_map.access.store import Vault
from php_map.config import env_value
from php_map.errors import ConfigError
from php_map.web.jobs import kill_tree

if TYPE_CHECKING:
    from php_map.web.state import AppState

logger = logging.getLogger(__name__)

CONFIG_NAME = "tunnel.json"
DEFAULT_URL_REGEX = r"https://[A-Za-z0-9.-]+"
URL_TIMEOUT = 30.0
HEALTH_TIMEOUT = 10.0
HEALTH_RETRY_SECONDS = 20.0
LOG_TAIL = 80
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ECM = re.compile(r"ecm_[A-Za-z0-9_-]+")

# Estados (`state`) e etapas (`stage`).
STARTING, UP, DOWN, STOPPED, FAILED = "iniciando", "no-ar", "caiu", "parado", "falhou"
STAGES: dict[str, str] = {
    "prechecks": "Pré-checagens",
    "secrets": "Varredura de segredos",
    "tunnel": "Túnel",
    "url": "URL pública",
    "health": "Saúde",
}

NGROK_EXAMPLE = {
    "id": "ngrok",
    "label": "ngrok (conta grátis, domínio fixo)",
    "command": [
        "ngrok",
        "http",
        "{port}",
        "--url=https://SEU-DOMINIO.ngrok-free.app",
        "--log=stdout",
        "--log-format=logfmt",
    ],
    "public_url": "https://SEU-DOMINIO.ngrok-free.app",
    "env_names": ["NGROK_AUTHTOKEN"],
    "url_regex": DEFAULT_URL_REGEX,
}
CLOUDFLARED_EXAMPLE = {
    "id": "cloudflared",
    "label": "cloudflared (quick tunnel, URL aleatória a cada subida)",
    "command": ["cloudflared", "tunnel", "--url", "http://127.0.0.1:{port}"],
    "public_url": None,
    "env_names": [],
    "url_regex": r"https://[A-Za-z0-9-]+\.trycloudflare\.com",
}
EXAMPLES: tuple[dict[str, Any], ...] = (NGROK_EXAMPLE, CLOUDFLARED_EXAMPLE)


@dataclass(frozen=True, slots=True)
class TunnelConfig:
    command: tuple[str, ...]
    public_url: str | None = None
    url_regex: str = DEFAULT_URL_REGEX
    env_names: tuple[str, ...] = ()


def validate_config(
    command: Sequence[str],
    public_url: str | None = None,
    url_regex: str | None = None,
    env_names: Sequence[str] = (),
) -> TunnelConfig:
    parts = tuple(p.strip() for p in command if p.strip())
    if not parts:
        raise ConfigError("informe o comando do túnel (ex.: ngrok http {port})")
    if any("\n" in p or "\x00" in p for p in command):
        raise ConfigError("o comando do túnel não pode ter quebra de linha")
    pattern = (url_regex or "").strip() or DEFAULT_URL_REGEX
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"expressão regular da URL inválida: {exc}") from exc
    fixed = (public_url or "").strip() or None
    if fixed is not None:
        parsed = urlparse(fixed)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ConfigError("a URL pública fixa precisa ser https://<host>")
        fixed = f"https://{parsed.hostname}"
    names = tuple(dict.fromkeys(n.strip() for n in env_names if n.strip()))
    for name in names:
        if not _ENV_NAME.match(name):
            raise ConfigError(f"nome de variável inválido: {name!r} (só o NOME, nunca o valor)")
    return TunnelConfig(parts, fixed, pattern, names)


class TunnelConfigStore:
    """`tunnel.json` ao lado do cofre. Gravação atômica; nunca guarda valor de variável."""

    def __init__(self, folder: Path) -> None:
        self.path = folder / CONFIG_NAME

    def load(self) -> TunnelConfig | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return validate_config(
                data.get("command", []),
                data.get("public_url"),
                data.get("url_regex"),
                data.get("env_names", []),
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, AttributeError, TypeError, ConfigError) as exc:
            logger.warning("%s inválido, ignorado: %s", self.path.name, exc)
            return None

    def save(self, config: TunnelConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {
            "command": list(config.command),
            "public_url": config.public_url,
            "url_regex": config.url_regex,
            "env_names": list(config.env_names),
        }
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        fsperm.secure_file(tmp)
        os.replace(tmp, self.path)


@dataclass(frozen=True, slots=True)
class SecretFinding:
    kind: str
    where: str
    masked: str
    hash: str


@dataclass(frozen=True, slots=True)
class TunnelJob:
    """Retrato do túnel (o que a API devolve)."""

    state: str
    stage: str | None
    url: str | None  # URL pública do MCP (`https://<host>/mcp`)
    host: str | None
    error: str | None
    log: tuple[str, ...]
    findings: tuple[SecretFinding, ...]
    started_at: float | None


@dataclass(frozen=True, slots=True)
class HealthResult:
    ok: bool
    detail: str


HealthCheck = Callable[[str], HealthResult]


def http_health(base_url: str) -> HealthResult:
    """401 em `POST /mcp` sem token e 404 em `GET /graphql`, pela URL pública."""
    headers = {"ngrok-skip-browser-warning": "1", "user-agent": "php-map-health"}

    def call(method: str, path: str, body: bytes | None) -> tuple[int, str | None]:
        request = urllib.request.Request(  # noqa: S310 - https do próprio túnel
            base_url.rstrip("/") + path,
            data=body,
            method=method,
            headers={**headers, **({"content-type": "application/json"} if body else {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=HEALTH_TIMEOUT) as response:  # noqa: S310
                return response.status, response.headers.get("ngrok-error-code")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("ngrok-error-code")

    try:
        mcp, mcp_err = call("POST", "/mcp", b"{}")
        graphql, gql_err = call("GET", "/graphql", None)
    except (OSError, ValueError) as exc:
        return HealthResult(False, f"sem resposta pela URL pública: {exc}")
    erro = mcp_err or gql_err
    if erro:
        return HealthResult(
            False, f"o ngrok respondeu com o erro {erro} (o servidor local está no ar?)"
        )
    if mcp != 401:
        return HealthResult(False, f"POST /mcp sem token devolveu {mcp} (esperado 401)")
    if graphql != 404:
        return HealthResult(
            False, f"GET /graphql devolveu {graphql} (esperado 404): a interface vazaria"
        )
    return HealthResult(True, "401 em /mcp sem token e 404 em /graphql")


@dataclass(slots=True)
class _Run:
    process: "subprocess.Popen[str] | None" = None
    state: str = STARTING
    stage: str | None = "prechecks"
    url: str | None = None
    host: str | None = None
    error: str | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_TAIL))
    findings: list[SecretFinding] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    secrets: tuple[str, ...] = ()
    url_found: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> TunnelJob:
        return TunnelJob(
            self.state, self.stage, self.url, self.host, self.error, tuple(self.log),
            tuple(self.findings), self.started,
        )  # fmt: skip


def mask_line(line: str, secrets: Sequence[str] = ()) -> str:
    """Tira da linha o que não pode aparecer: valores das variáveis, `ecm_…` e o que o
    `secret_scan` reconhecer."""
    text = line.rstrip("\r\n")
    for secret in secrets:
        if len(secret) >= 4:
            text = text.replace(secret, secret_scan.mask(secret))
    text = _ECM.sub(lambda m: secret_scan.mask(m.group(0)), text)
    for _, value in secret_scan.scan_text(text):
        text = text.replace(value, secret_scan.mask(value))
    return text


class TunnelManager:
    """Um túnel por servidor: `start`, `stop`, `status` e `shutdown`."""

    def __init__(
        self,
        state: "AppState",
        vault: Vault,
        store: TunnelConfigStore,
        *,
        databases: Callable[[], Sequence[Path]],
        health: HealthCheck = http_health,
        allowlist: Path = secret_scan.DEFAULT_ALLOWLIST,
        on_change: Callable[[TunnelJob], None] | None = None,
        url_timeout: float = URL_TIMEOUT,
        health_retry: float = HEALTH_RETRY_SECONDS,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.state = state
        self.vault = vault
        self.store = store
        self._databases = databases
        self._health = health
        self._allowlist = allowlist
        self._on_change = on_change
        self._url_timeout = url_timeout
        self._health_retry = health_retry
        self._which = which
        self._lock = threading.RLock()
        self._run: _Run | None = None

    # ----------------------------------------------------------------------- consulta

    def current(self) -> TunnelJob | None:
        with self._lock:
            return self._run.snapshot() if self._run is not None else None

    def is_active(self) -> bool:
        """Subindo ou no ar."""
        with self._lock:
            return self._run is not None and self._run.state in (STARTING, UP)

    def blocked_reason(self) -> str | None:
        if self.is_active():
            return "o túnel já está subindo ou no ar"
        return None

    # ----------------------------------------------------------------------- controle

    def start(self) -> TunnelJob:
        with self._lock:
            if (reason := self.blocked_reason()) is not None:
                raise ConfigError(reason)
            config = self.store.load()
            if config is None:
                raise ConfigError("configure o comando do túnel antes de expor")
            run = _Run()
            run.secrets = tuple(v for n in config.env_names if (v := env_value(n)) is not None)
            run.thread = threading.Thread(
                target=self._work, args=(run, config), name="tunnel", daemon=True
            )
            self._run = run
            run.thread.start()
            return run.snapshot()

    def stop(self) -> TunnelJob | None:
        with self._lock:
            run = self._run
            if run is None:
                return None
            if run.state in (STARTING, UP):
                run.stop.set()
                run.state = STOPPED
                run.stage = None
        self._teardown(run)
        if run.thread is not None and run.thread is not threading.current_thread():
            run.thread.join(timeout=15)
        self._notify(run)
        return run.snapshot()

    def shutdown(self) -> None:
        self.stop()

    def wait(self, timeout: float = 30.0) -> TunnelJob | None:
        with self._lock:
            run = self._run
        if run is not None and run.thread is not None:
            run.thread.join(timeout)
        return self.current()

    # ----------------------------------------------------------------------- trabalho

    def _set(self, run: _Run, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(run, key, value)

    def _notify(self, run: _Run) -> None:
        if self._on_change is not None:
            try:
                self._on_change(run.snapshot())
            except Exception:  # um ouvinte ruim não derruba o túnel
                logger.exception("ouvinte do túnel falhou")

    def _fail(self, run: _Run, message: str) -> None:
        self._set(run, state=FAILED, error=message)
        logger.warning("túnel: %s", message)
        self._teardown(run)
        self._notify(run)

    def _teardown(self, run: _Run) -> None:
        if run.host is not None:
            hosts = [h for h in self.state.public_host_list() if h != run.host]
            self.state.set_public_hosts(hosts)
        process = run.process
        if process is not None:
            kill_tree(process, "túnel")

    def _work(self, run: _Run, config: TunnelConfig) -> None:
        if not self._prechecks(run, config) or not self._secrets(run):
            return
        if not self._launch(run, config):
            return
        if not self._find_url(run, config):
            return
        self._healthcheck(run)

    def _prechecks(self, run: _Run, config: TunnelConfig) -> bool:
        if not access_tokens.has_active_token(self.vault):
            self._fail(run, "crie um token em Acesso antes de expor o MCP")
            return False
        if not self.state.mcp_available:
            self._fail(run, "o MCP não está montado nesta interface (extra [mcp] ou --no-mcp)")
            return False
        if self._which(config.command[0]) is None:
            self._fail(
                run,
                f"comando não encontrado: {config.command[0]} "
                "(instale e configure o authtoken do serviço)",
            )
            return False
        return True

    def _secrets(self, run: _Run) -> bool:
        self._set(run, stage="secrets")
        allowed = secret_scan.load_allowlist(self._allowlist)
        opened: list[SecretFinding] = []
        for db in self._databases():
            for item in secret_scan.open_findings(secret_scan.scan(db), allowed):
                opened.append(
                    SecretFinding(item.pattern, f"{item.table} {item.key}", item.masked, item.hash)
                )
        if opened:
            self._set(run, findings=opened)
            self._fail(
                run,
                f"a varredura de segredos achou {len(opened)} item(ns) fora da allowlist; "
                "revise-os antes de expor o mapa",
            )
            return False
        return not run.stop.is_set()

    def _launch(self, run: _Run, config: TunnelConfig) -> bool:
        if run.stop.is_set():
            return False
        self._set(run, stage="tunnel")
        port = str(self.state.port)
        argv = [p.replace("{port}", port).replace("{host}", "127.0.0.1") for p in config.command]
        env = {**os.environ, **{n: v for n in config.env_names if (v := env_value(n)) is not None}}
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        try:
            process = subprocess.Popen(  # noqa: S603 - lista de argumentos, sem shell
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, env=env, **options,
            )  # fmt: skip
        except OSError as exc:
            self._fail(run, f"não consegui iniciar {argv[0]}: {exc}")
            return False
        self._set(run, process=process)
        if run.stop.is_set():  # «Parar» chegou durante o `Popen`: não deixa processo órfão
            kill_tree(process, "túnel")
            return False
        threading.Thread(target=self._read, args=(run, config, process), daemon=True).start()
        return True

    def _read(self, run: _Run, config: TunnelConfig, process: "subprocess.Popen[str]") -> None:
        pattern = re.compile(config.url_regex)
        assert process.stdout is not None
        for raw in process.stdout:
            line = mask_line(raw, run.secrets)
            with self._lock:
                run.log.append(line)
            if run.url is None and config.public_url is None:
                match = pattern.search(raw)
                if match:
                    self._set(run, url=match.group(0))
                    run.url_found.set()
        process.wait()
        with self._lock:
            was = run.state
            if was == UP:
                run.state, run.stage = DOWN, None
                run.error = "o túnel caiu (veja o fim do log)"
        if was == UP:
            self._teardown(run)
            self._notify(run)
        run.url_found.set()  # destrava a espera da URL se o processo morreu cedo

    def _find_url(self, run: _Run, config: TunnelConfig) -> bool:
        self._set(run, stage="url")
        if config.public_url is not None:
            self._set(run, url=config.public_url)
        else:
            run.url_found.wait(self._url_timeout)
        if run.stop.is_set():
            return False
        process = run.process
        if run.url is None:
            died = process is not None and process.poll() is not None
            self._fail(
                run,
                "o túnel terminou antes de mostrar a URL pública (veja o log)"
                if died
                else f"não achei a URL pública em {int(self._url_timeout)} s "
                "(confira a expressão regular)",
            )
            return False
        parsed = urlparse(run.url)
        if parsed.scheme != "https" or not parsed.hostname:
            self._fail(run, "o túnel precisa entregar HTTPS")
            return False
        host = parsed.hostname.lower()
        self._set(run, host=host, url=f"https://{host}")
        self.state.add_public_host(host)  # o `HostGate` passa a aceitar o host do túnel
        return True

    def _healthcheck(self, run: _Run) -> None:
        self._set(run, stage="health")
        assert run.url is not None
        deadline = time.monotonic() + self._health_retry
        result = HealthResult(False, "sem resposta")
        while not run.stop.is_set():
            result = self._health(run.url)
            process = run.process
            if (
                result.ok
                or time.monotonic() >= deadline
                or (process and process.poll() is not None)
            ):
                break
            time.sleep(1.0 if self._health_retry >= 1 else 0.01)
        if run.stop.is_set():
            return
        if not result.ok:
            self._fail(run, f"o health pela URL pública falhou: {result.detail}")
            return
        with self._lock:
            run.url = f"{run.url}/mcp"
            run.state, run.stage = UP, None
        logger.info("túnel no ar: %s (%s)", run.url, result.detail)
        self._notify(run)
