"""Permissões 600/700 dos arquivos que o programa grava."""

import logging
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from php_map import fsperm
from php_map.cli import app

posix = pytest.mark.skipif(not fsperm.is_posix(), reason="chmod só vale em POSIX")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@posix
def test_secure_file_and_dir_restrict_to_the_owner(tmp_path: Path) -> None:
    folder = tmp_path / "grafos"
    folder.mkdir(mode=0o755)
    os.chmod(folder, 0o755)
    target = folder / "grafo.db"
    target.write_text("x")
    os.chmod(target, 0o644)
    fsperm.secure_file(target)
    fsperm.secure_dir(folder)
    assert _mode(target) == 0o600 and _mode(folder) == 0o700


@posix
def test_missing_paths_are_ignored(tmp_path: Path) -> None:
    fsperm.secure_file(tmp_path / "nao-existe")
    fsperm.secure_dir(tmp_path / "nao-existe")
    fsperm.secure_tree(tmp_path / "nao-existe")


@posix
def test_secure_tree_covers_children_but_not_symlink_targets(tmp_path: Path) -> None:
    root = tmp_path / "ast" / "origem"
    (root / "sub").mkdir(parents=True)
    keep = root / "php.jsonl"
    deep = root / "sub" / "meta.json"
    outside = tmp_path / "fora.txt"
    for path in (keep, deep, outside):
        path.write_text("x")
        os.chmod(path, 0o644)
    (root / "atalho").symlink_to(outside)
    fsperm.secure_tree(root)
    assert _mode(keep) == _mode(deep) == 0o600
    assert _mode(root) == _mode(root / "sub") == 0o700
    assert _mode(outside) == 0o644  # o alvo do link não é da pasta


@posix
def test_secure_dir_never_locks_the_working_directory_or_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "trabalho"
    work.mkdir()
    os.chmod(work, 0o755)
    monkeypatch.chdir(work)
    fsperm.secure_dir(work)
    assert _mode(work) == 0o755  # `--db grafo.db` não tranca a pasta onde o usuário está
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: work))
    monkeypatch.chdir(tmp_path)
    fsperm.secure_dir(work)
    assert _mode(work) == 0o755
    fsperm.secure_dir(Path("/"))  # a raiz nunca é tocada (o teste não falha sem permissão)


@posix
def test_apply_umask_makes_new_files_private(tmp_path: Path) -> None:
    fsperm.apply_umask()
    made = tmp_path / "novo.txt"
    made.write_text("x")
    folder = tmp_path / "pasta"
    folder.mkdir()
    assert _mode(made) == 0o600 and _mode(folder) == 0o700


@posix
def test_cli_sets_the_umask_once_on_entry() -> None:
    result = CliRunner().invoke(app, ["ui", "--print-schema"])
    assert result.exit_code == 0
    current = os.umask(0o022)
    os.umask(current)
    assert current == fsperm.PRIVATE_UMASK


def test_windows_warns_once_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(fsperm, "is_posix", lambda: False)
    monkeypatch.setattr(fsperm, "_warned_windows", False)
    target = tmp_path / "grafo.db"
    target.write_text("x")
    before = _mode(target)
    with caplog.at_level(logging.WARNING):
        fsperm.secure_file(target)
        fsperm.secure_file(target)
        fsperm.apply_umask()
    assert caplog.text.count("ignora chmod") == 1
    assert _mode(target) == before
