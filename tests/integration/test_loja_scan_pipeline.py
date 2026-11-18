"""`scan` sobre as fixtures: gabarito no banco, resumo e cache incremental.

Sem Docker, o extrator é trocado por um falso que devolve os registros do JSONL de
referência (com o `sha1` real do arquivo) e anota o que pediu.
"""

import json
import os
import shutil
import sqlite3
import stat
import sys
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from php_map import pipeline
from php_map.cli import app
from php_map.config import Settings
from php_map.errors import ConfigError, IngestError, StoreError
from php_map.graph import store
from php_map.ingest import ast_cache, php_extractor
from php_map.ingest.ast_cache import file_sha1
from php_map.ingest.records import PhpFileRecord, parse_record
from php_map.pipeline import ScanOptions, ScanSummary, run_scan

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MINI = FIXTURES / "loja-mini"
REFERENCE = FIXTURES / "ast/loja-mini.jsonl"
DDL = FIXTURES / "schema/loja_mini.sql"
CATALOG = FIXTURES / "catalog/loja_mini.sql"
EXPECTED = FIXTURES / "expected"
SUMMARY_KEYS = {
    "source",
    "started_at",
    "duration_s",
    "files",
    "nodes",
    "edges",
    "unresolved",
    "sql",
    "schema",
    "catalog",
    "search",
    "analytics",
    "php_map_version",
    "db",
    "warnings",
    "routines",
    "routine_origins",
    "routine_rules",
    "routine_fields",
    "field_writes",
    "formula_index",
    "project",
    "zend1",
    "doctrine",
    "endpoints",
    "routes",
    "http_calls",
    "eloquent",
}


class FakeExtractor:
    """Mesmo contrato de `php_extractor.extract`: grava as linhas cruas e devolve os registros."""

    def __init__(self) -> None:
        self.reference = {
            json.loads(line)["path"]: json.loads(line)
            for line in REFERENCE.read_text(encoding="utf-8").splitlines()
        }
        self.calls: list[list[str]] = []

    def __call__(
        self,
        source: Path,
        files: Sequence[str],
        settings: Settings,
        cache_path: Path,
        progress: Any = None,
    ) -> Iterator[PhpFileRecord]:
        self.calls.append(list(files))
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8", newline="\n") as out:
            for path in files:
                data = dict(self.reference.get(path) or _empty_record(path))
                data["sha1"] = file_sha1(source / path)
                raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                out.write(raw + "\n")
                yield parse_record(raw)

    @property
    def requested(self) -> list[str]:
        return [path for call in self.calls for path in call]


def _empty_record(path: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "path": path,
        "status": "ok",
        "errors": [],
        "sha1": None,
        "lines": 1,
        "declares": [],
        "includes": [],
        "calls": [],
        "dynamic": [],
        "sql_strings": [],
        "string_refs": [],
        "dispatch": [],
    }


def _seed_cache(source: Path, out: Path, legacy: bool = False) -> None:
    """Cache = JSONL de referência + `meta.json` coerente (o que um scan anterior deixaria).

    `legacy`: no formato antigo (`<out>/ast/php.jsonl`), que o scan migra.
    """
    ast_dir = out / "ast" if legacy else ast_cache.cache_dir(out, source)
    ast_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REFERENCE, ast_dir / ast_cache.CACHE_NAME)
    ast_cache.write_meta(ast_dir, ast_cache.current_meta(source, Settings()))


def _cache(out: Path, source: Path) -> Path:
    return ast_cache.cache_dir(out, source) / ast_cache.CACHE_NAME


def _graph(db: Path) -> tuple[set[tuple[str, str]], set[tuple[str, str, str, str, float]]]:
    graph = store.load(db)
    nodes = {(n, json.dumps(a, sort_keys=True, default=str)) for n, a in graph.nodes(data=True)}
    edges = {(u, v, d["type"], d["source"], d["confidence"]) for u, v, d in graph.edges(data=True)}
    return nodes, edges


def _gabarito() -> list[dict[str, Any]]:
    edges = _expected("loja_edges.json")
    assert isinstance(edges, list)
    return edges


def _expected(name: str) -> Any:
    return json.loads((EXPECTED / name).read_text(encoding="utf-8"))


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    target = tmp_path / "ecidade"
    shutil.copytree(MINI, target)
    return target


def _scan(source: Path, out: Path, fake: FakeExtractor, **kw: Any) -> ScanSummary:
    options = ScanOptions(source=source, schema=(DDL,), catalog=(CATALOG,), out_dir=out, **kw)
    return run_scan(options, extract_fn=fake)


# --------------------------------------------------------------------------- critério 1 e 2


def test_cli_scan_with_reuse_ast_has_the_whole_gabarito(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _seed_cache(MINI, out)
    result = CliRunner().invoke(
        app,
        [
            "scan",
            "--source",
            str(MINI),
            "--schema",
            str(DDL),
            "--catalog",
            str(CATALOG),
            "--out",
            str(out),
            "--reuse-ast",
        ],
    )
    assert result.exit_code == 0, result.output

    graph = store.load(out / "graph.db")
    edges = {(u, d["type"], v): d for u, v, d in graph.edges(data=True)}
    missing = [e for e in _gabarito() if (e["src"], e["type"], e["dst"]) not in edges]
    assert missing == []

    summary = json.loads((out / "scan_summary.json").read_text(encoding="utf-8"))
    assert set(summary) == SUMMARY_KEYS
    assert summary["files"]["cache"] == {
        "reused": 25,
        "extracted": 0,
        "removed": 0,
        "reason": "incremental",
    }
    assert summary["files"]["by_status"] == {"ok": 24, "parse_error": 1}
    assert summary["schema"]["tables"] > 0 and summary["sql"]["strings"] == 14
    catalog = summary["catalog"]
    assert catalog["read"]["files"] == 1 and catalog["read"]["unsupported"] == 0
    assert catalog["graph"]["modules"] == 2 and catalog["graph"]["menu_missing_files"] == 1
    assert summary["duration_s"]["catalog"] >= 0
    action = graph.nodes["BusinessAction:menu:7001"]
    assert action["menu_path"] == "Pedidos > Procedimentos > Cancelar Pedido"
    # o índice de busca é gravado depois do `save`, no mesmo banco.
    assert summary["search"]["documents"] == 44 and summary["duration_s"]["search"] >= 0
    assert store.load_meta(out / "graph.db")["search_docs"] == "44"
    # agregados gravados depois da busca, no mesmo banco.
    assert summary["analytics"]["screens_in_menu"] >= 1 and summary["duration_s"]["analytics"] >= 0
    with closing(sqlite3.connect(out / "graph.db")) as conn:
        in_menu = conn.execute(
            "SELECT in_menu, module FROM analytics_screens WHERE file_id = ?",
            ("File:ped4_cancelarpedido001.php",),
        ).fetchone()
        owners = conn.execute(
            "SELECT owner_modules FROM analytics_tables WHERE table_id = ?",
            ("Table:public.pedido",),
        ).fetchone()
    assert in_menu == (1, "Module:pedidos")
    assert json.loads(owners[0]) == ["Module:pedidos"]

    found = set()
    for line in (out / "unresolved.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        found.add((record["kind"], record["path"], record["hint"]))
    wanted = {
        (u["kind"], u["path"], u["hint"]) for u in _expected("loja_unresolved.json")["unresolved"]
    }
    assert wanted <= found

    meta = store.load_meta(out / "graph.db")
    assert meta["source"] == str(MINI.resolve())
    assert json.loads(meta["summary"])["files"]["total"] == 25


def test_no_search_then_search_command_says_index_is_missing(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _seed_cache(MINI, out)
    runner = CliRunner()
    scan = runner.invoke(
        app, ["scan", "--source", str(MINI), "--out", str(out), "--reuse-ast", "--no-search"]
    )
    assert scan.exit_code == 0, scan.output
    summary = json.loads((out / "scan_summary.json").read_text(encoding="utf-8"))
    assert summary["search"] is None and "search" not in summary["duration_s"]
    result = runner.invoke(app, ["search", "pedido", "--db", str(out / "graph.db")])
    assert result.exit_code == 1
    assert "sem índice de busca: rode o scan de novo, sem --no-search" in result.output


def test_search_index_failure_keeps_graph_and_exits_1(
    copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any) -> None:
        raise StoreError("disco cheio")

    monkeypatch.setattr(pipeline, "write_index", broken)
    out = tmp_path / "out"
    with pytest.raises(StoreError, match="índice de busca não gravado"):
        _scan(copy, out, FakeExtractor())
    summary = json.loads((out / "scan_summary.json").read_text(encoding="utf-8"))
    assert summary["search"] == {"error": "disco cheio"}
    assert store.load(out / "graph.db").number_of_nodes() > 0  # o grafo ficou


def test_no_analytics_creates_no_tables(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _seed_cache(MINI, out)
    result = CliRunner().invoke(
        app,
        ["scan", "--source", str(MINI), "--out", str(out), "--reuse-ast", "--no-search",
         "--no-analytics", "--catalog", str(CATALOG)],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    summary = json.loads((out / "scan_summary.json").read_text(encoding="utf-8"))
    assert summary["analytics"] is None and "analytics" not in summary["duration_s"]
    with closing(sqlite3.connect(out / "graph.db")) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {n for n in names if n.startswith("analytics_")}


def test_analytics_failure_keeps_graph_and_exits_1(
    copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any) -> None:
        raise StoreError("disco cheio")

    monkeypatch.setattr(pipeline, "write_analytics", broken)
    out = tmp_path / "out"
    with pytest.raises(StoreError, match="agregados não gravados"):
        _scan(copy, out, FakeExtractor())
    summary = json.loads((out / "scan_summary.json").read_text(encoding="utf-8"))
    assert summary["analytics"] == {"error": "disco cheio"}
    assert summary["search"]["documents"] > 0  # a busca foi gravada antes
    assert store.load(out / "graph.db").number_of_nodes() > 0


def test_without_schema_warns(copy: Path, tmp_path: Path) -> None:
    summary = run_scan(
        ScanOptions(source=copy, out_dir=tmp_path / "out"), extract_fn=FakeExtractor()
    )
    assert summary.schema is None and summary.catalog is None
    assert any("--schema" in warning for warning in summary.warnings)


# --------------------------------------------------------------------------- critério 3


def test_cli_nonexistent_source_exits_1(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["scan", "--source", str(tmp_path / "nao-existe")])
    assert result.exit_code == 1
    assert "diretório de origem inexistente" in result.output


def test_source_that_is_not_ecidade(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="perfil 'ecidade'.*libs/db_stdlib.php"):
        run_scan(ScanOptions(source=tmp_path, out_dir=tmp_path / "out"), extract_fn=FakeExtractor())


# --------------------------------------------------------------------------- critério 5 e 6


def test_incremental_cache(copy: Path, tmp_path: Path) -> None:
    out, fake = tmp_path / "out", FakeExtractor()
    first = _scan(copy, out, fake, reuse_ast=True)
    assert first.files["cache"]["reason"] == "no_cache" and len(fake.requested) == 25

    fake.calls.clear()
    second = _scan(copy, out, fake, reuse_ast=True)
    assert fake.calls == []  # zero arquivos pedidos ao extrator
    assert second.files["cache"] == {
        "reused": 25,
        "extracted": 0,
        "removed": 0,
        "reason": "incremental",
    }

    changed = copy / "func_pedido.php"
    changed.write_bytes(changed.read_bytes() + b"\n<?php // alterado ?>\n")
    (copy / "libs/JSON.php").unlink()
    (copy / "novo001.php").write_text("<?php\necho 'novo';\n", encoding="latin-1")

    fake.calls.clear()
    third = _scan(copy, out, fake, reuse_ast=True)
    assert fake.requested == ["func_pedido.php", "novo001.php"]
    assert third.files["cache"] == {
        "reused": 23,
        "extracted": 2,
        "removed": 1,
        "reason": "incremental",
    }
    cached = [r.path for r in php_extractor.read_cache(_cache(out, copy))]
    assert cached == sorted(cached) and "libs/JSON.php" not in cached and "novo001.php" in cached

    full_out = tmp_path / "full"
    _scan(copy, full_out, FakeExtractor())  # scan completo sobre a mesma cópia
    assert _graph(out / "graph.db") == _graph(full_out / "graph.db")
    assert _cache(out, copy).read_bytes() == _cache(full_out, copy).read_bytes()


@pytest.mark.parametrize("field", ["extractor_hash", "schema_version"])
def test_changed_extractor_forces_full_extraction(copy: Path, tmp_path: Path, field: str) -> None:
    out, fake = tmp_path / "out", FakeExtractor()
    _seed_cache(copy, out)
    meta_path = ast_cache.cache_dir(out, copy) / ast_cache.META_NAME
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta[field] = "outro" if field == "extractor_hash" else 1
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    summary = _scan(copy, out, fake, reuse_ast=True)
    assert summary.files["cache"]["reason"] == field
    assert len(fake.requested) == 25
    assert any("reextraindo tudo" in warning for warning in summary.warnings)
    meta_after = ast_cache.read_meta(ast_cache.cache_dir(out, copy))
    assert meta_after is not None
    assert replace(meta_after, files=None, updated_at=None) == ast_cache.current_meta(
        copy, Settings()
    )
    assert meta_after.files == 25 and meta_after.updated_at  # o aviso da interface


def test_cache_copied_by_hand_from_another_source_is_refused(copy: Path, tmp_path: Path) -> None:
    """Defesa: a subpasta da cópia com o `meta.json` de outra pasta."""
    out = tmp_path / "out"
    _seed_cache(MINI, out)
    shutil.copytree(ast_cache.cache_dir(out, MINI), ast_cache.cache_dir(out, copy))
    with pytest.raises(ConfigError, match="outra origem"):
        _scan(copy, out, FakeExtractor(), reuse_ast=True)


def test_cache_without_meta_is_no_cache(copy: Path, tmp_path: Path) -> None:
    out, fake = tmp_path / "out", FakeExtractor()
    _seed_cache(copy, out)
    (ast_cache.cache_dir(out, copy) / ast_cache.META_NAME).unlink()
    summary = _scan(copy, out, fake, reuse_ast=True)
    assert summary.files["cache"]["reason"] == "no_cache" and len(fake.requested) == 25


def test_limit_keeps_the_rest_of_the_cache(copy: Path, tmp_path: Path) -> None:
    out, fake = tmp_path / "out", FakeExtractor()
    _scan(copy, out, fake, reuse_ast=True)
    fake.calls.clear()
    limited = _scan(copy, out, fake, reuse_ast=True, limit=5)
    assert fake.calls == []
    assert limited.files["total"] == 5 and limited.files["cache"]["kept"] == 20
    assert len(list(php_extractor.read_cache(_cache(out, copy)))) == 25
    fake.calls.clear()
    _scan(copy, out, fake, reuse_ast=True)
    assert fake.calls == []  # o --limit não destruiu o cache


def test_failed_extraction_keeps_the_old_cache(copy: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _scan(copy, out, FakeExtractor(), reuse_ast=True)
    before = _cache(out, copy).read_bytes()
    (copy / "novo001.php").write_text("<?php\n", encoding="latin-1")

    def broken(*_args: Any) -> Iterator[PhpFileRecord]:
        raise IngestError("Docker não encontrado no PATH")
        yield  # pragma: no cover

    with pytest.raises(IngestError):
        run_scan(
            ScanOptions(source=copy, schema=(DDL,), out_dir=out, reuse_ast=True),
            extract_fn=broken,
        )
    assert _cache(out, copy).read_bytes() == before


def test_partial_extraction_left_by_a_cancel_is_not_reused(copy: Path, tmp_path: Path) -> None:
    """Cancelar no meio da extração deixa `php.new.jsonl` parcial. O scan seguinte
    refaz o plano pelo `sha1` e nunca aproveita esse arquivo."""
    out = tmp_path / "out"
    _scan(copy, out, FakeExtractor(), reuse_ast=True)
    complete = _cache(out, copy).read_bytes()
    partial = ast_cache.cache_dir(out, copy) / ast_cache.NEW_NAME
    garbage = b'{"schema_version":2,"path":"func_pedido.php","status":"ok","sha1":"'
    partial.write_bytes(garbage + b"0" * 40 + b'"}\n{"schema_version":2,"path":"novo0')

    fake = FakeExtractor()
    _scan(copy, out, fake, reuse_ast=True)  # nada mudou: nada a extrair
    assert fake.calls == [] and not partial.exists()
    assert _cache(out, copy).read_bytes() == complete

    partial.write_bytes(garbage + b"0" * 40 + b'"}\n')
    changed = copy / "func_pedido.php"
    changed.write_bytes(changed.read_bytes() + b"\n<?php // alterado ?>\n")
    summary = _scan(copy, out, fake, reuse_ast=True)
    assert fake.requested == ["func_pedido.php"] and not partial.exists()
    assert summary.files["cache"]["extracted"] == 1
    cached = {r.path: r.sha1 for r in php_extractor.read_cache(_cache(out, copy))}
    assert cached["func_pedido.php"] == file_sha1(changed)


# --------------------------------------------------------------------------- cache por pasta


def test_two_folders_keep_their_own_caches(copy: Path, tmp_path: Path) -> None:
    """A cópia de teste (CRLF) e a instalação real (LF) no mesmo `out`: cada uma com o seu."""
    other = tmp_path / "outra" / "ecidade"
    shutil.copytree(MINI, other)
    changed = other / "func_pedido.php"
    changed.write_bytes(changed.read_bytes().replace(b"\n", b"\r\n"))  # só o fim de linha
    out, fake = tmp_path / "out", FakeExtractor()

    _scan(copy, out, fake, reuse_ast=True)
    second = _scan(other, out, fake, reuse_ast=True)  # antes: "cache de outra origem"
    assert second.files["cache"]["reason"] == "no_cache"
    fake.calls.clear()
    third = _scan(copy, out, fake, reuse_ast=True)
    assert fake.calls == [] and third.files["cache"]["reused"] == 25
    assert ast_cache.cache_dir(out, copy) != ast_cache.cache_dir(out, other)

    before = _cache(out, copy).read_bytes()
    _scan(other, out, fake)  # sem --reuse-ast: refaz só o cache da própria pasta
    assert _cache(out, copy).read_bytes() == before
    assert third.files["cache_dir"] == f"ast/{ast_cache.cache_key(copy)}"


def test_legacy_cache_is_migrated_and_reused(tmp_path: Path) -> None:
    out, fake = tmp_path / "out", FakeExtractor()
    _seed_cache(MINI, out, legacy=True)  # formato antigo do cache
    summary = _scan(MINI, out, fake, reuse_ast=True)

    assert fake.calls == []  # nada reextraído
    assert summary.files["cache"] == {
        "reused": 25,
        "extracted": 0,
        "removed": 0,
        "reason": "incremental",
    }
    assert summary.files["cache_dir"] == f"ast/{ast_cache.cache_key(MINI)}"
    assert sorted(p.name for p in (out / "ast").iterdir()) == [ast_cache.cache_key(MINI)]
    meta = ast_cache.read_meta(ast_cache.cache_dir(out, MINI))
    assert meta is not None and meta.files == 25


def test_summary_and_pending_beside_a_db_outside_out(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _seed_cache(MINI, out)
    db = out / "grafos" / "2026-09-25T12-30-50.db"
    _scan(MINI, out, FakeExtractor(), reuse_ast=True, db=db)

    assert (db.parent / "2026-09-25T12-30-50.scan_summary.json").is_file()
    assert (db.parent / "2026-09-25T12-30-50.unresolved.jsonl").is_file()
    assert not (out / "scan_summary.json").exists() and not (out / "unresolved.jsonl").exists()
    assert pipeline.pending_path(db) == db.parent / "2026-09-25T12-30-50.unresolved.jsonl"

    report = tmp_path / "trace.md"
    result = CliRunner().invoke(
        app,
        [
            "trace",
            "--entry",
            "ped4_cancelarpedido001.php",
            "--db",
            str(db),
            "--output",
            str(report),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "unresolved_include" in report.read_text(encoding="utf-8")  # achou as pendências


def test_default_db_keeps_summary_and_pending_in_out(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _seed_cache(MINI, out)
    _scan(MINI, out, FakeExtractor(), reuse_ast=True)
    assert (out / "scan_summary.json").is_file() and (out / "unresolved.jsonl").is_file()
    assert pipeline.pending_path(out / "graph.db") == out / "unresolved.jsonl"


# --------------------------------------------------------------------------- critério 4 (Docker)


@pytest.mark.docker
def test_real_extractor_without_reuse_ast(copy: Path, tmp_path: Path) -> None:
    settings = Settings()
    try:
        php_extractor.check_environment(settings)
    except IngestError as exc:
        pytest.skip(str(exc))
    out = tmp_path / "out"
    summary = run_scan(
        ScanOptions(source=copy, schema=(DDL,), catalog=(CATALOG,), out_dir=out), settings
    )
    assert summary.files["cache"]["reason"] == "disabled"
    assert summary.files["by_status"] == {"ok": 24, "parse_error": 1}
    assert _cache(out, copy).read_bytes() == REFERENCE.read_bytes()
    graph = store.load(out / "graph.db")
    edges = {(u, d["type"], v) for u, v, d in graph.edges(data=True)}
    assert all((e["src"], e["type"], e["dst"]) in edges for e in _gabarito())


# --------------------------------------------------------------------------- schema lido do banco


def load_catalog_fixture() -> Any:
    from php_map.ingest.catalog_sql import load_catalog

    return load_catalog([CATALOG])


def _db_snapshot() -> Any:
    from php_map.ingest.schema_db import DbRoutine, SchemaSnapshot
    from php_map.ingest.table_directory import DbTable

    return SchemaSnapshot(
        source="db:db.exemplo/ecidade",
        search_path='"$user", public, varejo',
        tables=[
            DbTable("varejo", "pedido", "table", ("ped01_codigo", "ped01_total")),
            DbTable("varejo", "pedidocancelado", "table", ("can01_codigo", "can01_pedido")),
            DbTable("public", "w_nunca_usada", "table", ("id",)),
        ],
        functions=[
            DbRoutine(
                "public",
                "fc_saldo_estoque",
                "CREATE OR REPLACE FUNCTION public.fc_saldo_estoque(integer, "
                "numeric)\n RETURNS text\n LANGUAGE plpgsql\nAS $function$\n"
                "begin\n  perform 1 from pedido where ped01_codigo = $1;\n  return '';\nend;\n"
                "$function$\n",
            )
        ],
    )


def test_schema_db_materializes_the_referenced_tables(
    copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from php_map.ingest.catalog_sql import load_catalog

    monkeypatch.setattr(pipeline, "read_schema_db", lambda dsn: _db_snapshot())
    monkeypatch.setattr(pipeline, "load_catalog_db", lambda dsn: load_catalog([CATALOG]))
    options = ScanOptions(
        source=copy, schema_db=True, catalog_db="host=db.exemplo dbname=ecidade", out_dir=tmp_path
    )
    summary = run_scan(options, extract_fn=FakeExtractor())

    graph = store.load(options.db_path)
    pedido = graph.nodes["Table:varejo.pedido"]
    assert pedido["resolved"] is True and pedido["columns"] == ["ped01_codigo", "ped01_total"]
    assert pedido["source_file"] == "db:varejo.pedido"
    assert "Table:public.pedido" not in graph and "Table:public.pedidocancelado" not in graph
    assert "Table:public.w_nunca_usada" not in graph  # só as referenciadas viram nó
    assert graph.nodes["Table:public.entrega"]["resolved"] is False  # não está no banco
    assert graph.has_edge("Method:cl_pedidocancelado::incluir", "Table:varejo.pedidocancelado")
    rpc = "PgFunction:public.fc_saldo_estoque(int4,numeric)"
    assert graph.nodes[rpc]["source_file"] == "db:public.fc_saldo_estoque"
    assert graph.has_edge(rpc, "Table:varejo.pedido")
    schema = summary.schema
    assert schema is not None and schema["source"] == "db:db.exemplo/ecidade"
    assert schema["search_path"] == ["public", "varejo"] and schema["materialized"] == 2
    assert summary.sql["table_refs"]["search_path"] > 0
    assert not any("--schema" in warning for warning in summary.warnings)
    assert "db.exemplo dbname" not in options.db_path.with_name("scan_summary.json").read_text(
        encoding="utf-8"
    )


def test_schema_and_schema_db_are_exclusive(copy: Path, tmp_path: Path) -> None:
    dsn = "host=db.exemplo dbname=ecidade"
    both = ScanOptions(source=copy, schema=(DDL,), schema_db=True, catalog_db=dsn, out_dir=tmp_path)
    with pytest.raises(ConfigError, match="--schema ou --schema-db"):
        run_scan(both, extract_fn=FakeExtractor())
    without_dsn = ScanOptions(source=copy, schema_db=True, out_dir=tmp_path)
    with pytest.raises(ConfigError, match="--schema-db precisa do banco"):
        run_scan(without_dsn, extract_fn=FakeExtractor())


def test_cli_schema_db_without_the_catalog(
    copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--schema-db --no-catalog-db` lê o schema do banco do `.env` sem o catálogo."""
    out = tmp_path / "out"
    _seed_cache(copy, out)
    seen: list[str] = []

    def fake_read(dsn: str) -> Any:
        seen.append(dsn)
        return _db_snapshot()

    monkeypatch.setattr(pipeline, "read_schema_db", fake_read)
    base = ["scan", "--source", str(copy), "--out", str(out), "--reuse-ast", "--schema-db"]
    base += ["--no-catalog-db", "--no-search", "--no-analytics"]

    failed = CliRunner().invoke(app, base)  # sem DSN nenhum
    assert failed.exit_code == 1 and "PHP_MAP_CATALOG_DSN no .env" in failed.output

    monkeypatch.setenv("PHP_MAP_CATALOG_DSN", "host=db.exemplo dbname=ecidade password=segredo")
    result = CliRunner().invoke(app, base)
    assert result.exit_code == 0, result.output
    assert seen == ["host=db.exemplo dbname=ecidade password=segredo"]
    summary = json.loads((out / "scan_summary.json").read_text(encoding="utf-8"))
    assert summary["schema"]["source"] == "db:db.exemplo/ecidade" and summary["catalog"] is None
    assert "segredo" not in json.dumps(summary) and "segredo" not in result.output


# ------------------------------------------------------------------------ texto das funções da base


def test_scan_stores_the_text_of_every_db_function(
    copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from php_map.graph import routines

    monkeypatch.setattr(pipeline, "read_schema_db", lambda dsn: _db_snapshot())
    monkeypatch.setattr(pipeline, "load_catalog_db", lambda dsn: load_catalog_fixture())
    options = ScanOptions(
        source=copy,
        schema_db=True,
        catalog_db="host=db.exemplo dbname=ecidade",
        out_dir=tmp_path,
        search=False,  # a tabela é gravada mesmo sem o índice de busca
    )
    summary = run_scan(options, extract_fn=FakeExtractor())

    stats = summary.routines
    assert stats is not None and stats["source"] == "db" and stats["stored"] == 1
    assert stats["without_node"] == 0 and stats["warning"] is None and stats["bytes"] > 100
    with closing(sqlite3.connect(options.db_path)) as conn:
        from_graph = {
            row[0]
            for row in conn.execute(
                "SELECT id FROM nodes WHERE label = 'PgFunction' "
                "AND json_extract(attrs, '$.source_file') LIKE 'db:%'"
            )
        }
        stored = {row[0] for row in conn.execute("SELECT node_id FROM routine_sources")}
        row = routines.read_routine(conn, "PgFunction:public.fc_saldo_estoque(int4,numeric)")
    assert stored == from_graph and len(stored) == 1
    assert row is not None and "perform 1 from pedido" in row.ddl
    assert row.language == "plpgsql" and row.read_at == stats["read_at"]
    saved = json.loads(options.db_path.with_name("scan_summary.json").read_text(encoding="utf-8"))
    assert saved["routines"]["stored"] == 1


def test_scan_stores_the_rules_of_every_db_function(
    copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`routine_rules`/`routine_profile`, gravadas junto com `routine_sources`."""
    from php_map.graph import routines

    monkeypatch.setattr(pipeline, "read_schema_db", lambda dsn: _db_snapshot())
    monkeypatch.setattr(pipeline, "load_catalog_db", lambda dsn: load_catalog_fixture())
    options = ScanOptions(
        source=copy,
        schema_db=True,
        catalog_db="host=db.exemplo dbname=ecidade",
        out_dir=tmp_path,
        search=False,  # a tabela é gravada mesmo sem o índice de busca
    )
    summary = run_scan(options, extract_fn=FakeExtractor())

    node_id = "PgFunction:public.fc_saldo_estoque(int4,numeric)"
    stats = summary.routine_rules
    assert stats is not None and stats["functions"] == 1
    with closing(sqlite3.connect(options.db_path)) as conn:
        assert routines.has_rules(conn) is True
        profile = routines.read_profile(conn, node_id)
        found = routines.read_rules(conn, node_id, kinds=())
    assert profile is not None and profile["language"] == "plpgsql"
    assert profile["returns"] == "text"
    assert profile["counts"]["performs"] == 1
    assert found is not None and found["total"] == 0  # a função da fixture não declara regra
    saved = json.loads(options.db_path.with_name("scan_summary.json").read_text(encoding="utf-8"))
    assert saved["routine_rules"]["functions"] == 1


def test_scan_stores_the_fields_of_every_db_function(
    copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`routine_queries`/`routine_fields`, a partir do mesmo texto e do diretório de
    tabelas do schema (`--schema-db`)."""
    from php_map.graph import routines

    monkeypatch.setattr(pipeline, "read_schema_db", lambda dsn: _db_snapshot())
    monkeypatch.setattr(pipeline, "load_catalog_db", lambda dsn: load_catalog_fixture())
    options = ScanOptions(
        source=copy,
        schema_db=True,
        catalog_db="host=db.exemplo dbname=ecidade",
        out_dir=tmp_path,
        search=False,
    )
    summary = run_scan(options, extract_fn=FakeExtractor())

    node_id = "PgFunction:public.fc_saldo_estoque(int4,numeric)"
    stats = summary.routine_fields
    assert stats is not None and stats["functions"] == 1 and stats["queries"] > 0
    assert stats["fields"] > 0
    with closing(sqlite3.connect(options.db_path)) as conn:
        assert routines.has_fields(conn) is True
        found = routines.read_queries(conn, node_id)
        # A função da fixture é só `perform 1 from pedido where ped01_codigo = $1`.
        pedido = "Table:varejo.pedido"  # search_path: public, varejo
        usage = routines.column_usage(conn, pedido, "ped01_codigo", role="filter")
    assert found is not None and found["total"] > 0
    assert usage is not None and usage["total"] >= 1
    assert usage["items"][0]["routine_id"] == node_id
    saved = json.loads(options.db_path.with_name("scan_summary.json").read_text(encoding="utf-8"))
    assert saved["routine_fields"]["functions"] == 1


def test_scan_without_the_db_schema_has_no_rules_either(copy: Path, tmp_path: Path) -> None:
    from php_map.graph import routines

    options = ScanOptions(source=copy, schema=(DDL,), out_dir=tmp_path)
    summary = run_scan(options, extract_fn=FakeExtractor())

    assert summary.routine_rules is not None
    assert {k: v for k, v in summary.routine_rules.items() if k != "seconds"} == {
        "functions": 0,
        "rules": 0,
        "by_kind": {},
    }  # `seconds` depende do relógio
    with closing(sqlite3.connect(options.db_path)) as conn:
        assert routines.has_rules(conn) is True  # tabelas criadas, vazias (sem função nenhuma)
        found = routines.read_rules(conn, "PgFunction:qualquer()")
        assert found is not None and found["total"] == 0


def test_scan_stores_messages_of_db_functions_in_the_search_index(
    copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A função com uma `RAISE EXCEPTION` guardada entra em `search_texts`."""
    from php_map.ingest.schema_db import DbRoutine

    def snapshot_with_a_rule() -> Any:
        base = _db_snapshot()
        base.functions.append(
            DbRoutine(
                "public",
                "fc_com_regra",
                "CREATE OR REPLACE FUNCTION public.fc_com_regra(integer)\n"
                " RETURNS void\n LANGUAGE plpgsql\nAS $function$\nBEGIN\n"
                "  IF $1 < 0 THEN\n    RAISE EXCEPTION 'valor nao pode ser negativo';\n"
                "  END IF;\nEND;\n$function$\n",
            )
        )
        return base

    monkeypatch.setattr(pipeline, "read_schema_db", lambda dsn: snapshot_with_a_rule())
    monkeypatch.setattr(pipeline, "load_catalog_db", lambda dsn: load_catalog_fixture())
    options = ScanOptions(
        source=copy, schema_db=True, catalog_db="host=db.exemplo dbname=ecidade", out_dir=tmp_path
    )
    run_scan(options, extract_fn=FakeExtractor())

    node_id = "PgFunction:public.fc_com_regra(int4)"
    with closing(sqlite3.connect(options.db_path)) as conn:
        rows = conn.execute(
            "SELECT kind, text, scope FROM search_texts WHERE file_id = ?", (node_id,)
        ).fetchall()
    assert rows == [("literal", "valor nao pode ser negativo", node_id)]


def test_scan_stores_the_origin_of_every_db_function(
    copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "read_schema_db", lambda dsn: _db_snapshot())
    monkeypatch.setattr(pipeline, "load_catalog_db", lambda dsn: load_catalog_fixture())
    options = ScanOptions(
        source=copy, schema_db=True, catalog_db="host=db.exemplo dbname=ecidade", out_dir=tmp_path
    )
    summary = run_scan(options, extract_fn=FakeExtractor())

    origins = summary.routine_origins
    assert origins is not None and origins["total"] == 1 and origins["truncated"] == 0
    statuses = ("com_origem", "so_arquivo", "gerada", "sem_chamador", "cadeia_sem_origem")
    assert sum(origins[s] for s in statuses) == origins["total"]
    with closing(sqlite3.connect(options.db_path)) as conn:
        rows = conn.execute("SELECT routine_id, status, reason FROM analytics_routines").fetchall()
        mentions = conn.execute("SELECT count(*) FROM analytics_routine_mentions").fetchone()[0]
    assert [r[0] for r in rows] == ["PgFunction:public.fc_saldo_estoque(int4,numeric)"]
    assert rows[0][1] in statuses and rows[0][2]  # todo status vem com o motivo
    assert mentions == origins["mentions"]
    saved = json.loads(options.db_path.with_name("scan_summary.json").read_text(encoding="utf-8"))
    assert saved["routine_origins"]["total"] == 1


def test_scan_without_the_db_schema_warns_that_functions_have_no_text(
    copy: Path, tmp_path: Path
) -> None:
    options = ScanOptions(source=copy, schema=(DDL,), out_dir=tmp_path)
    summary = run_scan(options, extract_fn=FakeExtractor())

    assert summary.routines is not None and summary.routines["source"] == "none"
    assert summary.routines["stored"] == 0
    assert any("sem o texto das funções" in w for w in summary.warnings)
    with closing(sqlite3.connect(options.db_path)) as conn:
        from php_map.graph import routines

        assert routines.has_routines(conn) is False
        assert routines.count_routines(conn) == 0


def test_function_without_a_node_is_reported(
    copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from php_map.ingest.schema_db import DbRoutine

    snapshot = _db_snapshot()
    snapshot.functions.append(
        DbRoutine("public", "quebrada", "CREATE FUNCTION public.quebrada( ((( ")
    )
    monkeypatch.setattr(pipeline, "read_schema_db", lambda dsn: snapshot)
    options = ScanOptions(
        source=copy, schema_db=True, schema_dsn="host=db.exemplo dbname=ecidade", out_dir=tmp_path
    )
    summary = run_scan(options, extract_fn=FakeExtractor())

    assert summary.routines is not None and summary.routines["stored"] == 1
    assert summary.routines["without_node"] == 1
    assert any("public.quebrada" in w for w in summary.warnings)


def _auto_args(copy: Path, out: Path, *extra: str) -> list[str]:
    return ["scan", "--source", str(copy), "--out", str(out), "--reuse-ast", "--no-search", *extra]


def test_cli_reads_the_db_by_default_for_ecidade(
    copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    _seed_cache(copy, out)
    seen: list[str] = []

    def fake_read(dsn: str) -> Any:
        seen.append(dsn)
        return _db_snapshot()

    probed: list[str] = []
    monkeypatch.setattr(pipeline, "read_schema_db", fake_read)
    monkeypatch.setattr(pipeline, "check_reachable", probed.append)
    monkeypatch.setenv("PHP_MAP_CATALOG_DSN", "host=db.exemplo dbname=ecidade password=segredo")
    base = [*_auto_args(copy, out), "--no-catalog-db", "--no-analytics"]

    result = CliRunner().invoke(app, base)  # sem --schema-db: modo automático
    assert result.exit_code == 0, result.output
    assert len(seen) == 1 and len(probed) == 1 and "Funções com texto" in result.output
    assert "segredo" not in result.output
    summary = json.loads((out / "scan_summary.json").read_text(encoding="utf-8"))
    assert summary["routines"]["stored"] == 1

    seen.clear()
    off = CliRunner().invoke(app, [*base, "--no-schema-db"])
    assert off.exit_code == 0, off.output
    assert seen == []  # --no-schema-db escaneia sem a base
    summary = json.loads((out / "scan_summary.json").read_text(encoding="utf-8"))
    assert summary["routines"]["source"] == "none"
    assert any("sem o texto das funções" in w for w in summary["warnings"])

    files = CliRunner().invoke(app, [*base, "--schema", str(DDL)])  # --schema de arquivos vale
    assert files.exit_code == 0, files.output
    assert seen == []


def test_cli_without_dsn_goes_on_without_the_text(copy: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _seed_cache(copy, out)
    result = CliRunner().invoke(app, [*_auto_args(copy, out), "--no-catalog-db", "--no-analytics"])

    assert result.exit_code == 0, result.output
    summary = json.loads((out / "scan_summary.json").read_text(encoding="utf-8"))
    assert summary["routines"]["source"] == "none" and summary["routines"]["warning"]


def test_cli_inaccessible_db_in_auto_mode_fails_early_with_the_hint(
    copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    _seed_cache(copy, out)
    stages: list[str] = []

    def unreachable(dsn: str) -> None:
        raise ConfigError("não foi possível conectar em db:db.exemplo/ecidade: timeout")

    def read(dsn: str) -> Any:
        raise ConfigError("não foi possível ler o schema em db:db.exemplo/ecidade: timeout")

    monkeypatch.setattr(pipeline, "check_reachable", unreachable)
    monkeypatch.setattr(pipeline, "read_schema_db", read)
    monkeypatch.setattr(pipeline, "list_php_files", lambda *a: stages.append("discovery") or [])
    monkeypatch.setenv("PHP_MAP_CATALOG_DSN", "host=db.exemplo dbname=ecidade password=segredo")
    base = [*_auto_args(copy, out), "--no-catalog-db", "--no-analytics"]

    auto = CliRunner().invoke(app, base)
    assert auto.exit_code == 1
    assert "base inacessível" in auto.output and "--no-schema-db" in auto.output
    assert "segredo" not in auto.output
    assert stages == []  # falhou antes de descobrir e conferir os arquivos

    # a leitura falhando no meio (a sondagem passou) também leva a dica
    monkeypatch.setattr(pipeline, "check_reachable", lambda dsn: None)
    mid = CliRunner().invoke(app, base)
    assert (
        mid.exit_code == 1 and "base inacessível" in mid.output and "--no-schema-db" in mid.output
    )

    explicit = CliRunner().invoke(app, [*base, "--schema-db"])  # explícito: erro como antes
    assert explicit.exit_code == 1
    assert "não foi possível ler o schema" in explicit.output
    assert "--no-schema-db" not in explicit.output


# --------------------------------------------------------------------------- perfis de projeto


def _zend1_fake_extract(
    source: Path,
    files: Sequence[str],
    settings: Settings,
    cache_path: Path,
    progress: Any = None,
) -> Iterator[PhpFileRecord]:
    """Como `FakeExtractor`, mas sem o JSONL de referência do e-cidade: registros vazios, só
    com o `sha1` real. Um arquivo marcado com `encoding_fallback` simula um achado de campo
    (arquivo de um projeto zend1 que o extrator não leu como UTF-8 válido)."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8", newline="\n") as out:
        for path in files:
            data = _empty_record(path)
            data["sha1"] = file_sha1(source / path)
            if path == "application/Bootstrap.php":
                data["encoding_fallback"] = True
            raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            out.write(raw + "\n")
            yield parse_record(raw)


@pytest.fixture
def zend1_source(tmp_path: Path) -> Path:
    """Pasta mínima com os dois marcadores do perfil `zend1` e uma tela `.phtml`."""
    source = tmp_path / "zend-app"
    (source / "application/configs").mkdir(parents=True)
    (source / "application/configs/application.ini").write_text("appnamespace = Application\n")
    (source / "application/Bootstrap.php").write_text("<?php\n")
    views = source / "application/modules/default/views/scripts/index"
    views.mkdir(parents=True)
    (views / "index.phtml").write_text("<h1>Oi</h1>\n")
    return source


def test_run_scan_accepts_the_zend1_profile(zend1_source: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    options = ScanOptions(source=zend1_source, out_dir=out, profile="zend1", analytics=False)
    summary = run_scan(options, extract_fn=_zend1_fake_extract)

    assert summary.project == {"slug": "zend-app", "name": "zend-app", "profile": "zend1"}
    # application.ini não é .php/.phtml: só os outros 2 arquivos (Bootstrap.php, index.phtml).
    assert summary.files["total"] == 2
    assert summary.files["encoding_fallback"] == 1
    assert sum(summary.nodes.values()) > 0

    meta = store.load_meta(out / "graph.db")
    assert (meta["project"], meta["project_name"], meta["profile"]) == (
        "zend-app",
        "zend-app",
        "zend1",
    )


def test_run_scan_without_profile_rejects_the_zend1_source(
    zend1_source: Path, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError, match=r"perfil 'ecidade'.*falta libs/db_stdlib\.php"):
        run_scan(ScanOptions(source=zend1_source, out_dir=tmp_path), extract_fn=_zend1_fake_extract)


def test_run_scan_with_project_overrides(zend1_source: Path, tmp_path: Path) -> None:
    options = ScanOptions(
        source=zend1_source,
        out_dir=tmp_path,
        profile="zend1",
        project="sistema-zend",
        project_name="Sistema Zend (principal)",
        analytics=False,
    )
    summary = run_scan(options, extract_fn=_zend1_fake_extract)
    assert summary.project == {
        "slug": "sistema-zend",
        "name": "Sistema Zend (principal)",
        "profile": "zend1",
    }


def test_cli_rejects_unknown_profile_with_exit_code_2(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["scan", "--source", str(tmp_path), "--out", str(tmp_path / "out"), "--profile", "laravel"],
    )
    assert result.exit_code == 2
    assert "laravel" in result.output and "ecidade" in result.output and "zend1" in result.output


def test_scan_stores_the_php_field_writes_table_and_the_summary(copy: Path, tmp_path: Path) -> None:
    """O estágio `field_writes` roda depois do `save`, grava `php_field_writes` no
    `.db` e deixa o resumo no `scan_summary.json` (sem tabela derivada vazia nem erro)."""
    from php_map.graph import field_writes as fw

    options = ScanOptions(source=copy, schema=(DDL,), catalog=(CATALOG,), out_dir=tmp_path)
    summary = run_scan(options, extract_fn=FakeExtractor())

    stats = summary.field_writes
    assert stats is not None
    assert set(stats) >= {
        "rows", "atribuicao", "formulario", "not_column", "calls_with_sets",
        "calls_with_form", "calls",
    }  # fmt: skip
    assert stats["rows"] == stats["atribuicao"] + stats["formulario"]
    assert "field_writes" in summary.duration_s
    with closing(sqlite3.connect(options.db_path)) as conn:
        assert fw.has_field_writes(conn) is True
        total = conn.execute("SELECT COUNT(*) FROM php_field_writes").fetchone()[0]
    assert total == stats["rows"]
    saved = json.loads(options.db_path.with_name("scan_summary.json").read_text(encoding="utf-8"))
    assert saved["field_writes"]["rows"] == total


@pytest.mark.docker
def test_scan_stores_the_formula_index_table_and_shows_in_progress(
    copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O estágio `formula_index` roda depois do `field_writes`, grava
    `routine_formulas`/`routine_rule_topics` no `.db` e aparece no `--progress-json`."""
    from php_map.cli import STAGE_LABELS, app
    from php_map.graph import formula_index

    monkeypatch.setattr(pipeline, "read_schema_db", lambda dsn: _db_snapshot())
    monkeypatch.setattr(pipeline, "load_catalog_db", lambda dsn: load_catalog_fixture())
    out = tmp_path / "out"
    db = out / "graph.db"
    result = CliRunner().invoke(
        app,
        [
            "scan",
            "--source",
            str(copy),
            "--schema-db",
            "--catalog-db",
            "host=db.exemplo dbname=ecidade",
            "--out",
            str(out),
            "--progress-json",
        ],
    )
    assert result.exit_code == 0, result.output
    stages = [json.loads(line)["stage"] for line in result.stdout.splitlines() if "stage" in line]
    assert "formula_index" in stages
    assert STAGE_LABELS["formula_index"] == "Indexando as fórmulas e regras por assunto"

    summary = json.loads((out / "scan_summary.json").read_text(encoding="utf-8"))
    stats = summary["formula_index"]
    assert stats is not None and stats["functions"] == 1
    with closing(sqlite3.connect(db)) as conn:
        assert formula_index.has_formula_index(conn) is True
        total = conn.execute("SELECT COUNT(*) FROM routine_formulas").fetchone()[0]
    assert total == stats["formulas"]


# --------------------------------------------------------------------------- permissões


@pytest.mark.skipif(sys.platform == "win32", reason="chmod só vale em POSIX")
def test_scan_leaves_the_graph_and_its_files_readable_only_by_the_owner(
    copy: Path, tmp_path: Path
) -> None:
    os.umask(0o022)  # processo "descuidado": a restrição precisa ser explícita, não do umask
    out = tmp_path / "saida"
    out.mkdir()
    os.chmod(out, 0o755)
    stale = out / "graph.db"
    stale.write_text("grafo antigo")  # já existia com a permissão padrão
    os.chmod(stale, 0o644)
    summary_file = out / pipeline.SUMMARY_NAME
    summary_file.write_text("{}")
    os.chmod(summary_file, 0o644)

    _scan(copy, out, FakeExtractor())

    def mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    ast_dir = ast_cache.cache_dir(out, copy)
    assert mode(out) == 0o700
    assert mode(stale) == 0o600 and mode(summary_file) == 0o600
    assert mode(out / pipeline.UNRESOLVED_NAME) == 0o600
    assert mode(ast_dir) == 0o700
    assert mode(ast_dir / ast_cache.CACHE_NAME) == mode(ast_dir / ast_cache.META_NAME) == 0o600
