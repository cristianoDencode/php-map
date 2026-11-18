import os
import subprocess
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

from php_map import config
from php_map.config import Settings
from php_map.errors import IngestError
from php_map.ingest import php_extractor


def test_windows_paths_become_docker_paths() -> None:
    windows = PureWindowsPath(r"D:\ecidade-windows\www\html\e-cidade")
    assert php_extractor.docker_path(windows) == "D:/ecidade-windows/www/html/e-cidade"


IMAGE = "imagem-de-teste"


def test_extract_command_mounts_read_only_without_shell() -> None:
    settings = Settings(
        extractor_dir=PureWindowsPath(r"D:\Mega\Mapeador\php-extractor"),  # type: ignore[arg-type]
        php_image=IMAGE,
    )
    command = php_extractor.build_docker_command(PureWindowsPath(r"D:\ecidade\e-cidade"), settings)

    assert command[:4] == ["docker", "run", "--rm", "-i"]
    assert "D:/Mega/Mapeador/php-extractor:/extractor:ro" in command
    assert "D:/ecidade/e-cidade:/src:ro" in command
    assert "short_open_tag=1" in command
    assert command[command.index(IMAGE) + 1] == "php"
    assert command[-2:] == ["--root=/src", "--files-from=-"]
    assert all(isinstance(part, str) for part in command)


def test_extract_command_names_and_labels_the_container() -> None:
    name = php_extractor.container_name("job42", 3)
    settings = Settings(php_image=IMAGE)
    command = php_extractor.build_docker_command(PureWindowsPath(r"D:\e"), settings, name)

    assert name == "ecmap-scan-job42-3"
    assert command[command.index("--name") + 1] == "ecmap-scan-job42-3"
    assert command[command.index("--label") + 1] == "php-map=scan"
    assert command.index("--name") < command.index(IMAGE)  # opção do `run`
    unnamed = php_extractor.build_docker_command(PureWindowsPath(r"D:\e"), settings)
    assert "--name" not in unnamed and "php-map=scan" in unnamed


def test_job_id_comes_from_the_environment_or_the_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.JOB_ID_ENV, "a1b2; rm -rf /")
    assert config.job_id() == "a1b2rm-rf"  # só o que vale num nome de container
    monkeypatch.delenv(config.JOB_ID_ENV)
    assert config.job_id() == str(os.getpid())


def test_each_batch_runs_in_its_own_named_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    names: list[str | None] = []

    def fake_batch(
        _source: Path, batch: list[str], _settings: Settings, name: str | None = None
    ) -> Iterator[tuple[Any, str]]:
        names.append(name)
        return iter(())

    monkeypatch.setenv(config.JOB_ID_ENV, "j9")
    monkeypatch.setattr(php_extractor, "check_environment", lambda _settings: IMAGE)
    monkeypatch.setattr(php_extractor, "_run_batch", fake_batch)
    files = [f"f{i}.php" for i in range(5)]
    list(php_extractor.extract(tmp_path, files, Settings(ast_batch_size=2), tmp_path / "c.jsonl"))

    assert names == ["ecmap-scan-j9-1", "ecmap-scan-j9-2", "ecmap-scan-j9-3"]


def test_setup_command_mounts_extractor_writable() -> None:
    command = php_extractor.build_setup_command(Settings(php_image=IMAGE))
    assert any(part.endswith("php-extractor:/extractor") for part in command)
    assert command[-4:] == [IMAGE, "composer", "install", "--no-interaction"]


def test_setup_runs_as_the_host_user_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux: sem `--user`, o `vendor/` ficaria com dono root no host."""
    monkeypatch.setattr(php_extractor.sys, "platform", "win32")
    windows = php_extractor.build_setup_command(Settings(php_image=IMAGE))
    assert "--user" not in windows
    monkeypatch.setattr(php_extractor.sys, "platform", "linux")
    monkeypatch.setattr(php_extractor.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(php_extractor.os, "getgid", lambda: 1001, raising=False)
    linux = php_extractor.build_setup_command(Settings(php_image=IMAGE))
    assert linux[linux.index("--user") + 1] == "1000:1001"
    assert "COMPOSER_HOME=/tmp/composer" in linux
    assert linux.index("--user") < linux.index(IMAGE)  # opção do `run`


def test_build_image_command_uses_the_extractor_dockerfile() -> None:
    settings = Settings(extractor_dir=PureWindowsPath(r"D:\m\php-extractor"))  # type: ignore[arg-type]
    command = php_extractor.build_image_command(settings)
    assert command == ["docker", "build", "-t", "php-map-php74", "D:/m/php-extractor"]


# ------------------------------------------------------------------- escolha da imagem


def _images(monkeypatch: pytest.MonkeyPatch, *existing: str) -> list[str]:
    """`docker image inspect` simulado: só `existing` existem. Devolve as consultadas."""
    asked: list[str] = []

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert args[:3] == ["docker", "image", "inspect"]
        asked.append(args[3])
        return subprocess.CompletedProcess(args, 0 if args[3] in existing else 1)

    monkeypatch.setattr(php_extractor.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(php_extractor.subprocess, "run", run)
    monkeypatch.delenv(config.PHP_IMAGE_ENV, raising=False)
    monkeypatch.setattr(config, "DOTENV_PATHS", ())  # o `.env` do projeto não interfere
    return asked


def test_image_option_wins_over_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _images(monkeypatch, "da-opcao", "da-variavel", config.PHP_IMAGE_WINDOWS)
    monkeypatch.setenv(config.PHP_IMAGE_ENV, "da-variavel")

    assert php_extractor.resolve_image(Settings(php_image="da-opcao")) == "da-opcao"
    assert asked == ["da-opcao"]


def test_image_variable_wins_over_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _images(monkeypatch, "da-variavel", config.PHP_IMAGE_WINDOWS, config.PHP_IMAGE_OWN)
    monkeypatch.setenv(config.PHP_IMAGE_ENV, "da-variavel")

    assert php_extractor.resolve_image(Settings()) == "da-variavel"


def test_image_variable_is_read_from_the_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(monkeypatch, "do-dotenv")
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{config.PHP_IMAGE_ENV}=do-dotenv\n", encoding="utf-8")
    monkeypatch.setattr(config, "DOTENV_PATHS", (dotenv,))

    assert php_extractor.resolve_image(Settings()) == "do-dotenv"


def test_default_image_is_the_windows_one_when_it_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _images(monkeypatch, config.PHP_IMAGE_WINDOWS, config.PHP_IMAGE_OWN)

    assert php_extractor.resolve_image(Settings()) == config.PHP_IMAGE_WINDOWS
    assert asked == [config.PHP_IMAGE_WINDOWS]  # quem já usa no Windows não muda nada


def test_default_image_falls_back_to_the_own_one(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _images(monkeypatch, config.PHP_IMAGE_OWN)

    assert php_extractor.resolve_image(Settings()) == config.PHP_IMAGE_OWN
    assert asked == [config.PHP_IMAGE_WINDOWS, config.PHP_IMAGE_OWN]


def test_no_image_explains_how_to_build(monkeypatch: pytest.MonkeyPatch) -> None:
    _images(monkeypatch)

    with pytest.raises(IngestError, match="setup-extractor --build-image"):
        php_extractor.resolve_image(Settings())


def test_chosen_image_that_does_not_exist_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _images(monkeypatch, config.PHP_IMAGE_WINDOWS)

    with pytest.raises(IngestError, match="imagem Docker 'nao-existe' não encontrada"):
        php_extractor.resolve_image(Settings(php_image="nao-existe"))


def test_check_environment_returns_the_image_and_extract_uses_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(monkeypatch, config.PHP_IMAGE_OWN)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/extract.php").write_text("<?php\n")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor/autoload.php").write_text("<?php\n")
    settings = Settings(extractor_dir=tmp_path, ast_batch_size=10)
    assert php_extractor.check_environment(settings) == config.PHP_IMAGE_OWN
    used: list[str | None] = []

    def fake_batch(
        _source: Path, _batch: list[str], batch_settings: Settings, _name: str | None = None
    ) -> Iterator[tuple[Any, str]]:
        used.append(batch_settings.php_image)
        return iter(())

    monkeypatch.setattr(php_extractor, "_run_batch", fake_batch)
    list(php_extractor.extract(tmp_path, ["a.php"], settings, tmp_path / "c.jsonl"))
    assert used == [config.PHP_IMAGE_OWN]


def test_missing_docker_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(php_extractor.shutil, "which", lambda _name: None)
    with pytest.raises(IngestError, match="Docker não encontrado"):
        php_extractor.check_environment(Settings())


def _fake_run(returncode: int) -> object:
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=[], returncode=returncode)

    return run


def test_missing_image_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(php_extractor.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(php_extractor.subprocess, "run", _fake_run(1))
    monkeypatch.delenv(config.PHP_IMAGE_ENV, raising=False)
    monkeypatch.setattr(config, "DOTENV_PATHS", ())
    with pytest.raises(IngestError, match="nenhuma imagem do extrator PHP"):
        php_extractor.check_environment(Settings())


def test_missing_vendor_suggests_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/extract.php").write_text("<?php\n")
    monkeypatch.setattr(php_extractor.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(php_extractor.subprocess, "run", _fake_run(0))
    with pytest.raises(IngestError, match="setup-extractor"):
        php_extractor.check_environment(Settings(extractor_dir=tmp_path))
