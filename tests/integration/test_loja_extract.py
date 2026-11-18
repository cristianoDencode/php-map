"""`extract()` de verdade (Docker) sobre o legado fictício `loja-mini`."""

import subprocess
from pathlib import Path

import pytest

from php_map.config import Settings
from php_map.errors import IngestError
from php_map.ingest import php_extractor
from php_map.ingest.discovery import list_php_files

pytestmark = pytest.mark.docker

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SOURCE = FIXTURES / "loja-mini"
REFERENCE = FIXTURES / "ast/loja-mini.jsonl"


@pytest.fixture
def settings() -> Settings:
    # Lote pequeno de propósito: exercita vários containers seguidos.
    configured = Settings(ast_batch_size=10)
    try:
        php_extractor.check_environment(configured)
    except IngestError as exc:
        pytest.skip(str(exc))
    return configured


def test_extract_matches_reference(settings: Settings, tmp_path: Path) -> None:
    files = list_php_files(SOURCE, settings)
    cache = tmp_path / "ast/php.jsonl"

    records = list(php_extractor.extract(SOURCE, files, settings, cache))

    assert [r.path for r in records] == files
    assert "vendor/fake/Ignorar.php" not in files
    assert cache.read_bytes() == REFERENCE.read_bytes()
    assert [r.path for r in php_extractor.read_cache(cache)] == files


def test_invalid_php_is_reported_not_raised(settings: Settings, tmp_path: Path) -> None:
    files = list_php_files(SOURCE, settings)
    records = {
        r.path: r for r in php_extractor.extract(SOURCE, files, settings, tmp_path / "php.jsonl")
    }
    assert records["lixo_parse_error.php"].status in {"parse_error", "partial"}
    assert records["ped4_cancelarpedido001.php"].status == "ok"


def test_latin1_file_is_decoded_with_fallback(settings: Settings, tmp_path: Path) -> None:
    files = list_php_files(SOURCE, settings)
    records = {
        r.path: r for r in php_extractor.extract(SOURCE, files, settings, tmp_path / "php.jsonl")
    }
    assert records["forms/db_frmcancelarpedido.php"].encoding_fallback is True
    assert records["ped4_baixaestoque004.php"].encoding_fallback is False


def test_stopping_early_does_not_hang(settings: Settings, tmp_path: Path) -> None:
    files = list_php_files(SOURCE, settings)
    iterator = php_extractor.extract(SOURCE, files, settings, tmp_path / "php.jsonl")
    first = next(iterator)
    iterator.close()  # mata o container do lote em andamento
    assert first.path == files[0]


def test_every_fixture_is_valid_php_74_except_the_broken_one(settings: Settings) -> None:
    """`php -l` no PHP 7.4 da imagem do extrator: só `lixo_parse_error.php` é inválido."""
    image = php_extractor.resolve_image(settings)
    script = (
        "cd /src && for f in $(find . -name '*.php' | sort); do "
        'php -l "$f" >/dev/null 2>&1 || echo "$f"; done'
    )
    done = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{SOURCE.resolve()}:/src:ro", image, "sh", "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.split() == ["./lixo_parse_error.php"]
