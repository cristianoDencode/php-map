"""`cross` (join por `Endpoint`), `reaches`, `trace --cross` e a CLI.

Dois grafos sintéticos persistidos de verdade (`store.save`), como os que o `scan` gera:
- `serve` (projeto `ecidade-t`, perfil `ecidade`): serve `getA`, `getB` e `procA`, e chama
  `Prestacao` (o sentido contrário);
- `chama` (projeto `zend-t`, perfil `zend1`): chama `getA`, `procA` e `Missing`, e serve
  `Prestacao`.
"""

import csv
import io
import json
from pathlib import Path
from typing import Any

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
    RPC,
    build_chama,
    build_serve,
)
from cross_graphs import save as _save
from typer.testing import CliRunner

from php_map.analysis import TRAVERSED, walk
from php_map.analysis.cross import (
    CASADO,
    CROSS_EDGES,
    SEM_CLIENTE,
    SEM_SERVIDOR,
    Neighborhood,
    cross_projects,
    lazy_walk,
    trace_across,
)
from php_map.analysis.trace import TraceOptions
from php_map.cli import app
from php_map.errors import QueryError, UsageError
from php_map.graph import store
from php_map.graph.model import file_id, method_id, module_id, table_id
from php_map.report import cross as cross_report
from php_map.report import render

runner = CliRunner()


@pytest.fixture
def dbs(tmp_path: Path) -> tuple[Path, Path]:
    return (
        _save(tmp_path, "serve", build_serve(), META_SERVE),
        _save(tmp_path, "chama", build_chama(), META_CHAMA),
    )


def _by_id(report: Any) -> dict[str, Any]:
    return {e.endpoint_id: e for e in report.endpoints}


# --------------------------------------------------------------------------- status


def test_statuses_cover_casado_orphans_and_both_directions(dbs: tuple[Path, Path]) -> None:
    report = cross_projects(dbs)
    by_id = _by_id(report)
    assert {e: by_id[e].status for e in (GET_A, PROC_A, PRESTACAO)} == {
        GET_A: CASADO,
        PROC_A: CASADO,
        PRESTACAO: CASADO,  # costura no sentido contrário: o e-cidade chama, o zend-t serve
    }
    assert by_id[MISSING].status == SEM_SERVIDOR and by_id[MISSING].served_by is None
    assert by_id[GET_B].status == SEM_CLIENTE and by_id[GET_B].callers == ()
    assert by_id[RPC].status == SEM_CLIENTE
    assert report.counts == {CASADO: 3, SEM_SERVIDOR: 1, SEM_CLIENTE: 2}
    assert by_id[GET_A].served_by == "ecidade-t" and by_id[PRESTACAO].served_by == "zend-t"
    # `?` (nome não literal) e `externo` não casam com ninguém: ficam fora dos endpoints
    assert DYNAMIC not in by_id and EXTERNAL not in by_id


def test_dynamic_and_external_calls_are_counted_apart(dbs: tuple[Path, Path]) -> None:
    report = cross_projects(dbs)
    assert [(c.project, c.calls) for c in report.dynamic_calls] == [("zend-t", 1)]
    assert [(c.project, c.calls) for c in report.external_calls] == [("ecidade-t", 3)]


def test_endpoints_are_sorted_by_number_of_callers(dbs: tuple[Path, Path]) -> None:
    report = cross_projects(dbs)
    sizes = [len(e.callers) for e in report.endpoints]
    assert sizes == sorted(sizes, reverse=True)
    assert report.endpoints[0].endpoint_id == GET_A  # 2 chamadores (A.x e B.y)


def test_callers_carry_project_module_args_and_occurrences(dbs: tuple[Path, Path]) -> None:
    get_a = _by_id(cross_projects(dbs))[GET_A]
    assert [(c.project, c.module, c.line, c.occurrences) for c in get_a.callers] == [
        ("zend-t", module_id("contribuinte"), 20, 2),
        ("zend-t", module_id("fiscal"), 10, 1),
    ]
    assert get_a.callers[0].path == "application/modules/contribuinte/models/B.php"
    assert get_a.callers[0].args == {"argc": 2, "literals": ["getA"]}
    assert get_a.calls == 3
    prestacao = _by_id(cross_projects(dbs))[PRESTACAO]
    assert [(c.project, c.module) for c in prestacao.callers] == [
        ("ecidade-t", module_id("tributario"))
    ]


def test_servers_and_declared_params_come_from_the_serving_graph(dbs: tuple[Path, Path]) -> None:
    get_a = _by_id(cross_projects(dbs))[GET_A]
    assert [(s.project, s.node, s.path, s.line) for s in get_a.servers] == [
        ("ecidade-t", file_id("ws/getA.xml"), "ws/getA.xml", 1),
        ("ecidade-t", file_id("ws/server.php"), "ws/server.php", None),  # o XML é a evidência
    ]
    assert get_a.params == ({"name": "cnpj", "required": True, "type": None, "source": "ws_xml"},)
    assert get_a.kind == "soap_consulta" and (get_a.system, get_a.protocol) == ("ecidade", "soap")
    assert _by_id(cross_projects(dbs))[MISSING].params == ()  # placeholder: sem contrato


# --------------------------------------------------------------------------- reaches


def test_reaches_has_tables_modules_and_node_count(dbs: tuple[Path, Path]) -> None:
    by_id = _by_id(cross_projects(dbs, depth=3))
    get_a = by_id[GET_A].reaches
    assert get_a.tables == (table_id("t1"), table_id("t3")) and get_a.nodes == 2
    proc_a = by_id[PROC_A].reaches
    # Class, run, t2, deep::a, deep::b (depth 3). `db_query` é infra; `deep::c` e `far` ficam fora.
    assert proc_a.tables == (table_id("t2"),)
    assert proc_a.nodes == 5
    # o arquivo de `run` tem 2 módulos; vale o de maior confiança (1.0)
    assert proc_a.modules == (module_id("empenho"),)
    assert by_id[MISSING].reaches.nodes == 0 and by_id[MISSING].reaches.tables == ()


def test_reaches_respects_depth(dbs: tuple[Path, Path]) -> None:
    deep = _by_id(cross_projects(dbs, depth=5))[PROC_A].reaches
    assert table_id("far") in deep.tables  # run→a→b→c→far: 4 passos do endpoint + o CALLS
    assert table_id("infra_only") not in deep.tables  # db_query é infraestrutura
    one = _by_id(cross_projects(dbs, depth=1))[PROC_A].reaches
    assert one.tables == () and one.nodes == 2  # só a Class e o método de 1 passo
    zero = _by_id(cross_projects(dbs, depth=0))[PROC_A].reaches
    assert zero.nodes == 0


def test_reaches_starts_at_the_php_method_that_serves_the_endpoint(
    dbs: tuple[Path, Path],
) -> None:
    """`Method SERVES Endpoint` (setClass): o código continua no método, não no `File` XML."""
    prestacao = _by_id(cross_projects(dbs))[PRESTACAO].reaches
    assert prestacao.tables == (table_id("zend_t"),)
    assert prestacao.modules == (module_id("webservice"),)
    assert prestacao.nodes == 2  # o método e a tabela
    # `File` que serve (XML, despachante) não vira ponto de partida: o RPC fica sem alcance
    assert _by_id(cross_projects(dbs))[RPC].reaches.nodes == 0


# --------------------------------------------------------------------------- resumo por par


def test_dependencies_per_pair_of_projects(dbs: tuple[Path, Path]) -> None:
    deps = {(d.caller, d.server): d for d in cross_projects(dbs).dependencies}
    zend = deps[("zend-t", "ecidade-t")]
    assert (zend.endpoints, zend.calls) == (2, 4)  # getA (3 chamadas) + procA (1)
    assert [(m.module, m.calls, m.endpoints) for m in zend.modules] == [
        (module_id("contribuinte"), 3, 2),
        (module_id("fiscal"), 1, 1),
    ]
    assert zend.tables == (table_id("t1"), table_id("t2"), table_id("t3"))
    assert zend.reached_modules == (module_id("empenho"),)
    reverse = deps[("ecidade-t", "zend-t")]
    assert (reverse.endpoints, reverse.calls) == (1, 1)
    assert reverse.tables == (table_id("zend_t"),)


def test_filtered_recomputes_the_summary(dbs: tuple[Path, Path]) -> None:
    report = cross_projects(dbs)
    only = report.filtered(only=SEM_SERVIDOR)
    assert [e.endpoint_id for e in only.endpoints] == [MISSING]
    assert only.counts == {CASADO: 0, SEM_SERVIDOR: 1, SEM_CLIENTE: 0}
    assert only.dependencies == ()
    rpc = report.filtered(protocol="rpc")
    assert [e.endpoint_id for e in rpc.endpoints] == [RPC]
    zend = report.filtered(system="zend1app")
    assert [e.endpoint_id for e in zend.endpoints] == [PRESTACAO]
    assert {p.slug: p.served for p in zend.projects} == {"ecidade-t": 0, "zend-t": 1}
    assert len(report.endpoints) == 6  # o original não muda


# --------------------------------------------------------------------------- erros


def test_two_databases_of_the_same_project_are_refused(tmp_path: Path) -> None:
    first = _save(tmp_path, "a", build_serve(), META_SERVE)
    second = _save(tmp_path, "b", build_serve(), META_SERVE)  # outra época do mesmo e-cidade
    with pytest.raises(UsageError, match="mesmo projeto.*ecidade-t"):
        cross_projects([first, second])


def test_at_least_two_databases_are_required(dbs: tuple[Path, Path]) -> None:
    with pytest.raises(UsageError, match="ao menos dois"):
        cross_projects([dbs[0]])


def test_missing_database_is_a_store_error(dbs: tuple[Path, Path], tmp_path: Path) -> None:
    from php_map.errors import StoreError

    with pytest.raises(StoreError, match="inexistente"):
        cross_projects([dbs[0], tmp_path / "nao-existe.db"])


def test_database_without_meta_project_falls_back_to_the_file_name(tmp_path: Path) -> None:
    serve = _save(
        tmp_path, "s", build_serve(), {"profile": "ecidade"}
    )  # mapa antigo: sem `project`
    chama = _save(tmp_path, "c", build_chama(), META_CHAMA)
    report = cross_projects([serve, chama])
    assert [p.slug for p in report.projects] == ["graph", "zend-t"]
    assert any("sem meta.project" in w and "graph" in w for w in report.warnings)
    assert report.counts[CASADO] == 3  # o join não depende do nome do projeto


def test_two_databases_without_meta_project_are_told_apart_by_folder(tmp_path: Path) -> None:
    serve = _save(tmp_path, "s", build_serve(), {})
    chama = _save(tmp_path, "c", build_chama(), {})
    report = cross_projects([serve, chama])
    assert [p.slug for p in report.projects] == ["s/graph", "c/graph"]
    assert len(report.warnings) == 2


# --------------------------------------------------------------------------- saídas


def test_json_is_the_whole_report(dbs: tuple[Path, Path]) -> None:
    data = json.loads(cross_report.to_json(cross_projects(dbs)))
    assert set(data) == {
        "schema_version",
        "depth",
        "projects",
        "counts",
        "endpoints",
        "dependencies",
        "dynamic_calls",
        "external_calls",
        "warnings",
    }
    assert data["schema_version"] == 1 and data["depth"] == 3
    get_a = next(e for e in data["endpoints"] if e["endpoint_id"] == GET_A)
    assert set(get_a) == {
        "endpoint_id",
        "system",
        "protocol",
        "name",
        "kind",
        "served_by",
        "servers",
        "callers",
        "params",
        "reaches",
        "status",
    }
    assert get_a["reaches"] == {
        "tables": [table_id("t1"), table_id("t3")],
        "modules": [],
        "nodes": 2,
    }
    assert get_a["callers"][0]["module"] == module_id("contribuinte")
    assert data["projects"][0] == {
        "slug": "ecidade-t",
        "name": "e-cidade teste",
        "profile": "ecidade",
        "db": data["projects"][0]["db"],
        "served": 4,
        "called": 1,
    }


def test_csv_has_one_row_per_endpoint(dbs: tuple[Path, Path]) -> None:
    rows = list(csv.DictReader(io.StringIO(cross_report.to_csv(cross_projects(dbs)))))
    assert len(rows) == 6 and list(rows[0]) == list(cross_report.CSV_COLUMNS)
    get_a = next(r for r in rows if r["endpoint"] == GET_A)
    assert (get_a["status"], get_a["callers"], get_a["calls"], get_a["tables"]) == (
        CASADO,
        "2",
        "3",
        "2",
    )
    assert get_a["caller_modules"] == "contribuinte;fiscal"
    assert get_a["params"] == "cnpj"


def test_summary_lists_orphans_modules_tables_and_the_dead_code_warning(
    dbs: tuple[Path, Path],
) -> None:
    text = "\n".join(cross_report.summary_lines(cross_projects(dbs)))
    assert (
        "Endpoints: 5 servidos (ecidade-t 4, zend-t 1), 4 chamados (ecidade-t 1, zend-t 3)" in text
    )
    assert "chamado_sem_servidor" in text and "Missing" in text
    assert "servido_sem_cliente" in text and "(rpc 1, soap 1)" in text
    assert "Módulos do zend-t que dependem do ecidade-t: contribuinte (3), fiscal (1)" in text
    assert "Tabelas do ecidade-t alcançadas pelo zend-t: 3" in text
    assert "não quer dizer código morto" in text


# --------------------------------------------------------------------------- busca lazy


def test_lazy_walk_matches_the_graph_walk(dbs: tuple[Path, Path]) -> None:
    graph = store.load(dbs[0])
    import sqlite3

    with sqlite3.connect(dbs[0]) as conn:
        for edge_types in (CROSS_EDGES, TRAVERSED):
            nb = Neighborhood(conn, edge_types)
            for root in (PROC_A, GET_A, method_id("cl_x", "run")):
                for depth, min_conf, infra in ((3, 0.0, False), (6, 0.0, True), (6, 1.0, False)):
                    expected = walk(
                        graph,
                        root,
                        depth=depth,
                        min_confidence=min_conf,
                        include_infra=infra,
                        traversed=frozenset(edge_types),
                    )
                    got = lazy_walk(
                        nb, root, depth=depth, min_confidence=min_conf, include_infra=infra
                    )
                    assert list(got.steps) == list(expected.steps)
                    for node, step in expected.steps.items():
                        other = got.steps[node]
                        assert (other.parent, other.edge_type, other.depth, other.confidence) == (
                            step.parent,
                            step.edge_type,
                            step.depth,
                            step.confidence,
                        )


# --------------------------------------------------------------------------- trace --cross


def _trace_across(dbs: tuple[Path, Path], **options: Any) -> Any:
    chama = dbs[1]
    graph = store.load(chama)
    entry = "application/modules/fiscal/views/scripts/a.phtml"
    return trace_across(graph, "zend-t", TraceOptions(entry, **options), [dbs[0]])


def test_trace_crosses_the_seam_into_the_serving_project(dbs: tuple[Path, Path]) -> None:
    result = _trace_across(dbs, depth=8)
    crossings = {c.endpoint: c for c in result.crossings}
    assert set(crossings) == {GET_A, PROC_A}
    assert result.unserved == [MISSING]  # chamado e sem servidor em todos os bancos
    get_a = crossings[GET_A]
    assert (get_a.from_project, get_a.to_project, get_a.depth) == ("zend-t", "ecidade-t", 2)
    assert get_a.path == [  # os dois métodos chamam: a BFS fica com o 1º por ID
        file_id("application/modules/fiscal/views/scripts/a.phtml"),
        method_id("contribuinte_b", "y"),
        GET_A,
    ]
    assert [(t.operation, t.table, t.origin) for t in get_a.tables] == [
        ("SELECT", table_id("t1"), GET_A),
        ("SELECT", table_id("t3"), GET_A),
    ]
    proc = crossings[PROC_A]
    assert (table_id("t2")) in {t.table for t in proc.tables}
    assert proc.labels[table_id("t2")] == "Table"


def test_trace_across_uses_only_the_depth_that_is_left(dbs: tuple[Path, Path]) -> None:
    # A view → B.y (1) → procA (2): com depth=2 o endpoint entra, mas nada abaixo dele.
    shallow = _trace_across(dbs, depth=2)
    proc = next(c for c in shallow.crossings if c.endpoint == PROC_A)
    assert list(proc.walk.steps) == [PROC_A] and proc.tables == []
    one_left = _trace_across(dbs, depth=3)
    proc = next(c for c in one_left.crossings if c.endpoint == PROC_A)
    assert method_id("cl_x", "run") in proc.walk.steps
    assert table_id("t2") not in proc.walk.steps  # precisa de mais um passo


def test_trace_across_rejects_the_same_project(dbs: tuple[Path, Path]) -> None:
    graph = store.load(dbs[1])
    with pytest.raises(UsageError, match="mesmo projeto"):
        trace_across(
            graph,
            "zend-t",
            TraceOptions("application/modules/fiscal/views/scripts/a.phtml"),
            [dbs[1]],
        )


def test_trace_across_still_reports_a_missing_entry(dbs: tuple[Path, Path]) -> None:
    graph = store.load(dbs[1])
    with pytest.raises(QueryError, match="não encontrado"):
        trace_across(graph, "zend-t", TraceOptions("nao-existe.phtml"), [dbs[0]])


def test_trace_without_cross_does_not_follow_remote_calls(dbs: tuple[Path, Path]) -> None:
    from php_map.analysis.trace import trace

    graph = store.load(dbs[1])
    entry = "application/modules/fiscal/views/scripts/a.phtml"
    plain = trace(graph, TraceOptions(entry, depth=8))
    assert not any(label == "Endpoint" for label in plain.labels.values())


def test_rendered_trace_marks_the_crossing_and_the_project(dbs: tuple[Path, Path]) -> None:
    result = _trace_across(dbs, depth=8)
    text = render.render_trace(result.primary, {"scanned_at": "x", "source": "y"}, [], 60, result)
    assert "- ~> [ecidade-t] `Endpoint:ecidade:soap:getA`" in text
    assert "CALLS_REMOTE → [zend-t] `Endpoint:ecidade:soap:getA`" in text
    assert "QUERIES → [ecidade-t] `Table:public.t1`" in text
    assert "Travessias para outros projetos: 2" in text
    assert "endpoints chamados que nenhum dos bancos" in text
    assert "`Endpoint:ecidade:soap:Missing`" in text
    # a travessia aparece mais fundo que o endpoint de origem
    lines = text.splitlines()
    origin = next(
        i
        for i, line in enumerate(lines)
        if "CALLS_REMOTE → [zend-t] `Endpoint:ecidade:soap:getA`" in line
    )
    indent = len(lines[origin]) - len(lines[origin].lstrip())
    assert lines[origin + 1].startswith(" " * (indent + 2) + "- ~> [ecidade-t]")


# --------------------------------------------------------------------------- CLI


def test_cli_cross_table_json_csv_and_filters(dbs: tuple[Path, Path], tmp_path: Path) -> None:
    graphs = ["--graph", str(dbs[0]), "--graph", str(dbs[1])]
    table = runner.invoke(app, ["cross", *graphs, "--limit", "2"])
    assert table.exit_code == 0, table.output
    assert "casado" in table.output and "Missing" in table.output
    assert "2 de 6 endpoints" in table.output

    out = tmp_path / "cross.json"
    saved = runner.invoke(app, ["cross", *graphs, "--format", "json", "--out", str(out)])
    assert saved.exit_code == 0, saved.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["counts"] == {CASADO: 3, SEM_SERVIDOR: 1, SEM_CLIENTE: 2}

    only = runner.invoke(
        app, ["cross", *graphs, "--format", "csv", "--only", "chamado_sem_servidor"]
    )
    assert only.exit_code == 0, only.output
    assert only.stdout.splitlines()[0].startswith("endpoint,system,protocol")
    assert MISSING in only.stdout and GET_A not in only.stdout


def test_cli_cross_table_to_a_file_has_every_row(dbs: tuple[Path, Path], tmp_path: Path) -> None:
    out = tmp_path / "cross.txt"
    result = runner.invoke(
        app, ["cross", "--graph", str(dbs[0]), "--graph", str(dbs[1]), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    text = out.read_text(encoding="utf-8")
    assert "getB" in text and "Prestacao" in text and "não quer dizer código morto" in text


def test_cli_cross_usage_errors_exit_with_code_2(dbs: tuple[Path, Path], tmp_path: Path) -> None:
    same = _save(tmp_path, "same", build_serve(), META_SERVE)
    result = runner.invoke(app, ["cross", "--graph", str(dbs[0]), "--graph", str(same)])
    assert result.exit_code == 2 and "mesmo projeto" in result.output
    one = runner.invoke(app, ["cross", "--graph", str(dbs[0])])
    assert one.exit_code == 2 and "ao menos dois" in one.output
    for bad in (["--format", "xml"], ["--only", "talvez"], ["--protocol", "ftp"]):
        invalid = runner.invoke(
            app, ["cross", "--graph", str(dbs[0]), "--graph", str(dbs[1]), *bad]
        )
        assert invalid.exit_code == 2, bad
    missing = runner.invoke(
        app, ["cross", "--graph", str(dbs[0]), "--graph", str(tmp_path / "nada.db")]
    )
    assert missing.exit_code == 1 and "inexistente" in missing.output


def test_cli_trace_with_cross_writes_the_report(dbs: tuple[Path, Path], tmp_path: Path) -> None:
    out = tmp_path / "trace.md"
    result = runner.invoke(
        app,
        [
            "trace",
            "--db",
            str(dbs[1]),
            "--cross",
            str(dbs[0]),
            "--entry",
            "application/modules/fiscal/views/scripts/a.phtml",
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "travessias: 2" in result.output and "endpoints sem servidor: 1" in result.output
    assert "~> [ecidade-t]" in out.read_text(encoding="utf-8")
    same = runner.invoke(
        app, ["trace", "--db", str(dbs[1]), "--cross", str(dbs[1]), "--entry", "x.phtml"]
    )
    assert same.exit_code == 2
