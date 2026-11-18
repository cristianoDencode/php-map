"""`scan --progress-json`: os eventos que a interface lê no stdout do subprocesso.

Sobre as fixtures com o cache de AST semeado (como `test_scan_pipeline`): tudo vem do cache,
então o scan não chama o Docker e não emite a etapa `extract`.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from php_map import config
from php_map.cli import STAGE_LABELS, app
from php_map.config import Settings
from php_map.ingest import ast_cache

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MINI = FIXTURES / "loja-mini"
REFERENCE = FIXTURES / "ast/loja-mini.jsonl"
DDL = FIXTURES / "schema/loja_mini.sql"
CATALOG = FIXTURES / "catalog/loja_mini.sql"


def _seed_cache(out: Path) -> None:
    ast_dir = ast_cache.cache_dir(out, MINI)
    ast_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REFERENCE, ast_dir / ast_cache.CACHE_NAME)
    ast_cache.write_meta(ast_dir, ast_cache.current_meta(MINI, Settings()))


def _events(result: Result) -> list[dict[str, Any]]:
    """Cada linha do stdout é um objeto JSON (falha se houver qualquer outra coisa)."""
    lines = result.stdout.splitlines()
    assert lines, result.output
    return [json.loads(line) for line in lines]


def _scan(*args: str) -> Result:
    return CliRunner().invoke(app, ["scan", "--progress-json", *args])


def test_events_from_start_to_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.JOB_ID_ENV, "t25")
    out, db = tmp_path / "out", tmp_path / "out/grafos/2026-09-25T10-00-00.db"
    _seed_cache(out)
    result = _scan(
        *("--source", str(MINI), "--schema", str(DDL), "--catalog", str(CATALOG)),
        *("--out", str(out), "--db", str(db), "--reuse-ast"),
    )

    assert result.exit_code == 0, result.output
    events = _events(result)
    assert [e["event"] for e in events] == ["start", *["stage"] * 15, "done"]
    start, done = events[0], events[-1]
    assert start["job"] == "t25"
    assert Path(start["source"]) == MINI.resolve() and Path(start["db"]) == db.resolve()

    stages = [e["stage"] for e in events if e["event"] == "stage"]
    assert "extract" not in stages  # tudo veio do cache
    # Na ordem de STAGE_LABELS; `zend1_structure` e `doctrine` só existem no perfil zend1.
    skipped = {"extract", "zend1_structure", "doctrine"}
    assert stages == [s for s in STAGE_LABELS if s not in skipped]
    assert all(e["label"] == STAGE_LABELS[e["stage"]] for e in events if e["event"] == "stage")
    assert all(e["at"] for e in events)

    assert Path(done["db"]) == db.resolve() and db.is_file()
    assert done["seconds"] >= 0
    summary = done["summary"]
    assert set(summary) == {
        "nodes",
        "edges",
        "modules",
        "screens_in_menu",
        "files",
        "routines",
        "warnings",
    }
    assert summary["files"] == 25 and summary["nodes"] > 0 and summary["edges"] > 0
    assert summary["modules"] == 2 and summary["screens_in_menu"] >= 1
    assert "Resumo do scan" in result.stderr  # o resumo impresso continua, no stderr
    # com o banco fora de `--out`, o resumo e as pendências ficam ao lado dele.
    assert (db.parent / "2026-09-25T10-00-00.scan_summary.json").is_file()
    assert (db.parent / "2026-09-25T10-00-00.unresolved.jsonl").is_file()
    assert not (out / "scan_summary.json").exists() and not (out / "unresolved.jsonl").exists()


def test_invalid_source_is_an_error_event(tmp_path: Path) -> None:
    result = _scan("--source", str(tmp_path), "--out", str(tmp_path / "out"))

    assert result.exit_code == 1
    events = _events(result)
    assert [e["event"] for e in events] == ["error"]
    assert (
        "perfil 'ecidade'" in events[0]["message"] and "libs/db_stdlib.php" in events[0]["message"]
    )


def test_usage_error_is_an_error_event(tmp_path: Path) -> None:
    result = _scan(
        *("--source", str(MINI), "--catalog", str(CATALOG), "--catalog-db", "host=x"),
        *("--out", str(tmp_path / "out")),
    )

    assert result.exit_code == 1
    assert [e["event"] for e in _events(result)] == ["error"]


def test_unexpected_error_hides_the_dsn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dsn = "host=db.exemplo dbname=ecidade user=u password=segredo"

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"falhou com {dsn}")

    monkeypatch.setattr("php_map.pipeline.run_scan", broken)
    result = _scan("--source", str(MINI), "--catalog-db", dsn, "--out", str(tmp_path / "out"))

    assert result.exit_code == 1
    events = _events(result)
    assert events[0]["event"] == "start" and events[-1]["event"] == "error"
    assert "RuntimeError" in events[-1]["message"]
    assert "segredo" not in result.stdout and "segredo" not in result.stderr
