"""Comando `php-map value-origin`."""

import json
from pathlib import Path

from test_value_origin import TABELA_A, TMP_CALC, _db
from typer.testing import CliRunner

from php_map import cli

runner = CliRunner()


def test_without_alvo_lists_candidates(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = runner.invoke(
        cli.app, ["value-origin", "valor m2 do item", "--db", str(db), "--funcao", "fc_root"]
    )
    assert result.exit_code == 0, result.output
    assert "Candidatos" in result.output and "tabela_a" in result.output


def test_without_alvo_json(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = runner.invoke(
        cli.app,
        [
            "value-origin",
            "valor m2 do item",
            "--db",
            str(db),
            "--funcao",
            "fc_root",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["function"] and payload["candidates"]


def test_with_alvo_prints_the_dossier(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = runner.invoke(
        cli.app,
        [
            "value-origin",
            "valor m2 do item",
            "--db",
            str(db),
            "--funcao",
            "fc_root",
            "--alvo",
            f"{TMP_CALC}#resultado",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "cadastro encontrado" in result.output


def test_with_alvo_writes_to_a_file(tmp_path: Path) -> None:
    db = _db(tmp_path)
    out = tmp_path / "dossie.md"
    result = runner.invoke(
        cli.app,
        [
            "value-origin",
            "valor m2 do item",
            "--db",
            str(db),
            "--funcao",
            "fc_root",
            "--alvo",
            f"{TABELA_A}#valor",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.is_file() and "hipótese" in out.read_text(encoding="utf-8")


def test_unknown_function_is_an_error(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = runner.invoke(
        cli.app, ["value-origin", "x", "--db", str(db), "--funcao", "fc_fantasma"]
    )
    assert result.exit_code == 1
    assert "função não encontrada" in result.output


def test_missing_db_is_an_error(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["value-origin", "x", "--db", str(tmp_path / "nao-existe.db")])
    assert result.exit_code == 1
    assert "banco inexistente" in result.output
