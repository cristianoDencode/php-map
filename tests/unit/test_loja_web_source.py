"""Pasta do e-cidade e leitura de código-fonte: `web/source.py`."""

import json
import os
from pathlib import Path

import pytest

from php_map.errors import QueryError
from php_map.web.source import Workspace, read_source, resolve_workspace

MINI = Path(__file__).resolve().parents[1] / "fixtures/loja-mini"


def _workspace(root: Path) -> Workspace:
    return resolve_workspace(root, None, None)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "ecidade"
    (folder / "libs").mkdir(parents=True)
    (folder / "libs/db_stdlib.php").write_bytes(b"<?php\n")
    (folder / "tela.php").write_bytes(b"<?php\r\n// linha 2\r\necho 'Cancela\xe7\xe3o';\r\n")
    (folder / "binario.png").write_bytes(b"\x89PNG\x00\x00")
    (tmp_path / "segredo.txt").write_text("fora da pasta", encoding="utf-8")
    return folder


# --------------------------------------------------------------------------- pasta em uso


def test_workspace_order(root: Path, tmp_path: Path) -> None:
    settings = tmp_path / "ui-settings.json"
    other = tmp_path / "outra"
    other.mkdir()
    settings.write_text(json.dumps({"source": str(other)}), encoding="utf-8")

    by_arg = resolve_workspace(root, settings, str(tmp_path / "do-grafo"))
    assert (by_arg.origin, by_arg.source, by_arg.is_ecidade) == (
        "argumento",
        str(root.resolve()),
        True,
    )
    by_settings = resolve_workspace(None, settings, str(root))
    assert (by_settings.origin, by_settings.source) == ("configuração", str(other.resolve()))
    assert by_settings.exists and not by_settings.is_ecidade  # sem libs/db_stdlib.php
    by_graph = resolve_workspace(None, tmp_path / "nao-existe.json", str(root))
    assert (by_graph.origin, by_graph.graph_source) == ("grafo", str(root))
    none = resolve_workspace(None, None, None)
    assert (none.origin, none.source, none.exists) == ("nenhuma", None, False)


def test_missing_folder_and_invalid_settings(tmp_path: Path) -> None:
    settings = tmp_path / "ui-settings.json"
    settings.write_text("{ quebrado", encoding="utf-8")
    ws = resolve_workspace(None, settings, str(tmp_path / "sumiu"))
    assert (ws.origin, ws.exists, ws.is_ecidade) == ("grafo", False, False)
    with pytest.raises(QueryError, match="nenhuma pasta do e-cidade configurada"):
        read_source(ws, "tela.php")


# --------------------------------------------------------------------------- leitura


def test_reads_latin1_and_normalizes_line_endings(root: Path) -> None:
    found = read_source(_workspace(root), "tela.php", line=3)

    assert found.lines == ["<?php", "// linha 2", "echo 'Cancelação';"]
    assert (found.total_lines, found.start_line, found.highlight, found.truncated) == (
        3,
        1,
        3,
        False,
    )
    assert found.path == "tela.php" and found.size_bytes > 0


def test_reads_a_real_fixture() -> None:
    found = read_source(_workspace(MINI), "forms/db_frmcancelarpedido.php")
    assert any("Cancelação" in line for line in found.lines)
    assert read_source(_workspace(MINI), "forms\\db_frmcancelarpedido.php").path == (
        "forms/db_frmcancelarpedido.php"
    )


def test_big_file_returns_a_window(root: Path) -> None:
    (root / "grande.sql").write_text("\n".join(f"linha {i}" for i in range(1, 2001)), "latin-1")

    window = read_source(_workspace(root), "grande.sql", line=1000, context=10, max_full_bytes=100)
    assert (window.start_line, len(window.lines), window.truncated) == (990, 21, True)
    assert window.lines[10] == "linha 1000" and window.highlight == 1000
    head = read_source(_workspace(root), "grande.sql", max_full_bytes=100)
    assert (head.start_line, len(head.lines), head.highlight) == (1, 400, None)
    capped = read_source(_workspace(root), "grande.sql", line=5, context=10_000, max_full_bytes=100)
    assert (capped.start_line, len(capped.lines)) == (1, 205)  # contexto máximo 200


@pytest.mark.parametrize(
    "path",
    [
        "../segredo.txt",
        "libs/../../segredo.txt",
        "..\\segredo.txt",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "C:\\Windows\\win.ini",
        "\\\\servidor\\share\\x.php",
        "",
    ],
)
def test_paths_outside_are_refused_without_leaking(root: Path, path: str) -> None:
    with pytest.raises(QueryError) as error:
        read_source(_workspace(root), path)
    assert str(error.value) == "caminho fora da pasta do e-cidade"
    assert str(root) not in str(error.value)


def test_symlink_to_outside_is_refused(root: Path, tmp_path: Path) -> None:
    link = root / "atalho.txt"
    try:
        os.symlink(tmp_path / "segredo.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("o sistema não permite criar link simbólico")
    with pytest.raises(QueryError, match="caminho fora da pasta do e-cidade"):
        read_source(_workspace(root), "atalho.txt")


def test_binary_directory_and_missing(root: Path) -> None:
    ws = _workspace(root)
    with pytest.raises(QueryError, match="arquivo binário: binario.png"):
        read_source(ws, "binario.png")
    for path in ("libs", "nao-existe.php"):
        with pytest.raises(QueryError) as error:
            read_source(ws, path)
        assert str(error.value) == f"arquivo não encontrado na pasta do e-cidade: {path}"
