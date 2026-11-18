"""`scan`, busca, `trace` e `impact` sobre o legado fictício (`loja-mini`), sem Docker.

O cache de AST de referência (`ast/loja-mini.jsonl`) entra no lugar do extrator."""

import json
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from php_map.analysis.impact import ImpactOptions, impact
from php_map.analysis.trace import TraceOptions, trace
from php_map.cli import app
from php_map.config import Settings
from php_map.graph import store
from php_map.ingest import ast_cache
from php_map.pipeline import ScanOptions, ScanSummary, run_scan
from php_map.report import render
from php_map.search.query import search

if TYPE_CHECKING:
    from php_map.graph.builder import KnowledgeGraph

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SOURCE = FIXTURES / "loja-mini"
REFERENCE = FIXTURES / "ast/loja-mini.jsonl"
DDL = FIXTURES / "schema/loja_mini.sql"
CATALOG = FIXTURES / "catalog/loja_mini.sql"
EXPECTED = FIXTURES / "expected"
ENTRY = "ped4_cancelarpedido001.php"
FIXED_META = {"scanned_at": "2026-01-01T00:00:00-03:00", "source": "tests/fixtures/loja-mini"}
MERMAID_LINE = re.compile(
    r'^    (n\d+(\[\("|\[\["|\("|\["|)[^"\n]*("\)\]|"\]\]|"\)|"\]|)'
    r"|n\d+ (-->|-\.->)\|[A-Z_]+( \d\.\d\d)?\| n\d+)$"
)


@pytest.fixture(scope="module")
def scan(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, ScanSummary]:
    """O `scan` completo com o cache de AST de referência (sem extrair nada)."""
    out = tmp_path_factory.mktemp("scan-loja") / "out"
    ast_dir = ast_cache.cache_dir(out, SOURCE)
    ast_dir.mkdir(parents=True)
    shutil.copyfile(REFERENCE, ast_dir / ast_cache.CACHE_NAME)
    ast_cache.write_meta(ast_dir, ast_cache.current_meta(SOURCE, Settings()))
    summary = run_scan(
        ScanOptions(source=SOURCE, schema=(DDL,), catalog=(CATALOG,), out_dir=out, reuse_ast=True)
    )
    return out / "graph.db", summary


@pytest.fixture(scope="module")
def db(scan: tuple[Path, ScanSummary]) -> Path:
    return scan[0]


@pytest.fixture(scope="module")
def graph(db: Path) -> "KnowledgeGraph":
    return store.load(db)


def _expected(name: str) -> Any:
    return json.loads((EXPECTED / name).read_text(encoding="utf-8"))


def assert_valid_mermaid(markdown: str) -> str:
    blocks = re.findall(r"```mermaid\n(.*?)\n```", markdown, re.DOTALL)
    assert len(blocks) == 1
    lines = blocks[0].split("\n")
    assert lines[0] == "flowchart LR"
    assert [line for line in lines[1:] if not MERMAID_LINE.match(line)] == []
    return blocks[0]


# --------------------------------------------------------------------------- scan


def test_scan_summary_counts_and_cache(scan: tuple[Path, ScanSummary]) -> None:
    _db, summary = scan
    assert summary.files["total"] == 25
    assert summary.files["by_status"] == {"ok": 24, "parse_error": 1}
    assert summary.files["cache"]["reused"] == 25 and summary.files["cache"]["extracted"] == 0
    assert summary.project == {"slug": "loja-mini", "name": "loja-mini", "profile": "ecidade"}


def test_scan_graph_has_the_expected_nodes(graph: "KnowledgeGraph") -> None:
    for node in (
        "File:ped4_cancelarpedido001.php",
        "Table:public.pedido",
        "PgFunction:public.fc_saldo_estoque(int4,numeric)",
        "Module:pedidos",
        "BusinessAction:menu:7001",
    ):
        assert node in graph


# --------------------------------------------------------------------------- busca


def _search_cases() -> list[dict[str, Any]]:
    data = _expected("loja_search.json")
    assert isinstance(data, list)
    return data


@pytest.mark.parametrize("case", _search_cases(), ids=lambda c: f"{c['query']}->{c['expect']}")
def test_search_gabarito(db: Path, case: dict[str, Any]) -> None:
    hits = search(db, case["query"], limit=case["top_k"])
    assert case["expect"] in [hit.node_id for hit in hits]


def test_search_next_commands_and_modules(db: Path) -> None:
    hits = {h.node_id: h for h in search(db, "cancelamento de pedido", limit=10)}
    assert hits["BusinessAction:menu:7001"].next_command == f"php-map trace --entry {ENTRY}"
    table = hits["Table:public.pedidocancelado"]
    assert table.next_command == "php-map impact --table pedidocancelado --schema public"
    assert table.modules == ["Pedidos"]


def test_cli_search_text_and_json(db: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app, ["search", "cancelamento de pedido", "--db", str(db)], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    assert f"Próximo passo: php-map trace --entry {ENTRY}" in result.output
    result = runner.invoke(
        app, ["search", "cancelamento de pedido", "--db", str(db), "--json", "--limit", "3"]
    )
    data = json.loads(result.stdout)
    assert len(data) == 3 and {"node_id", "next_command", "snippet", "field"} <= set(data[0])
    result = runner.invoke(app, ["search", "xyzzy inexistente", "--db", str(db)])
    assert result.exit_code == 0 and "Nenhum resultado" in result.output


# --------------------------------------------------------------------------- trace


def test_trace_reaches_the_gabarito(graph: "KnowledgeGraph") -> None:
    gabarito = _expected("loja_trace.json")
    reached = set(trace(graph, TraceOptions(gabarito["entry"])).walk.steps)
    assert [m["node"] for m in gabarito["must_reach"] if m["node"] not in reached] == []
    assert [m["node"] for m in gabarito["must_not_reach"] if m["node"] in reached] == []


def test_infra_hidden_by_default_and_shown_with_flag(graph: "KnowledgeGraph") -> None:
    hidden = trace(graph, TraceOptions(ENTRY)).walk.steps
    shown = trace(graph, TraceOptions(ENTRY, include_infra=True)).walk.steps
    for node in ("Function:db_query", "File:libs/db_stdlib.php"):
        assert node not in hidden
        assert node in shown


def test_min_confidence_one_does_not_follow_references(graph: "KnowledgeGraph") -> None:
    strict = trace(graph, TraceOptions(ENTRY, min_confidence=1.0))
    assert not [s for s in strict.walk.steps.values() if s.edge_type == "REFERENCES"]
    assert "File:ped4_baixaestoque004.php" not in strict.walk.steps
    assert "File:ped4_baixaestoque004.php" in trace(graph, TraceOptions(ENTRY)).walk.steps


def test_expand_pgfunctions_goes_into_the_database(graph: "KnowledgeGraph") -> None:
    function = "PgFunction:public.fc_saldo_estoque(int4,numeric)"
    only_by_function = "Table:public.parametroloja"  # só o corpo da função a lê
    leaf = trace(graph, TraceOptions(ENTRY)).walk
    expanded = trace(graph, TraceOptions(ENTRY, expand_pgfunctions=True)).walk
    assert function in leaf.steps and leaf.children().get(function) is None
    assert only_by_function not in leaf.steps
    assert only_by_function in expanded.children()[function]


def test_cli_trace_writes_markdown_with_mermaid(db: Path, tmp_path: Path) -> None:
    output = tmp_path / "trace.md"
    result = CliRunner().invoke(
        app, ["trace", "--entry", ENTRY, "--db", str(db), "--output", str(output)]
    )
    assert result.exit_code == 0, result.output
    text = output.read_text(encoding="utf-8")
    assert_valid_mermaid(text)
    assert "## Tabelas tocadas" in text and "`Table:public.pedidocancelado`" in text


def test_cli_trace_shows_dispatch_actions_and_pending(db: Path, tmp_path: Path) -> None:
    output = tmp_path / "baixa.md"
    result = CliRunner().invoke(
        app,
        ["trace", "--entry", "ped4_baixaestoque004.php", "--db", str(db), "--output", str(output)],
    )
    assert result.exit_code == 0, result.output
    text = output.read_text(encoding="utf-8")
    assert "cancelar, getItens, getEntregas" in text  # dispatch_actions
    assert "unresolved_include" in text  # model/estoque/ReservaEstoque.model.php ausente


def test_cli_trace_unknown_entry_suggests(db: Path) -> None:
    result = CliRunner().invoke(app, ["trace", "--entry", "cancelarpedido.php", "--db", str(db)])
    assert result.exit_code == 1
    assert "não encontrado" in result.output and ENTRY in result.output


def test_mermaid_is_cut_keeping_paths_to_tables(graph: "KnowledgeGraph") -> None:
    result = trace(graph, TraceOptions(ENTRY))
    text = render.render_trace(result, FIXED_META, [], max_mermaid_nodes=8)
    block = assert_valid_mermaid(text)
    assert len(re.findall(r"^    n\d+[\[(]", block, re.MULTILINE)) == 8
    tables = re.findall(r'^    (n\d+)\[\("Table: ', block, re.MULTILINE)
    assert tables
    for table in tables:  # toda tabela exibida tem a aresta que chega nela
        assert re.search(rf"\| {table}$", block, re.MULTILINE)
    assert "Diagrama cortado em 8 nós" in text


# --------------------------------------------------------------------------- impact


def test_impact_lists_direct_writers_and_callers(graph: "KnowledgeGraph") -> None:
    result = impact(graph, ImpactOptions("pedido"))
    direct = {(d.origin, d.operation) for d in result.direct}
    assert ("Method:cl_pedido::incluir", "INSERT") in direct
    assert ("Method:cl_pedido::alterar", "UPDATE") in direct
    files = {f.path for f in result.files}
    assert {
        "classes/pedido.php",
        "ped4_baixaestoque004.php",
        "ped4_liberarpedidos.RPC.php",
        "forms/db_frmcancelarpedido.php",
    } <= files


def test_impact_reaches_the_table_through_a_database_function(graph: "KnowledgeGraph") -> None:
    result = impact(graph, ImpactOptions("estoque"))
    direct = {(d.origin, d.operation) for d in result.direct}
    assert ("Method:lancamentoestoque::estornar", "UPDATE") in direct
    assert ("PgFunction:public.fc_saldo_estoque(int4,numeric)", "INSERT") in direct


def test_cli_impact_markdown_and_mermaid_export(db: Path, tmp_path: Path) -> None:
    md, mmd = tmp_path / "impact.md", tmp_path / "impact.mmd"
    runner = CliRunner()
    base = ["impact", "--table", "pedido", "--db", str(db)]
    assert runner.invoke(app, [*base, "--output", str(md)]).exit_code == 0
    text = md.read_text(encoding="utf-8")
    assert_valid_mermaid(text)
    assert "`Method:cl_pedido::incluir` | Method | INSERT" in text
    assert runner.invoke(app, [*base, "--export", "mermaid", "--output", str(mmd)]).exit_code == 0
    only = mmd.read_text(encoding="utf-8")
    assert only.startswith("```mermaid\nflowchart LR") and "## " not in only
    assert_valid_mermaid(only)


def test_cli_impact_unknown_table(db: Path) -> None:
    result = CliRunner().invoke(app, ["impact", "--table", "pedid", "--db", str(db)])
    assert result.exit_code == 1 and "public.pedido" in result.output
