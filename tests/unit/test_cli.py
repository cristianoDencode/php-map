import pytest
from typer.testing import CliRunner

from php_map.cli import app
from php_map.errors import IngestError
from php_map.ingest import php_extractor

runner = CliRunner()


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("scan", "trace", "impact", "setup-extractor"):
        assert command in result.output


def test_trace_and_impact_require_their_target() -> None:
    for command in ("trace", "impact"):
        result = runner.invoke(app, [command])

        assert result.exit_code == 2  # uso incorreto (Typer): falta --entry / --table


def test_setup_extractor_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(php_extractor, "setup_extractor", lambda _settings: None)
    result = runner.invoke(app, ["setup-extractor"])

    assert result.exit_code == 0
    assert "pronto" in result.output


def test_setup_extractor_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_settings: object) -> None:
        raise IngestError("Docker não encontrado no PATH")

    monkeypatch.setattr(php_extractor, "setup_extractor", fail)
    result = runner.invoke(app, ["setup-extractor"])

    assert result.exit_code == 1
    assert "Docker não encontrado" in result.output
