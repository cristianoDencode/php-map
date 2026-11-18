"""Peças do cache incremental de AST, sem Docker."""

import hashlib
import re
import shutil
from dataclasses import replace
from pathlib import Path

from php_map.config import PROJECT_ROOT, Settings
from php_map.ingest.ast_cache import (
    CACHE_NAME,
    NEW_NAME,
    AstMeta,
    CacheInfo,
    cache_dir,
    cache_info,
    cache_key,
    current_meta,
    extractor_hash,
    file_sha1,
    index_cache,
    migrate_legacy,
    plan_cache,
    read_meta,
    source_key,
    write_meta,
)


def test_index_cache_reads_path_sha1_status_and_offsets(tmp_path: Path) -> None:
    lines = [
        b'{"schema_version":2,"path":"a\\/b.php","status":"ok","errors":["\\"sha1\\":x"],'
        b'"sha1":"' + b"1" * 40 + b'","lines":1}\n',
        b'{"schema_version":2,"path":"c.php","status":"read_error","errors":[],"sha1":null}\n',
    ]
    cache = tmp_path / "php.jsonl"
    cache.write_bytes(b"".join(lines))
    entries = index_cache(cache)
    assert list(entries) == ["a/b.php", "c.php"]
    assert entries["a/b.php"].sha1 == "1" * 40 and entries["a/b.php"].status == "ok"
    assert entries["c.php"].sha1 is None and entries["c.php"].status == "read_error"
    assert entries["c.php"].offset == len(lines[0]) and entries["c.php"].length == len(lines[1])


def test_meta_round_trip_and_invalid_meta(tmp_path: Path) -> None:
    meta = AstMeta("D:\\e-cidade", 2, "abc")
    write_meta(tmp_path, meta)
    assert read_meta(tmp_path) == meta
    (tmp_path / "meta.json").write_text("{quebrado", encoding="utf-8")
    assert read_meta(tmp_path) is None
    assert read_meta(tmp_path / "inexistente") is None


def test_extractor_hash_changes_with_the_extractor(tmp_path: Path) -> None:
    copy = tmp_path / "php-extractor"
    shutil.copytree(PROJECT_ROOT / "php-extractor/src", copy / "src")
    shutil.copytree(PROJECT_ROOT / "php-extractor/bin", copy / "bin")
    settings = Settings(extractor_dir=copy)
    before = extractor_hash(settings)
    assert before == extractor_hash(Settings())  # só src/*.php e bin/extract.php contam
    with (copy / "src/Extractor.php").open("a", encoding="utf-8") as handle:
        handle.write("\n// mudou\n")
    assert extractor_hash(settings) != before


def test_file_sha1_matches_hashlib(tmp_path: Path) -> None:
    path = tmp_path / "x.php"
    path.write_bytes(b"<?php\n" * 300_000)  # maior que um bloco de leitura

    assert file_sha1(path) == hashlib.sha1(path.read_bytes()).hexdigest()


def test_source_key_ignores_case_and_separators_on_windows(tmp_path: Path) -> None:
    assert source_key(tmp_path) == source_key(Path(str(tmp_path) + "/."))


def test_plan_without_reuse_extracts_everything(tmp_path: Path) -> None:
    plan = plan_cache(tmp_path, ["a.php"], ["a.php"], tmp_path / "ast", Settings(), reuse=False)
    assert (plan.reason, plan.to_extract) == ("disabled", ["a.php"])


# --------------------------------------------------------------------------- cache por pasta (26)


def _cache_of(ast_dir: Path, source: Path, **meta: object) -> None:
    """`php.jsonl` qualquer + `meta.json` coerente com `source` (campos trocáveis por `meta`)."""
    ast_dir.mkdir(parents=True, exist_ok=True)
    (ast_dir / CACHE_NAME).write_text('{"path":"a.php"}\n', encoding="utf-8")
    write_meta(ast_dir, replace(current_meta(source, Settings()), **meta))  # type: ignore[arg-type]


def test_cache_key_names_and_separates_folders_with_the_same_name(tmp_path: Path) -> None:
    copy, real = tmp_path / "Mega" / "e-cidade", tmp_path / "www" / "e-cidade"
    key = cache_key(copy)
    assert re.fullmatch(r"e-cidade-[0-9a-f]{8}", key)
    assert key == cache_key(Path(str(copy) + "/."))  # estável
    assert cache_key(real) != key
    assert re.fullmatch(r"meu-e-cidade-v2-[0-9a-f]{8}", cache_key(tmp_path / "Meu e-Cidade (v2)"))
    assert cache_dir(tmp_path / "out", copy) == tmp_path / "out" / "ast" / key


def test_migrate_legacy_moves_the_cache_and_cleans_leftovers(tmp_path: Path) -> None:
    out, source = tmp_path / "out", tmp_path / "e-cidade"
    root = out / "ast"
    _cache_of(root, source)
    content = (root / CACHE_NAME).read_bytes()
    (root / NEW_NAME).write_text("parcial", encoding="utf-8")
    (root / (CACHE_NAME + ".tmp")).write_text("parcial", encoding="utf-8")

    target = migrate_legacy(out)

    assert target == cache_dir(out, source)
    assert (target / CACHE_NAME).read_bytes() == content
    assert read_meta(target) == current_meta(source, Settings())
    assert sorted(p.name for p in root.iterdir()) == [target.name]  # nada solto em ast/
    assert migrate_legacy(out) is None  # nada mais a migrar


def test_migrate_legacy_without_meta_or_with_the_target_taken(tmp_path: Path) -> None:
    out, source = tmp_path / "out", tmp_path / "e-cidade"
    (out / "ast").mkdir(parents=True)
    (out / "ast" / CACHE_NAME).write_text("{}\n", encoding="utf-8")
    assert migrate_legacy(out) is None  # sem meta.json: formato inválido, fica como está
    assert (out / "ast" / CACHE_NAME).is_file()

    _cache_of(out / "ast", source)
    _cache_of(cache_dir(out, source), source, files=7)
    assert migrate_legacy(out) is None  # não sobrescreve o cache novo
    assert (out / "ast" / CACHE_NAME).is_file()
    assert read_meta(cache_dir(out, source)).files == 7  # type: ignore[union-attr]


def test_cache_info_reasons(tmp_path: Path) -> None:
    out, source = tmp_path / "out", tmp_path / "e-cidade"
    settings = Settings()
    assert cache_info(out, source, settings) == CacheInfo(False, False, None, None, "no_cache")

    _cache_of(cache_dir(out, source), source, files=35181, updated_at="2026-09-25T12:41:02-03:00")
    info = cache_info(out, source, settings)
    assert (info.exists, info.reusable, info.reason) == (True, True, "reusable")
    assert (info.files, info.updated_at) == (35181, "2026-09-25T12:41:02-03:00")

    _cache_of(cache_dir(out, source), source, extractor_hash="outro")
    assert cache_info(out, source, settings).reason == "extractor_hash"
    _cache_of(cache_dir(out, source), source, schema_version=1)
    info = cache_info(out, source, settings)
    assert (info.exists, info.reusable, info.reason) == (True, False, "schema_version")


def test_cache_info_sees_the_legacy_cache_of_the_same_folder_only(tmp_path: Path) -> None:
    out, source, other = tmp_path / "out", tmp_path / "e-cidade", tmp_path / "outra"
    _cache_of(out / "ast", source)  # formato antigo, sem `files` nem `updated_at`
    info = cache_info(out, source, Settings())
    assert (info.exists, info.reusable, info.files, info.updated_at) == (True, True, None, None)
    assert cache_info(out, other, Settings()).exists is False


def test_meta_without_the_new_fields_is_still_read(tmp_path: Path) -> None:
    (tmp_path / "meta.json").write_text(
        '{"source": "D:/e", "schema_version": 3, "extractor_hash": "x", "files": true}',
        encoding="utf-8",
    )
    assert read_meta(tmp_path) == AstMeta("D:/e", 3, "x")  # `files` booleano é descartado
