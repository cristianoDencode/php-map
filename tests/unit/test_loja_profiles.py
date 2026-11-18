"""Perfil de projeto: `get_profile`, `suggest_profile` e os dois perfis."""

from pathlib import Path

import pytest

from php_map.profiles.base import PROFILES, get_profile, suggest_profile
from php_map.profiles.ecidade import ECIDADE_MARKER

MINI = Path(__file__).resolve().parents[1] / "fixtures/loja-mini"


def test_profiles_registry_has_ecidade_and_zend1() -> None:
    assert set(PROFILES) == {"ecidade", "zend1"}


def test_get_profile_returns_the_matching_spec() -> None:
    assert get_profile("ecidade").name == "ecidade"
    assert get_profile("zend1").name == "zend1"


def test_get_profile_unknown_name_lists_the_valid_ones() -> None:
    with pytest.raises(ValueError, match="ecidade.*zend1|zend1.*ecidade"):
        get_profile("laravel")


def test_ecidade_profile_matches_todays_values() -> None:
    profile = get_profile("ecidade")
    assert profile.markers == (ECIDADE_MARKER,)
    assert profile.required_marker == "libs/db_stdlib.php"
    assert profile.source_extensions == frozenset({".php"})
    assert profile.encoding == "latin-1"
    assert profile.module_source == "catalog"
    assert profile.reads_doctrine is False  # o e-cidade não usa Doctrine
    assert len(profile.class_resolvers) == 2  # DAO + sufixo do autoloader
    assert profile.third_party_dirs == ()


def test_zend1_profile_is_the_zend1_profile() -> None:
    profile = get_profile("zend1")
    assert profile.required_marker == "application/configs/application.ini"
    assert "application/Bootstrap.php" in profile.markers  # confirmação, não exigido
    assert profile.source_extensions == frozenset({".php", ".phtml"})
    # só os `library/<terceiro>` são excluídos, pela raiz do projeto — o `library/`
    # de cada módulo (convenção `Lib_`, código próprio) não é mais apagado (era um bug da 34).
    assert "library" not in profile.exclude_dirs
    assert "library/Zend" in profile.exclude_dirs
    assert "library/Zend" in profile.third_party_dirs
    assert profile.encoding == "utf-8"
    assert profile.module_source == "directory"
    assert profile.reads_doctrine is True  # `@Entity` e DQL
    assert profile.class_resolvers == ()  # estático; os de verdade vêm de `class_resolvers_factory`
    assert profile.class_resolvers_factory is not None  # lê o application.ini
    assert profile.default_search_path == ("public",)


def test_suggest_profile_by_marker(tmp_path: Path) -> None:
    assert suggest_profile(MINI) == "ecidade"

    zend1_dir = tmp_path / "zend-app"
    (zend1_dir / "application/configs").mkdir(parents=True)
    (zend1_dir / "application/configs/application.ini").write_text("appnamespace = Application\n")
    assert suggest_profile(zend1_dir) == "zend1"


def test_suggest_profile_without_any_marker_is_none(tmp_path: Path) -> None:
    assert suggest_profile(tmp_path) is None
