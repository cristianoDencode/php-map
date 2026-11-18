"""Scan pela interface: um subprocesso por vez, acompanhado e cancelável.

O servidor roda `python -m php_map scan … --progress-json` e lê os eventos do stdout (uma
linha JSON por evento) e o fim do stderr (log). O grafo novo vai para outro arquivo
(`<out>/grafos/<data-hora>.db`, porque o `os.replace` falha no Windows com o banco aberto) e o
cache de AST continua em `<out>/ast`. No sucesso, `on_switch` troca o banco servido.

Cancelar encerra a árvore do processo (o cliente `docker run` e o Python) e depois os
containers **deste** job, pelo nome (`ecmap-scan-<job>-*`, `ambiente.md`). O DSN do catálogo
nunca passa pela linha de comando: o subprocesso herda o ambiente e a CLI lê o `.env`.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from php_map import config, fsperm
from php_map.errors import ConfigError
from php_map.ingest.php_extractor import CONTAINER_PREFIX
from php_map.pipeline import SUMMARY_NAME, UNRESOLVED_NAME, validate_source
from php_map.web.settings import UiSettingsStore

logger = logging.getLogger(__name__)

GRAPHS_DIR = "grafos"
KEEP_GRAPHS = 2
LOG_TAIL = 200
DOCKER_TIMEOUT = 20.0  # segundos por comando `docker` no cancelamento
KILL_ROUNDS = 3  # conferências do `docker ps` depois de encerrar a árvore

RUNNING, SUCCEEDED, FAILED, CANCELED = "running", "succeeded", "failed", "canceled"


@dataclass(frozen=True, slots=True)
class ScanRequest:
    source: Path
    schema: tuple[Path, ...] = ()
    schema_from_db: bool = False  # `--schema-db`: DSN do `.env`, nunca na linha
    catalog_from_env: bool = True
    reuse_ast: bool = True
    search: bool = True
    analytics: bool = True
    # Projeto: sem `slug`, o scan é o de antes (perfil e nome da pasta por padrão).
    slug: str | None = None
    name: str | None = None
    profile: str | None = None
    dsn_env: str | None = None  # NOME da variável do `.env`; o valor nunca passa pela API


@dataclass(frozen=True, slots=True)
class ScanStage:
    stage: str
    label: str
    at: str


@dataclass(frozen=True, slots=True)
class ScanJob:
    """Retrato de um scan (o que a API devolve)."""

    id: str
    state: str
    stage: str | None
    stage_label: str | None
    stages: tuple[ScanStage, ...]
    started_at: str
    finished_at: str | None
    elapsed_seconds: float
    log_tail: tuple[str, ...]
    result: dict[str, Any] | None
    error: str | None
    db: str
    slug: str | None = None  # o projeto escaneado (atalho "Publicar no MCP")


@dataclass(slots=True)
class _Run:
    """Estado mutável do scan, sempre alterado com o lock do gerenciador."""

    id: str
    db: Path
    process: "subprocess.Popen[str]"
    started_at: str
    started: float
    state: str = RUNNING
    stage: str | None = None
    stage_label: str | None = None
    stages: list[ScanStage] = field(default_factory=list)
    log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_TAIL))
    result: dict[str, Any] | None = None
    error: str | None = None
    finished_at: str | None = None
    finished: float | None = None
    cancel_requested: bool = False
    finishing: bool = False
    watcher: threading.Thread | None = None
    slug: str | None = None

    def snapshot(self) -> ScanJob:
        end = self.finished if self.finished is not None else time.monotonic()
        return ScanJob(
            id=self.id,
            state=self.state,
            stage=self.stage,
            stage_label=self.stage_label,
            stages=tuple(self.stages),
            started_at=self.started_at,
            finished_at=self.finished_at,
            elapsed_seconds=round(end - self.started, 1),
            log_tail=tuple(self.log),
            result=self.result,
            error=self.error,
            db=str(self.db),
            slug=self.slug,
        )


def companions(db: Path) -> tuple[Path, Path]:
    """O resumo e as pendências gravados ao lado de um banco de `grafos/`."""
    return (
        db.with_name(f"{db.stem}.{SUMMARY_NAME}"),
        db.with_name(f"{db.stem}.{UNRESOLVED_NAME}"),
    )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def kill_tree(process: "subprocess.Popen[str]", what: str = "scan") -> None:
    """Encerra o processo e os filhos dele (o `docker run` do scan)."""
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # `terminate()` encerraria só o Python; o filho continuaria.
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=DOCKER_TIMEOUT,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("falha ao encerrar a árvore do %s: %s", what, exc)
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        logger.warning("o processo do %s não terminou em 10 s", what)


class ScanJobManager:
    """Inicia, acompanha e cancela o scan; no sucesso, troca o grafo e aplica a retenção."""

    def __init__(
        self,
        out_dir: Path,
        settings: UiSettingsStore,
        on_switch: Callable[[Path], None],
        command: Sequence[str] | None = None,
        docker: Sequence[str] = ("docker",),
        on_project_scanned: Callable[[str, Path], None] | None = None,
    ) -> None:
        self.out_dir = out_dir.resolve()
        self.settings = settings
        self.on_switch = on_switch
        self.on_project_scanned = on_project_scanned
        self.command = list(command) if command is not None else [sys.executable, "-m", "php_map"]
        self.docker = list(docker)
        self._lock = threading.Lock()
        self._run: _Run | None = None

    @property
    def graphs_dir(self) -> Path:
        return self.out_dir / GRAPHS_DIR

    # ----------------------------------------------------------------------- consulta

    def current(self) -> ScanJob | None:
        with self._lock:
            return self._run.snapshot() if self._run is not None else None

    def current_slug(self) -> str | None:
        """O projeto do scan atual ou do último (None = scan sem projeto)."""
        with self._lock:
            return self._run.slug if self._run is not None else None

    def is_running(self) -> bool:
        with self._lock:
            return self._run is not None and self._run.state == RUNNING

    def wait(self, timeout: float | None = None) -> ScanJob | None:
        """Espera o scan atual terminar (testes e encerramento do servidor)."""
        with self._lock:
            watcher = self._run.watcher if self._run is not None else None
        if watcher is not None:
            watcher.join(timeout)
        return self.current()

    # ----------------------------------------------------------------------- início

    def start(self, request: ScanRequest) -> ScanJob:
        profile = request.profile or config.DEFAULT_PROFILE
        source = validate_source(request.source, profile, hint="").resolve()
        dsn_name = request.dsn_env or config.CATALOG_DSN_ENV
        # Só o ecidade tem o dicionário de dados e os menus: outros perfis não leem catálogo.
        reads_catalog = request.catalog_from_env and profile == config.DEFAULT_PROFILE
        schema = [path.resolve() for path in request.schema]
        missing = [str(path) for path in schema if not path.exists()]
        if missing:
            raise ConfigError(f"caminho de schema inexistente: {', '.join(missing)}")
        if request.schema_from_db and schema:
            raise ConfigError("use arquivos de schema ou o schema do banco, não os dois")
        if request.schema_from_db and not config.env_value(dsn_name):
            raise ConfigError(
                f"schema do banco indisponível: defina {dsn_name} no .env "
                "ou escolha outra fonte de schema"
            )
        if reads_catalog and not config.env_value(dsn_name):
            raise ConfigError(
                f"catálogo do banco indisponível: defina {dsn_name} no .env "
                "ou desmarque a leitura do catálogo"
            )
        with self._lock:
            if self._run is not None and self._run.state == RUNNING:
                raise ConfigError("já há um scan em andamento: espere terminar ou cancele")
            job = uuid.uuid4().hex[:12]
            db = self._new_db_path(request.slug)
            args = [
                *self.command, "scan",
                "--source", str(source),
                "--out", str(self.out_dir),
                "--db", str(db),
                "--progress-json",
            ]  # fmt: skip
            if request.slug:  # O `meta.project` do grafo é o slug do cadastro
                args += ["--profile", profile]
                args += ["--project", request.slug, "--project-name", request.name or request.slug]
            for path in schema:
                args += ["--schema", str(path)]
            if request.schema_from_db:
                args.append("--schema-db")  # o subprocesso lê o DSN do `.env`
            else:
                # Vale a escolha da tela; sem isso a CLI leria do banco sozinha.
                args.append("--no-schema-db")
            if request.reuse_ast:
                args.append("--reuse-ast")
            if not reads_catalog:
                args.append("--no-catalog-db")
            if not request.search:
                args.append("--no-search")
            if not request.analytics:
                args.append("--no-analytics")
            env = {
                **os.environ,
                config.JOB_ID_ENV: job,
                "PYTHONIOENCODING": "utf-8",
                "COLUMNS": "120",  # o resumo do Rich no log, sem quebrar em 80 colunas
            }
            if dsn_name != config.CATALOG_DSN_ENV:
                # A conexão do projeto vai pelo ambiente do subprocesso (a CLI lê
                # `PHP_MAP_CATALOG_DSN`): nunca pela linha de comando nem pela API.
                dsn = config.env_value(dsn_name)
                if dsn:
                    env[config.CATALOG_DSN_ENV] = dsn
            logger.info("scan %s: %s", job, " ".join(args[len(self.command) :]))
            process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                start_new_session=sys.platform != "win32",  # POSIX: grupo próprio (killpg)
            )
            run = _Run(job, db, process, _now(), time.monotonic(), slug=request.slug)
            stderr = threading.Thread(target=self._read_stderr, args=(run,), daemon=True)
            run.watcher = threading.Thread(
                target=self._watch, args=(run, stderr), name=f"scan-{job}", daemon=True
            )
            self._run = run
            stderr.start()
            run.watcher.start()
            snapshot = run.snapshot()
        self.settings.remember_source(
            str(source),
            schema=[str(path) for path in schema],
            schema_source="db" if request.schema_from_db else ("files" if schema else "none"),
            catalog_from_env=request.catalog_from_env,
            reuse_ast=request.reuse_ast,
        )
        return snapshot

    def _new_db_path(self, slug: str | None = None) -> Path:
        """`grafos/<data-hora>.db`, ou `grafos/<slug>/<data-hora>.db` num scan de projeto (cada
        projeto guarda as suas 2 versões)."""
        folder = self.graphs_dir / slug if slug else self.graphs_dir
        folder.mkdir(parents=True, exist_ok=True)
        fsperm.secure_dir(folder)
        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        db, suffix = folder / f"{stamp}.db", 2
        while db.exists():  # dois scans no mesmo segundo
            db, suffix = folder / f"{stamp}-{suffix}.db", suffix + 1
        return db

    # ----------------------------------------------------------------------- leitura

    def _read_stderr(self, run: _Run) -> None:
        assert run.process.stderr is not None
        for line in run.process.stderr:
            with self._lock:
                run.log.append(line.rstrip("\r\n"))

    def _watch(self, run: _Run, stderr: threading.Thread) -> None:
        assert run.process.stdout is not None
        for line in run.process.stdout:
            self._on_line(run, line.strip())
        returncode = run.process.wait()
        stderr.join(timeout=10)
        self._finish(run, returncode)

    def _on_line(self, run: _Run, line: str) -> None:
        if not line:
            return
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        with self._lock:
            if not isinstance(event, dict):
                run.log.append(line)  # fora do contrato: vai para o log, não quebra o job
                return
            kind = event.get("event")
            if kind == "stage":
                stage, label = str(event.get("stage")), str(event.get("label"))
                run.stage, run.stage_label = stage, label
                run.stages.append(ScanStage(stage, label, str(event.get("at") or _now())))
            elif kind == "done":
                run.result = event
            elif kind == "error":
                run.error = str(event.get("message") or "erro sem mensagem")

    def _finish(self, run: _Run, returncode: int) -> None:
        with self._lock:
            if run.cancel_requested:
                return  # quem cancelou termina o job
            run.finishing = True
            result, error = run.result, run.error
        state = FAILED
        if returncode == 0 and result is not None and error is None:
            try:
                if not run.db.is_file():
                    raise ConfigError("o scan terminou, mas o banco novo não existe")
                self.settings.update(last_db=str(run.db))
                if run.slug and self.on_project_scanned is not None:
                    self.on_project_scanned(run.slug, run.db)  # grava o projeto; troca se ativo
                else:
                    self.on_switch(run.db)  # por último: se algo antes falhar, nada muda
                state = SUCCEEDED
            except Exception as exc:  # o servidor continua no grafo anterior
                logger.exception("scan %s: grafo novo não aplicado", run.id)
                error = f"o grafo novo não pôde ser usado: {exc}"
            if state == SUCCEEDED:
                self._retain(run.db)
        elif error is None:
            error = f"o scan terminou com código {returncode} sem informar o erro (veja o log)"
        with self._lock:
            run.state, run.error = state, error
            run.finished_at, run.finished = _now(), time.monotonic()
        logger.info("scan %s: %s", run.id, state)

    # ----------------------------------------------------------------------- retenção

    def _retain(self, in_use: Path) -> None:
        """Mantém os `KEEP_GRAPHS` `.db` mais recentes de `grafos/`; nunca apaga `in_use` nem
        nada fora de `grafos/`. Se o Windows recusar (arquivo aberto), tenta no próximo scan."""
        folder = in_use.resolve().parent  # `grafos/` ou `grafos/<slug>/`: a retenção é por pasta
        if folder != self.graphs_dir and folder.parent != self.graphs_dir:
            return
        graphs = [path for path in folder.glob("*.db") if path.is_file()]
        graphs.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
        keep = in_use.resolve()
        for old in graphs[KEEP_GRAPHS:]:
            if old.resolve() == keep or old.resolve().parent != folder:
                continue
            try:
                old.unlink()
                logger.info("grafo antigo apagado: %s", old.name)
            except OSError as exc:
                logger.warning("grafo antigo %s não apagado agora (%s)", old.name, exc)
                continue
            for extra in companions(old):  # o resumo e as pendências dele
                extra.unlink(missing_ok=True)

    # ----------------------------------------------------------------------- cancelamento

    def cancel(self) -> ScanJob:
        with self._lock:
            run = self._run
            if run is None:
                raise ConfigError("nenhum scan em andamento")
            if run.state != RUNNING or run.finishing or run.cancel_requested:
                return run.snapshot()
            run.cancel_requested = True
        logger.info("scan %s: cancelando", run.id)
        self._kill_tree(run.process)
        self.kill_containers(run.id)
        if run.watcher is not None:
            run.watcher.join(timeout=15)
        for path in (run.db, run.db.with_name(run.db.name + ".tmp"), *companions(run.db)):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("banco parcial %s não apagado (%s)", path.name, exc)
        with self._lock:
            run.state = CANCELED
            run.finished_at, run.finished = _now(), time.monotonic()
            return run.snapshot()

    def shutdown(self) -> None:
        """Servidor encerrando: não deixa scan nem container para trás."""
        if self.is_running():
            self.cancel()

    @staticmethod
    def _kill_tree(process: "subprocess.Popen[str]") -> None:
        kill_tree(process, "scan")

    def kill_containers(self, job: str) -> list[str]:
        """`docker kill` dos containers `ecmap-scan-<job>-*` (só deste job, pelo nome)."""
        prefix = f"{CONTAINER_PREFIX}{job}-"
        killed: list[str] = []
        for round_ in range(KILL_ROUNDS):
            names = self._docker_names(prefix)
            if not names:
                break
            for name in names:
                self._docker("kill", name)
                killed.append(name)
            if round_ < KILL_ROUNDS - 1:
                time.sleep(1.0)  # um `docker run` interrompido pode criar o container depois
        return killed

    def _docker_names(self, prefix: str) -> list[str]:
        listed = self._docker("ps", "--filter", f"name={prefix}", "--format", "{{.Names}}")
        # O filtro `name=` do Docker casa trecho do nome: confere o prefixo aqui também.
        return [name for name in listed.split() if name.startswith(prefix)]

    def _docker(self, *args: str) -> str:
        try:
            done = subprocess.run(
                [*self.docker, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=DOCKER_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("docker %s falhou: %s", args[0], exc)
            return ""
        return done.stdout or ""
