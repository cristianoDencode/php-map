"""Relatório de fronteira (plano de corte por endpoint e acoplamento por módulo)."""

import csv
import io
import json
from pathlib import Path

import pytest
from cross_graphs import (
    DYNAMIC,
    EXTERNAL,
    GET_A,
    GET_B,
    META_CHAMA,
    META_SERVE,
    MISSING,
    PRESTACAO,
    PROC_A,
    _edge,
    _endpoint,
    _node,
    build_chama,
    build_serve,
)
from cross_graphs import save as _save
from typer.testing import CliRunner

from php_map.analysis import boundary
from php_map.analysis.aggregate import build_analytics, write_analytics
from php_map.analysis.boundary import (
    ALVO_DINAMICO,
    COMANDO,
    EXTERNO,
    INDETERMINADO,
    LEITURA,
    SEM_ALCANCE,
    SEM_SERVIDOR,
    build_report,
    is_real_table,
    table_writers,
)
from php_map.cli import app
from php_map.config import Settings
from php_map.errors import QueryError, UsageError
from php_map.graph import store
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    EdgeSource,
    EdgeType,
    NodeLabel,
    endpoint_id,
    file_id,
    method_id,
    module_id,
    table_id,
)
from php_map.mcp import tools
from php_map.mcp.catalog import ProjectCatalog
from php_map.report import boundary as boundary_report

runner = CliRunner()


def _with_analytics(tmp_path: Path, name: str, b: GraphBuilder, meta: dict[str, str]) -> Path:
    db = _save(tmp_path, name, b, meta)
    write_analytics(db, build_analytics(b, Settings()))
    return db


@pytest.fixture
def dbs(tmp_path: Path) -> tuple[Path, Path]:
    return (
        _with_analytics(tmp_path, "serve", build_serve(), META_SERVE),
        _with_analytics(tmp_path, "chama", build_chama(), META_CHAMA),
    )


def _plans(report: boundary.BoundaryReport) -> dict[str, boundary.EndpointPlan]:
    return {p.endpoint_id: p for p in report.endpoints}


# --------------------------------------------------------------------------- natureza


def test_natures_and_reasons_across_projects(dbs: tuple[Path, Path]) -> None:
    serve, chama = dbs
    plans = _plans(build_report(chama, [serve], selection="called"))
    assert (plans[GET_A].natureza, plans[GET_A].served_by) == (LEITURA, "ecidade-t")
    assert plans[GET_A].reads == ("Table:public.t1", "Table:public.t3")
    assert plans[PROC_A].natureza == COMANDO and plans[PROC_A].writes == ("Table:public.t2",)
    assert (plans[MISSING].natureza, plans[MISSING].motivo) == (INDETERMINADO, SEM_SERVIDOR)
    assert plans[DYNAMIC].motivo == ALVO_DINAMICO
    # chamadores vêm do cross: 2 nós, 3 ocorrências, módulos de quem chama
    assert (plans[GET_A].callers, plans[GET_A].calls) == (2, 3)
    assert plans[GET_A].caller_projects == ("zend-t",)
    assert plans[GET_A].params == ("cnpj",)
    served = _plans(build_report(serve, [chama], selection="all"))
    assert (served[GET_B].natureza, served[GET_B].motivo) == (INDETERMINADO, SEM_ALCANCE)
    assert served[EXTERNAL].motivo == EXTERNO


def test_matched_is_the_default_with_cross_and_called_without(dbs: tuple[Path, Path]) -> None:
    serve, chama = dbs
    with_cross = build_report(chama, [serve])
    assert with_cross.selection == "matched"
    assert MISSING not in _plans(with_cross) and GET_A in _plans(with_cross)
    alone = build_report(chama)
    assert alone.selection == "called" and MISSING in _plans(alone)
    # sem o outro grafo, quem serve não é conhecido: tudo indeterminado (sem_servidor)
    assert _plans(alone)[GET_A].motivo == SEM_SERVIDOR
    with pytest.raises(UsageError, match="--cross"):
        build_report(chama, selection="matched")
    with pytest.raises(UsageError, match="--endpoints"):
        build_report(chama, selection="todos")


def _trap_graph() -> GraphBuilder:
    """O endpoint lê `t` (passo 1) e o método que o serve escreve em `t`: o `Walk` guarda só o
    `QUERIES`. Mais: escrita só em tabela de infraestrutura, e escritores via DAO."""
    b = GraphBuilder()
    ep, infra_ep = endpoint_id("s", "soap", "e"), endpoint_id("s", "soap", "audita")
    serve_m, dao = method_id("ws", "run"), method_id("cl_t", "incluir")
    for node, label, path in (
        (serve_m, NodeLabel.METHOD, "ws.php"),
        (dao, NodeLabel.METHOD, "classes/db_t.php"),
        (method_id("tela", "a"), NodeLabel.METHOD, "a.php"),
        (method_id("tela", "b"), NodeLabel.METHOD, "b.php"),
    ):
        _node(b, node, label, path=path)
    _endpoint(b, ep, kind="soap_class", path="ws.php", line=1)
    _endpoint(b, infra_ep, kind="soap_class", path="ws.php", line=2)
    _edge(b, ep, table_id("t"), EdgeType.QUERIES, EdgeSource.WS_XML)
    _edge(b, serve_m, ep, EdgeType.SERVES, line=3)
    _edge(b, serve_m, dao, EdgeType.CALLS, line=4)
    _edge(b, dao, table_id("t"), EdgeType.INSERTS, line=9)
    _edge(b, file_id("x.php"), table_id("t"), EdgeType.UPDATES, line=1)
    _edge(b, method_id("tela", "a"), dao, EdgeType.CALLS, line=5)
    _edge(b, method_id("tela", "b"), dao, EdgeType.INSTANTIATES, line=6)
    # o endpoint de auditoria só escreve numa tabela de infraestrutura
    audit = method_id("ws", "audit")
    _node(b, audit, NodeLabel.METHOD, path="ws.php")
    _node(b, table_id("db_acount"), NodeLabel.TABLE, is_infra=True)
    _edge(b, audit, infra_ep, EdgeType.SERVES, line=7)
    _edge(b, audit, table_id("db_acount"), EdgeType.INSERTS, line=8)
    _edge(b, infra_ep, table_id("t"), EdgeType.QUERIES, EdgeSource.WS_XML)
    return b


def test_write_after_read_is_a_command_and_infra_writes_are_not(tmp_path: Path) -> None:
    db = _save(tmp_path, "trap", _trap_graph(), {"project": "s", "profile": "ecidade"})
    plans = _plans(build_report(db, selection="served"))
    first = plans[endpoint_id("s", "soap", "e")]
    assert first.natureza == COMANDO and first.writes == ("Table:public.t",)
    assert first.reads == ()  # a tabela escrita não conta também como lida
    assert (first.max_writers_direct, first.max_writers_via_dao) == (2, 3)
    audited = plans[endpoint_id("s", "soap", "audita")]
    assert audited.natureza == LEITURA and audited.writes == ()


def test_table_writers_direct_and_via_dao(tmp_path: Path) -> None:
    db = _save(tmp_path, "trap", _trap_graph(), {"project": "s", "profile": "ecidade"})
    with store.connect_readonly(db) as conn:
        found = table_writers(conn, ["Table:public.t", "Table:public.nada"])
    assert found["Table:public.t"] == boundary.TableWriters("Table:public.t", 2, 3)
    assert found["Table:public.nada"] == boundary.TableWriters("Table:public.nada", 0, 0)


@pytest.mark.parametrize(
    ("node", "real"),
    [
        ("Table:public.cgm", True),
        ("Table:acordos.acordo_x2", True),
        ("Table:public.(", False),
        ("Table:public.(select", False),
        ("Table:public.fc_statusdebitos($numpre,", False),
    ],
)
def test_real_table_filter(node: str, real: bool) -> None:
    assert is_real_table(node) is real


# --------------------------------------------------------------------------- módulos


def _modules_graph() -> GraphBuilder:
    """`a` (3 arquivos) escreve em `tb`, que é de `b` (2 arquivos); `b` lê `ta`, que é de `a`;
    `c` (2 arquivos) está isolado; `d` (1 arquivo) fica fora do ranking."""
    b = GraphBuilder()
    layout = {"a": 3, "b": 2, "c": 2, "d": 1}
    for module, files in layout.items():
        for i in range(files):
            path = f"{module}{i}.php"
            _node(b, file_id(path), NodeLabel.FILE, path=path)
            _edge(
                b, file_id(path), module_id(module), EdgeType.BELONGS_TO, EdgeSource.ECIDADE_CATALOG
            )
    for table, owner in (("ta", "a"), ("tb", "b"), ("tc", "c")):
        _node(b, table_id(table), NodeLabel.TABLE)
        _edge(b, table_id(table), module_id(owner), EdgeType.BELONGS_TO, EdgeSource.ECIDADE_CATALOG)
    _edge(b, file_id("a0.php"), table_id("tb"), EdgeType.INSERTS, line=1)
    _edge(b, file_id("b0.php"), table_id("ta"), EdgeType.QUERIES, line=1)
    _edge(b, file_id("c0.php"), table_id("tc"), EdgeType.UPDATES, line=1)
    return b


@pytest.fixture
def modules_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(boundary, "MIN_FILES", 2)
    return _with_analytics(tmp_path, "mods", _modules_graph(), {"project": "m"})


def test_candidates_cost_and_tie_break(modules_db: Path) -> None:
    report = build_report(modules_db)
    assert report.has_modules and report.modules_total == 4
    ranking = [(c.module, c.ca, c.ce, c.writes_into_others, c.owned_written_by_others, c.cost)
               for c in report.candidates]  # fmt: skip
    assert ranking == [
        ("Module:c", 0, 0, 0, 0, 0),
        ("Module:a", 1, 1, 1, 0, 4),  # empate com b: mais arquivos primeiro
        ("Module:b", 1, 1, 0, 1, 4),
    ]
    assert {(m.src, m.dst) for m in report.matrix} >= {
        ("Module:a", "Module:b"),
        ("Module:b", "Module:a"),
    }


def test_module_card_and_unknown_module(modules_db: Path) -> None:
    card = build_report(modules_db, module="b").module
    assert card is not None and card.module == "Module:b" and card.tables_owned == 1
    assert [(t.table, t.modules) for t in card.owned_written_by_others] == [
        ("Table:public.tb", ("Module:a",))
    ]
    assert card.writes_into_others == () and card.cost == 4
    assert {e.module for e in card.edges_in} == {"Module:a"}
    a = build_report(modules_db, module="Module:a").module
    assert a is not None and [t.table for t in a.writes_into_others] == ["Table:public.tb"]
    with pytest.raises(UsageError, match="módulo inexistente: zz"):
        build_report(modules_db, module="zz")


def test_report_without_modules_from_plain_graph(tmp_path: Path) -> None:
    db = _with_analytics(tmp_path, "trap", _trap_graph(), {"project": "s"})
    report = build_report(db, selection="served")
    assert not report.has_modules and report.warnings == (boundary.NO_MODULES_WARNING,)
    bare = _save(tmp_path, "bare", _trap_graph(), {"project": "s"})  # sem analytics_*
    assert build_report(bare, selection="served").warnings == (boundary.NO_ANALYTICS_WARNING,)
    assert boundary_report.to_markdown(report).count("não tem módulos") == 1
    with pytest.raises(UsageError, match="não tem módulos"):
        build_report(db, module="x")


# --------------------------------------------------------------------------- saída e CLI


def test_formats(modules_db: Path, dbs: tuple[Path, Path]) -> None:
    serve, chama = dbs
    report = build_report(chama, [serve])
    data = json.loads(boundary_report.to_json(report))
    assert data["natureza_counts"][COMANDO] >= 1 and data["cost_formula"].startswith("custo")
    rows = list(csv.DictReader(io.StringIO(boundary_report.to_csv(report, "endpoints"))))
    assert {r["endpoint"] for r in rows} == {p.endpoint_id for p in report.endpoints}
    mods = build_report(modules_db)
    assert (
        list(csv.DictReader(io.StringIO(boundary_report.to_csv(mods, "modules"))))[0]["module"]
        == "Module:c"
    )
    matrix = list(csv.DictReader(io.StringIO(boundary_report.to_csv(mods, "matrix"))))
    assert {"src", "dst", "kind", "weight", "edges"} <= set(matrix[0])
    md = boundary_report.to_markdown(mods)
    assert "## Candidatos à extração" in md and "## Matriz de acoplamento" in md
    with pytest.raises(ValueError, match="--what"):
        boundary_report.to_csv(mods, "tudo")


def test_markdown_never_rounds_a_small_weight_to_zero() -> None:
    assert boundary_report._weight(0.01) == "<0.1"  # noqa: SLF001
    assert boundary_report._weight(0.0) == ""  # noqa: SLF001
    assert boundary_report._weight(2.25) == "2.2"  # noqa: SLF001


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["--format", "xml"], 2),
        (["--format", "csv", "--what", "tudo"], 2),
        (["--module", "zz"], 2),
        (["--endpoints", "matched"], 2),
    ],
)
def test_cli_usage_errors(modules_db: Path, args: list[str], code: int) -> None:
    result = runner.invoke(app, ["boundary", "--db", str(modules_db), *args])
    assert result.exit_code == code, result.output


def test_cli_writes_outputs(modules_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "r" / "fronteira.md"
    result = runner.invoke(app, ["boundary", "--db", str(modules_db), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "custo_corte" in out.read_text(encoding="utf-8")
    result = runner.invoke(
        app, ["boundary", "--db", str(modules_db), "--format", "csv", "--what", "matrix"]
    )
    assert result.exit_code == 0 and result.stdout.startswith("src,dst,kind")


# --------------------------------------------------------------------------- MCP


def test_mcp_boundary_tools(dbs: tuple[Path, Path], modules_db: Path, tmp_path: Path) -> None:
    catalog = ProjectCatalog.from_dbs(list(dbs))
    plan = tools.cut_plan(catalog, "zend-t")
    assert plan["selection"] == "matched" and plan["total"] >= 2
    commands = tools.cut_plan(catalog, "zend-t", natureza=COMANDO)
    # procA (servido pelo e-cidade) e Prestacao (servido pelo zend-t, escreve em zend_t)
    assert {e["endpoint_id"] for e in commands["endpoints"]} == {PROC_A, PRESTACAO}
    capped = tools.cut_plan(catalog, "zend-t", limit=1)
    assert capped["truncated"] and len(capped["endpoints"]) == 1
    with pytest.raises(QueryError, match="natureza"):
        tools.cut_plan(catalog, "zend-t", natureza="tudo")
    mods = ProjectCatalog.from_dbs([modules_db])
    assert tools.cut_plan(mods, "m")["selection"] == "called"
    card = tools.module_boundary(mods, "m", "b")
    assert card["cost"] == 4 and card["cost_formula"].startswith("custo_corte")
    with pytest.raises(QueryError, match="inexistente"):
        tools.module_boundary(mods, "m", "zz")
    ranking = tools.extraction_candidates(mods, "m", top=2)
    assert [c["module"] for c in ranking["candidates"]] == ["Module:c", "Module:a"]
    assert ranking["warning"] is None
    plain = _with_analytics(tmp_path, "plain", _trap_graph(), {"project": "s"})
    no_modules = tools.extraction_candidates(ProjectCatalog.from_dbs([plain]), "s")
    assert no_modules["candidates"] == [] and no_modules["warning"] == boundary.NO_MODULES_WARNING
