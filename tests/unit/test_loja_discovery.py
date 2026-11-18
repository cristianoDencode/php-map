from pathlib import Path

import pytest

from php_map.config import Settings
from php_map.errors import ConfigError
from php_map.ingest.discovery import list_php_files

MINI = Path(__file__).resolve().parents[1] / "fixtures/loja-mini"


def test_fixture_discovery_skips_vendor() -> None:
    files = list_php_files(MINI, Settings())

    assert "vendor/fake/Ignorar.php" not in files
    assert "forms/db_frmcancelarpedido.php" in files
    assert "model/estoque/LancamentoEstoque.model.php" in files
    assert all("\\" not in f and not f.startswith("/") for f in files)
    assert files == sorted(files)
    assert len(files) == 25


def test_excluded_dirs_are_pruned_at_any_level(tmp_path: Path) -> None:
    for rel in ["a.php", "b.PHP", "c.js", "x/node_modules/d.php", "x/y/vendor/e.php", "x/f.php"]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<?php\n")

    assert list_php_files(tmp_path, Settings()) == ["a.php", "b.PHP", "x/f.php"]


def test_missing_source_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        list_php_files(tmp_path / "nao_existe", Settings())


def test_phtml_is_discovered_only_in_the_zend1_profile(tmp_path: Path) -> None:
    """`.phtml` entra como `File` comum só com as extensões do zend1."""
    for rel in ["a.php", "views/scripts/index/index.phtml"]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<?php\n")

    assert list_php_files(tmp_path, Settings()) == ["a.php"]  # perfil ecidade: só .php
    zend1 = Settings(php_extensions=frozenset({".php", ".phtml"}))
    assert list_php_files(tmp_path, zend1) == ["a.php", "views/scripts/index/index.phtml"]


def test_exclude_dirs_with_a_slash_only_prune_the_exact_path_from_the_root(tmp_path: Path) -> None:
    """`library/Zend` exclui só esse caminho a partir da raiz, sem apagar o
    `library/` de cada módulo (`application/modules/fiscal/library/`, convenção `Lib_`)."""
    for rel in [
        "library/Zend/Loader.php",
        "library/DBSeller/Helper.php",
        "application/modules/fiscal/library/Controller/AbstractController.php",
    ]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<?php\n")

    settings = Settings(exclude_dirs=frozenset({"library/Zend"}))
    files = list_php_files(tmp_path, settings)
    assert "library/Zend/Loader.php" not in files
    assert "library/DBSeller/Helper.php" in files
    assert "application/modules/fiscal/library/Controller/AbstractController.php" in files
