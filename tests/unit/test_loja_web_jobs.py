"""`ScanJobManager` e `ui-settings.json` com um `scan` e um `docker` falsos.

Os dois são scripts Python gerados no `tmp_path`: o `scan` emite os eventos do
`--progress-json` conforme `FAKE_SCAN_MODE`; o `docker` responde `ps` e `kill` a partir de um
arquivo de estado. O encerramento da árvore do processo é o real (`taskkill` no Windows).
"""

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from php_map import config
from php_map.errors import ConfigError
from php_map.web.jobs import ScanJob, ScanJobManager, ScanRequest
from php_map.web.settings import MAX_RECENT, UiSettingsStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MINI = FIXTURES / "loja-mini"
DDL = FIXTURES / "schema/loja_mini.sql"

FAKE_SCAN = r"""
import json, os, sys, time
args = sys.argv[1:]
db = args[args.index("--db") + 1]
mode = os.environ.get("FAKE_SCAN_MODE", "ok")
job = os.environ["PHP_MAP_JOB_ID"]
def emit(**event):
    print(json.dumps(event), flush=True)
def beside(name):  # o resumo e as pendências ao lado do banco
    open(db[: -len(".db")] + "." + name, "w").close()
emit(event="start", job=job, db=db)
with open(os.environ["FAKE_SCAN_ARGS"], "w", encoding="utf-8") as out:
    json.dump(args, out)
if mode == "ok":
    for stage in ("discovery", "hash", "save"):
        emit(event="stage", stage=stage, label="etapa " + stage, at="2026-09-25T10:00:00-03:00")
    open(db, "wb").close()
    beside("scan_summary.json")
    beside("unresolved.jsonl")
    print("resumo no stderr", file=sys.stderr, flush=True)
    emit(event="done", db=db, seconds=1.5, summary={"nodes": 10, "edges": 20, "modules": 3,
         "screens_in_menu": 2, "files": 34, "warnings": []})
elif mode == "sleep":
    emit(event="stage", stage="extract", label="Extraindo AST PHP", at="2026-09-25T10:00:00-03:00")
    open(db + ".tmp", "wb").close()
    beside("unresolved.jsonl")
    state = os.environ["FAKE_DOCKER_STATE"]
    names = [f"ecmap-scan-{job}-1", f"ecmap-scan-{job}-2", "ecmap-scan-outrojob-1",
             "postgres_interno", f"x-ecmap-scan-{job}-7"]
    with open(state, "w", encoding="utf-8") as out:
        json.dump({"running": names, "killed": []}, out)
    time.sleep(120)
elif mode == "crash":
    for number in range(250):
        print(f"linha {number}", file=sys.stderr)
    print("Traceback: algo quebrou", file=sys.stderr, flush=True)
    sys.exit(3)
elif mode == "error":
    emit(event="error", message="não parece um e-cidade")
    sys.exit(1)
"""

FAKE_DOCKER = r"""
import json, os, sys
state_path = os.environ["FAKE_DOCKER_STATE"]
try:
    with open(state_path, encoding="utf-8") as handle:
        state = json.load(handle)
except FileNotFoundError:
    state = {"running": [], "killed": []}
args = sys.argv[1:]
if args[0] == "ps":
    wanted = args[args.index("--filter") + 1].removeprefix("name=")
    print("\n".join(n for n in state["running"] if wanted in n))  # trecho, como o Docker
elif args[0] == "kill":
    state["running"].remove(args[1])
    state["killed"].append(args[1])
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
"""


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.out = tmp_path / "out"
        self.out.mkdir()
        scan, docker = tmp_path / "fake_scan.py", tmp_path / "fake_docker.py"
        scan.write_text(FAKE_SCAN, encoding="utf-8")
        docker.write_text(FAKE_DOCKER, encoding="utf-8")
        self.docker_state = tmp_path / "docker.json"
        self.args_file = tmp_path / "args.json"
        monkeypatch.setenv("FAKE_DOCKER_STATE", str(self.docker_state))
        monkeypatch.setenv("FAKE_SCAN_ARGS", str(self.args_file))
        self.monkeypatch = monkeypatch
        self.switched: list[Path] = []
        self.settings = UiSettingsStore(self.out)
        self.manager = ScanJobManager(
            self.out,
            self.settings,
            self.switched.append,
            command=[sys.executable, str(scan)],
            docker=[sys.executable, str(docker)],
        )

    def mode(self, mode: str) -> None:
        self.monkeypatch.setenv("FAKE_SCAN_MODE", mode)

    def run(self, mode: str = "ok", **request: object) -> ScanJob:
        self.mode(mode)
        self.manager.start(ScanRequest(MINI, catalog_from_env=False, **request))  # type: ignore[arg-type]
        job = self.manager.wait(timeout=30)
        assert job is not None and job.state != "running"
        return job

    def args(self) -> list[str]:
        data: list[str] = json.loads(self.args_file.read_text(encoding="utf-8"))
        return data

    def graphs(self) -> list[str]:
        return sorted(p.name for p in (self.out / "grafos").glob("*.db"))


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


def _until(condition: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "tempo esgotado"
        time.sleep(0.05)


# --------------------------------------------------------------------------- sucesso


def test_success_switches_the_graph_and_saves_last_db(harness: Harness) -> None:
    job = harness.run("ok", schema=(DDL,))

    assert job.state == "succeeded" and job.error is None
    db = Path(job.db)
    assert db.parent == (harness.out / "grafos").resolve() and db.is_file()
    assert harness.switched == [db]
    assert harness.settings.load().last_db == str(db)
    assert [s.stage for s in job.stages] == ["discovery", "hash", "save"]
    assert job.stage == "save" and job.stage_label == "etapa save"
    assert job.result is not None and job.result["summary"]["modules"] == 3
    assert job.finished_at is not None and job.elapsed_seconds >= 0
    assert "resumo no stderr" in job.log_tail

    args = harness.args()
    assert args[:3] == ["scan", "--source", str(MINI.resolve())]
    assert args[args.index("--out") + 1] == str(harness.out.resolve())
    assert args[args.index("--schema") + 1] == str(DDL.resolve())
    assert {"--progress-json", "--reuse-ast", "--no-catalog-db"} <= set(args)
    assert "--no-search" not in args and "--no-analytics" not in args
    assert not any("password" in a or "host=" in a for a in args)  # o DSN nunca vai no comando

    settings = harness.settings.load()
    assert settings.source == str(MINI.resolve()) and settings.schema == [str(DDL.resolve())]
    assert settings.catalog_from_env is False and settings.recent_sources == [str(MINI.resolve())]


def test_options_become_flags(harness: Harness) -> None:
    harness.run("ok", reuse_ast=False, search=False, analytics=False)
    args = harness.args()
    assert "--reuse-ast" not in args and {"--no-search", "--no-analytics"} <= set(args)


def test_schema_from_db_becomes_the_flag_without_the_dsn(harness: Harness) -> None:
    """`--schema-db`, com ou sem o catálogo; o DSN fica no ambiente do filho."""
    harness.monkeypatch.setenv(config.CATALOG_DSN_ENV, "host=db.exemplo password=segredo")
    job = harness.run("ok", schema_from_db=True)  # catalog_from_env=False no `run`

    assert job.state == "succeeded"
    args = harness.args()
    assert {"--schema-db", "--no-catalog-db"} <= set(args) and "--schema" not in args
    assert not any("segredo" in a or "host=" in a for a in args)
    assert harness.settings.load().schema_source == "db"

    harness.mode("ok")
    harness.manager.start(ScanRequest(MINI, schema_from_db=True, catalog_from_env=True))
    harness.manager.wait(timeout=30)
    args = harness.args()
    assert "--schema-db" in args and "--no-catalog-db" not in args


def test_schema_from_db_is_refused_with_paths_or_without_dsn(harness: Harness) -> None:
    with pytest.raises(ConfigError, match="schema do banco indisponível"):
        harness.manager.start(ScanRequest(MINI, schema_from_db=True, catalog_from_env=False))
    harness.monkeypatch.setenv(config.CATALOG_DSN_ENV, "host=db.exemplo")
    with pytest.raises(ConfigError, match="não os dois"):
        harness.manager.start(
            ScanRequest(MINI, schema=(DDL,), schema_from_db=True, catalog_from_env=False)
        )
    assert harness.manager.current() is None


def test_schema_source_is_remembered(harness: Harness) -> None:
    harness.run("ok", schema=(DDL,))
    assert harness.settings.load().schema_source == "files"
    harness.run("ok")
    assert harness.settings.load().schema_source == "none"


# --------------------------------------------------------------------------- falhas


def test_nonzero_exit_without_error_event_fails_with_the_log(harness: Harness) -> None:
    job = harness.run("crash")

    assert job.state == "failed" and "código 3" in (job.error or "")
    assert job.log_tail[-1] == "Traceback: algo quebrou"
    assert len(job.log_tail) == 200  # só o fim do stderr
    assert harness.switched == [] and harness.settings.load().last_db is None


def test_error_event_is_the_failure_message(harness: Harness) -> None:
    job = harness.run("error")
    assert job.state == "failed" and job.error == "não parece um e-cidade"
    assert harness.switched == []


def test_invalid_requests_are_refused(harness: Harness, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="perfil 'ecidade'.*libs/db_stdlib.php"):
        harness.manager.start(ScanRequest(tmp_path, catalog_from_env=False))
    with pytest.raises(ConfigError, match="schema inexistente"):
        harness.manager.start(
            ScanRequest(MINI, schema=(tmp_path / "nao.sql",), catalog_from_env=False)
        )
    with pytest.raises(ConfigError, match=config.CATALOG_DSN_ENV):
        harness.manager.start(ScanRequest(MINI, catalog_from_env=True))  # sem .env nos testes
    assert harness.manager.current() is None


# --------------------------------------------------------------------------- um por vez e cancelar


def test_second_start_is_refused_and_cancel_kills_only_this_job(harness: Harness) -> None:
    harness.mode("sleep")
    job = harness.manager.start(ScanRequest(MINI, catalog_from_env=False))
    _until(lambda: harness.docker_state.is_file() and harness.manager.current().stage == "extract")  # type: ignore[union-attr]
    db = Path(job.db)
    assert db.with_name(db.name + ".tmp").is_file()

    with pytest.raises(ConfigError, match="em andamento"):
        harness.manager.start(ScanRequest(MINI, catalog_from_env=False))

    started = time.monotonic()
    canceled = harness.manager.cancel()
    assert time.monotonic() - started < 10

    assert canceled.state == "canceled" and canceled.finished_at is not None
    state = json.loads(harness.docker_state.read_text(encoding="utf-8"))
    assert sorted(state["killed"]) == [f"ecmap-scan-{job.id}-1", f"ecmap-scan-{job.id}-2"]
    assert "ecmap-scan-outrojob-1" in state["running"] and "postgres_interno" in state["running"]
    assert not db.exists() and not db.with_name(db.name + ".tmp").exists()
    assert not db.with_name(f"{db.stem}.unresolved.jsonl").exists()
    assert harness.switched == []
    assert harness.manager.cancel().state == "canceled"  # repetir não muda nada

    after = harness.run("ok")  # o scan seguinte roda normalmente
    assert after.state == "succeeded"


def test_cancel_without_scan_is_an_error(harness: Harness) -> None:
    with pytest.raises(ConfigError, match="nenhum scan"):
        harness.manager.cancel()


# --------------------------------------------------------------------------- retenção


def test_retention_keeps_two_and_never_the_graph_in_use(harness: Harness) -> None:
    outside = harness.out / "graph.db"
    outside.write_bytes(b"")
    jobs = [harness.run("ok") for _ in range(3)]

    assert all(j.state == "succeeded" for j in jobs)
    assert harness.graphs() == sorted(Path(j.db).name for j in jobs[1:])
    # o resumo e as pendências saem junto com o banco apagado.
    kept = sorted(p.name for p in (harness.out / "grafos").iterdir() if p.suffix != ".db")
    assert kept == sorted(
        f"{Path(j.db).stem}.{name}"
        for j in jobs[1:]
        for name in ("scan_summary.json", "unresolved.jsonl")
    )
    assert outside.is_file()  # fora de grafos/: nunca apagado

    # Dois arquivos "mais novos" que o grafo em uso: ele fica mesmo assim.
    future = time.time() + 3600
    for name in ("z1.db", "z2.db"):
        path = harness.out / "grafos" / name
        path.write_bytes(b"")
        os.utime(path, (future, future))
    last = harness.run("ok")
    assert Path(last.db).is_file() and harness.switched[-1] == Path(last.db)
    assert harness.graphs() == sorted([Path(last.db).name, "z1.db", "z2.db"])


# --------------------------------------------------------------------------- ui-settings.json


def test_recent_sources_are_limited_and_unique(tmp_path: Path) -> None:
    store = UiSettingsStore(tmp_path)
    for number in range(7):
        store.remember_source(f"D:/ecidade{number}")
    store.remember_source("d:/ECIDADE3" if os.name == "nt" else "D:/ecidade3")

    recent = store.load().recent_sources
    assert len(recent) == MAX_RECENT == 5
    assert recent[0].lower() == "d:/ecidade3" and len({r.lower() for r in recent}) == 5
    assert recent[1:] == ["D:/ecidade6", "D:/ecidade5", "D:/ecidade4", "D:/ecidade2"]


def test_invalid_settings_file_is_tolerated(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.settings.path.write_text("{ isto não é json", encoding="utf-8")
    assert harness.settings.load().source is None
    assert "inválido" in caplog.text

    job = harness.run("ok")  # grava de novo, válido
    assert job.state == "succeeded"
    assert json.loads(harness.settings.path.read_text(encoding="utf-8"))["last_db"] == job.db


def test_wrong_types_fall_back_field_by_field(tmp_path: Path) -> None:
    store = UiSettingsStore(tmp_path)
    store.path.write_text(
        json.dumps({"source": 3, "schema": ["a.sql", 7], "reuse_ast": "sim", "last_db": "x.db"}),
        encoding="utf-8",
    )
    settings = store.load()
    assert settings.source is None and settings.schema == ["a.sql"]
    assert settings.reuse_ast is True and settings.last_db == "x.db"
    assert not list(tmp_path.glob("*.tmp"))


def test_schema_source_accepts_only_the_three_values(tmp_path: Path) -> None:
    """Arquivo antigo (sem o campo) e valor inválido viram `None`."""
    store = UiSettingsStore(tmp_path)
    store.path.write_text(json.dumps({"source": "x", "schema": ["a.sql"]}), encoding="utf-8")
    assert store.load().schema_source is None
    cases: list[tuple[object, str | None]] = [
        ("db", "db"), ("files", "files"), ("none", "none"), ("DB", None), (1, None),
    ]  # fmt: skip
    for value, expected in cases:
        store.path.write_text(json.dumps({"schema_source": value}), encoding="utf-8")
        assert store.load().schema_source == expected
