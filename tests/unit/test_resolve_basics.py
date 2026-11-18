"""classify, unresolved e includes."""

import io
import json

from conftest import PhpRecords

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.ingest.records import Include
from php_map.resolve.classify import is_infra_file, is_infra_symbol, module_hint
from php_map.resolve.includes import (
    IncludeGraph,
    ResolvedInclude,
    normalize_path,
    resolve_file,
    resolve_includes,
)
from php_map.resolve.unresolved import UnresolvedWriter


def test_module_hint() -> None:
    assert module_hint("emp4_anularempenho001.php") == "emp"
    assert module_hint("sub/dir/Con1_periodo001.php") == "con"
    assert module_hint("forms/db_frmempanularempenho.php") is None
    assert module_hint("classes/empenho.php") is None


def test_infra_lists() -> None:
    settings = Settings()
    assert is_infra_symbol("DB_Query", settings)
    assert is_infra_symbol("db_utils", settings)
    assert not is_infra_symbol("empenho", settings)
    assert is_infra_file("libs/db_stdlib.php", settings)
    assert not is_infra_file("emp4_anularempenho001.php", settings)


def test_third_party_dirs_mark_is_infra() -> None:
    """Arquivo dentro de `third_party_dirs` é terceiro, is_infra=True."""
    settings = Settings(third_party_dirs=("library/Zend", "library/Doctrine"))
    assert is_infra_file("library/Zend/Soap/Client.php", settings)
    assert is_infra_file("library/Zend", settings)  # o próprio diretório, sem sufixo
    assert not is_infra_file("library/DBSeller/Helper/Number.php", settings)  # código próprio
    assert not is_infra_file("application/Bootstrap.php", settings)
    # `library/ZendAlgoQueNaoE` não deve casar por prefixo de string (só por diretório inteiro).
    assert not is_infra_file("library/ZendAlgoQueNaoE/X.php", settings)


def test_unresolved_writer_jsonl_and_aggregation() -> None:
    sink = io.StringIO()
    with UnresolvedWriter(sink) as writer:
        writer.add("dynamic_include", "a.php", 3, "require x", ["b.php"], hint="x")
        for line in (10, 11, 12):
            writer.add_aggregated(
                "ambiguous_function", "dao.php", line, "db_query()", hint="db_query"
            )
        assert sink.getvalue().count("\n") == 1  # agregados só no close()

    rows = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert rows[0] == {
        "kind": "dynamic_include",
        "path": "a.php",
        "line": 3,
        "snippet": "require x",
        "candidates": ["b.php"],
        "hint": "x",
        "count": 1,
    }
    assert rows[1]["kind"] == "ambiguous_function"
    assert rows[1]["line"] == 10 and rows[1]["count"] == 3
    assert writer.counts == {"dynamic_include": 1, "ambiguous_function": 3}


def test_normalize_path() -> None:
    assert normalize_path("./libs/../libs/db_stdlib.php") == "libs/db_stdlib.php"
    assert normalize_path("libs\\JSON.php") == "libs/JSON.php"
    assert normalize_path("../fora.php") is None
    assert normalize_path("") is None


def test_resolve_file_prefers_root_then_directory() -> None:
    files = {"a.php", "forms/a.php", "forms/b.php"}
    assert resolve_file("a.php", "forms/x.php", files) == "a.php"
    assert resolve_file("b.php", "forms/x.php", files) == "forms/b.php"
    assert resolve_file("../a.php", "forms/x.php", files) == "a.php"
    assert resolve_file("c.php", "forms/x.php", files) is None


def test_closure_includes_self_and_survives_cycles() -> None:
    graph = IncludeGraph()
    for src, dst in (("a", "b"), ("b", "c"), ("c", "a"), ("d", "a")):
        graph.add(ResolvedInclude(src, dst, 1, {}))
    assert graph.closure("a") == {"a", "b", "c"}
    assert graph.closure("d") == {"a", "b", "c", "d"}
    assert graph.closure("solto") == {"solto"}


def test_resolve_includes_edges_and_pending(php: PhpRecords) -> None:
    files = {"tela.php", "libs/db_stdlib.php", "libs/JSON.php", "db_conn.php", "x/db_conn.php"}
    includes = [
        Include.model_validate(php.inc("libs/db_stdlib.php", kind="modification", line=2)),
        Include.model_validate(php.inc("libs/JSON.php", type_="include", line=3)),
        Include.model_validate(php.inc("model/ausente.model.php", line=4)),
        Include.model_validate(php.inc(None, kind="interpolated", hint="db_conn.php", line=5)),
    ]
    graph, builder, writer = IncludeGraph(), GraphBuilder(), UnresolvedWriter()
    resolve_includes("tela.php", includes, files, graph, builder, writer)

    edges = {(e.dst, e.type.value) for e in builder.iter_edges()}
    assert edges == {("File:libs/db_stdlib.php", "REQUIRES"), ("File:libs/JSON.php", "INCLUDES")}
    pending = {(r.kind, r.hint): r.candidates for r in writer.records}
    assert pending == {
        ("unresolved_include", "model/ausente.model.php"): [],
        ("dynamic_include", "db_conn.php"): ["db_conn.php", "x/db_conn.php"],
    }
    assert graph.closure("tela.php") == {"tela.php", "libs/db_stdlib.php", "libs/JSON.php"}
