from pathlib import Path

from pytest import MonkeyPatch

from php_map.config import Settings, env_value


def test_defaults() -> None:
    settings = Settings()

    assert settings.source is None
    assert settings.db_path == Path("out") / "graph.db"
    assert {"vendor", "node_modules"} <= settings.exclude_dirs
    assert settings.php_extensions == frozenset({".php"})
    assert settings.php_image is None  # escolhida por `php_extractor.resolve_image`


def test_db_path_follows_out_dir() -> None:
    settings = Settings(out_dir=Path("outro"))

    assert settings.db_path == Path("outro") / "graph.db"


def test_env_value_prefers_the_environment(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("PHP_MAP_CATALOG_DSN=do-arquivo\n", encoding="utf-8")
    monkeypatch.setenv("PHP_MAP_CATALOG_DSN", "da-variavel")

    assert env_value("PHP_MAP_CATALOG_DSN", [dotenv]) == "da-variavel"


def test_env_value_reads_the_first_dotenv_that_has_it(tmp_path: Path) -> None:
    first, second = tmp_path / "a.env", tmp_path / "b.env"
    first.write_text("# DSN comentado\n#PHP_MAP_CATALOG_DSN=nao\nOUTRA=1\n", encoding="utf-8")
    second.write_text(
        "export PHP_MAP_CATALOG_DSN = \"host=h dbname=b password='x'\"\n", encoding="utf-8"
    )

    assert env_value("PHP_MAP_CATALOG_DSN", [tmp_path / "nao-existe", first, second]) == (
        "host=h dbname=b password='x'"
    )
    assert env_value("PHP_MAP_CATALOG_DSN", [first]) is None
