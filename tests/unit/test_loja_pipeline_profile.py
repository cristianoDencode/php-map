"""Perfil de projeto no pipeline: `validate_source` e os slugs."""

from pathlib import Path

import pytest

from php_map.errors import ConfigError
from php_map.pipeline import project_defaults, project_slug, validate_source

MINI = Path(__file__).resolve().parents[1] / "fixtures/loja-mini"


def test_validate_source_accepts_the_ecidade_fixture() -> None:
    assert validate_source(MINI) == MINI
    assert validate_source(MINI, "ecidade") == MINI


def test_validate_source_default_profile_rejects_folder_without_marker(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"perfil 'ecidade'.*falta libs/db_stdlib\.php"):
        validate_source(tmp_path)


def test_validate_source_zend1_accepts_only_its_own_marker(tmp_path: Path) -> None:
    (tmp_path / "application/configs").mkdir(parents=True)
    (tmp_path / "application/configs/application.ini").write_text("appnamespace = Application\n")

    assert validate_source(tmp_path, "zend1") == tmp_path
    with pytest.raises(ConfigError, match="perfil 'zend1'"):
        validate_source(MINI, "zend1")  # a fixture do e-cidade não tem o marcador do zend1


def test_validate_source_unknown_profile_lists_the_valid_ones(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="ecidade.*zend1|zend1.*ecidade"):
        validate_source(tmp_path, "laravel")


def test_validate_source_missing_dir_is_config_error_before_profile_lookup(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="inexistente"):
        validate_source(tmp_path / "nao-existe", "laravel")


def test_project_slug_normalizes_the_folder_name() -> None:
    assert project_slug(Path("/caminho/do/e-cidade")) == "e-cidade"
    assert project_slug(Path("/caminho/do/Zend App")) == "zend-app"
    assert project_slug(Path("/tmp/Projeto Com Espaço!")) == "projeto-com-espa-o"


def test_project_defaults_without_options_use_the_folder_slug() -> None:
    slug, name = project_defaults(Path("/srv/Zend App"), None, None)
    assert (slug, name) == ("zend-app", "zend-app")


def test_project_defaults_honor_explicit_values() -> None:
    slug, name = project_defaults(Path("/srv/Zend App"), "sistema-zend", "Sistema Zend (principal)")
    assert (slug, name) == ("sistema-zend", "Sistema Zend (principal)")
