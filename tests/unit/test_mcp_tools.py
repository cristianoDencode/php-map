"""Ferramentas do servidor MCP (funções puras) sobre dois grafos de fixture.

Os grafos são os do `cross` (`tests/cross_graphs.py`), persistidos de verdade e com o índice de
busca, como o `scan` grava. Nada aqui importa o SDK do MCP.
"""

import ast
import json
import os
from pathlib import Path

import pytest
from cross_graphs import (
    GET_A,
    META_CHAMA,
    META_SERVE,
    MISSING,
    PRESTACAO,
    build_chama,
    build_serve,
)
from cross_graphs import save as _save

from php_map.config import Settings
from php_map.errors import ConfigError, QueryError
from php_map.graph.builder import GraphBuilder
from php_map.mcp import tools
from php_map.mcp.catalog import ProjectCatalog
from php_map.search.index import build_documents, write_index

SERVE, CHAMA = "ecidade-t", "zend-t"
VIEW = "application/modules/fiscal/views/scripts/a.phtml"


def _indexed(tmp_path: Path, name: str, builder: GraphBuilder, meta: dict[str, str]) -> Path:
    db = _save(tmp_path, name, builder, meta)
    docs, stats = build_documents(lambda: [], builder, Settings())
    write_index(db, docs, stats)
    return db


@pytest.fixture
def dbs(tmp_path: Path) -> tuple[Path, Path]:
    return (
        _indexed(tmp_path, "serve", build_serve(), META_SERVE),
        _indexed(tmp_path, "chama", build_chama(), META_CHAMA),
    )


@pytest.fixture
def catalog(dbs: tuple[Path, Path]) -> ProjectCatalog:
    return ProjectCatalog.from_dbs(list(dbs))


# --------------------------------------------------------------------------- catálogo


def test_catalog_from_dbs_uses_meta_project(catalog: ProjectCatalog) -> None:
    assert [p.slug for p in catalog.projects] == [SERVE, CHAMA]
    with pytest.raises(QueryError, match="válidos: ecidade-t, zend-t"):
        catalog.get("outro")


def test_projects_file_resolves_relative_paths_and_ignores_bad_entries(
    dbs: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve, chama = dbs
    folder = tmp_path / "cfg"
    folder.mkdir()
    beside = folder / "g.db"
    beside.write_bytes(serve.read_bytes())
    monkeypatch.chdir(tmp_path)
    data = {
        "version": 1,
        "projects": [
            {"slug": "a", "name": "A", "profile": "ecidade", "db": "g.db"},  # ao lado do json
            # relativo ao diretório atual e com barra invertida, como a interface grava
            {
                "slug": "b",
                "profile": "zend1",
                "db": os.path.relpath(chama, tmp_path).replace("/", "\\"),
                "source": None,
                "dsn_env": "SEGUNDO_PROJETO_DSN",
            },
            {"slug": "c", "db": "nao-existe.db"},
            {"name": "sem slug", "db": "g.db"},
            "lixo",
        ],
    }
    path = folder / "projects.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    catalog = ProjectCatalog.from_projects_file(path)
    assert [(p.slug, p.name, p.profile, p.source) for p in catalog.projects] == [
        ("a", "A", "ecidade", None),
        ("b", "b", "zend1", None),
    ]


def test_projects_file_finds_paths_relative_to_an_ancestor_folder(
    dbs: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Como o `out/scan38/projects.json` real: o banco relativo à raiz do repositório, e o
    servidor subindo de outro diretório (o cliente MCP de outro projeto)."""
    repo = tmp_path / "repo"
    (repo / "out" / "scan").mkdir(parents=True)
    (repo / "out" / "scan" / "g.db").write_bytes(dbs[0].read_bytes())
    path = repo / "out" / "scan" / "projects.json"
    path.write_text(
        json.dumps({"projects": [{"slug": "e", "db": r"out\scan\g.db"}]}), encoding="utf-8"
    )
    elsewhere = tmp_path / "outro-projeto"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    found = ProjectCatalog.from_projects_file(path).get("e").db
    assert found.resolve() == (repo / "out" / "scan" / "g.db").resolve()


def test_catalog_without_valid_projects_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "projects.json"
    path.write_text('{"projects": [{"slug": "x", "db": "x.db"}]}', encoding="utf-8")
    with pytest.raises(ConfigError, match="nenhum projeto"):
        ProjectCatalog.from_projects_file(path)
    with pytest.raises(ConfigError, match="inexistente"):
        ProjectCatalog.from_projects_file(tmp_path / "nada.json")


def test_cross_is_cached_until_a_database_changes(
    catalog: ProjectCatalog, dbs: tuple[Path, Path]
) -> None:
    first = catalog.cross(3)
    assert catalog.cross(3) is first
    os.utime(dbs[0], ns=(1, 1))
    assert catalog.cross(3) is not first


# --------------------------------------------------------------------------- ferramentas


def test_list_projects(catalog: ProjectCatalog) -> None:
    items = tools.list_projects(catalog)["projects"]
    assert [(p["slug"], p["profile"], p["has_source"]) for p in items] == [
        (SERVE, "ecidade", False),
        (CHAMA, "zend1", False),
    ]
    assert all(p["nodes"] > 0 and p["edges"] > 0 for p in items)


def test_search_finds_endpoint_and_caps_limit(catalog: ProjectCatalog) -> None:
    result = tools.search(catalog, "getA", SERVE, limit=10_000)
    assert result["hits"][0]["node_id"] == GET_A
    assert len(result["hits"]) <= tools.MAX_SEARCH
    with pytest.raises(QueryError, match="projeto desconhecido"):
        tools.search(catalog, "getA", "outro")


def test_node_and_missing_node(catalog: ProjectCatalog) -> None:
    found = tools.node(catalog, "Table:public.t2", SERVE)
    assert found["label"] == "Table" and found["edges_in"] == {"UPDATES": 1}
    with pytest.raises(QueryError, match="nó inexistente"):
        tools.node(catalog, "Table:public.nada", SERVE)


def test_neighbors_direction_types_limit_and_truncated(catalog: ProjectCatalog) -> None:
    out = tools.neighbors(catalog, "Method:cl_x::run", SERVE, edge_types=["UPDATES"])
    assert [(e["dst"], e["type"]) for e in out["edges"]] == [("Table:public.t2", "UPDATES")]
    assert out["edges"][0]["evidence"]["path"] == "classes/x.php"
    incoming = tools.neighbors(catalog, "Method:cl_x::run", SERVE, direction="in", limit=1)
    assert incoming["total"] == 1 and not incoming["truncated"]
    every = tools.neighbors(catalog, "Method:cl_x::run", SERVE, limit=1)
    assert every["total"] > 1 and every["truncated"] and len(every["edges"]) == 1
    with pytest.raises(QueryError, match="direction"):
        tools.neighbors(catalog, "Method:cl_x::run", SERVE, direction="both")
    with pytest.raises(QueryError, match="nó inexistente"):
        tools.neighbors(catalog, "Method:nada::x", SERVE)


def test_trace_lists_remote_calls_and_accepts_file_id(catalog: ProjectCatalog) -> None:
    out = tools.trace(catalog, f"File:{VIEW}", CHAMA, mermaid=True)
    assert out["root"] == f"File:{VIEW}" and not out["truncated"]
    assert {c["endpoint"] for c in out["remote_calls"]} >= {GET_A, MISSING}
    assert out["mermaid"].startswith(("flowchart", "graph"))
    assert all(set(e) == {"src", "dst", "type", "source", "confidence"} for e in out["edges"])


def test_trace_max_nodes_truncates_and_rejects_non_files(catalog: ProjectCatalog) -> None:
    out = tools.trace(catalog, VIEW, CHAMA, max_nodes=1)
    assert out["truncated"] and len(out["nodes"]) == 1
    assert tools.trace(catalog, VIEW, CHAMA, max_nodes=10_000)["limits"]["max_nodes"] == 500
    with pytest.raises(QueryError, match="neighbors"):
        tools.trace(catalog, "Method:cl_x::run", SERVE)
    with pytest.raises(QueryError):
        tools.trace(catalog, "nao/existe.php", SERVE)


def test_impact_writers_come_from_sql_not_from_the_cut_walk(catalog: ProjectCatalog) -> None:
    out = tools.impact(catalog, "t2", SERVE, max_nodes=1)
    assert out["truncated"] or len(out["nodes"]) == 1
    assert out["writers"] == ["Method:cl_x::run"] and out["writers_total"] == 1
    readers = tools.impact(catalog, "Table:public.t1", SERVE)
    assert readers["readers"] == [GET_A] and readers["writers"] == []


def test_communication_and_endpoint_with_counterpart(catalog: ProjectCatalog) -> None:
    page = tools.communication(catalog, CHAMA, direction="sai", limit=1)
    assert page["total"] >= 2 and page["truncated"] and len(page["items"]) == 1
    assert page["cross_note"] is None and page["counters"]["cross_available"]
    detail = tools.endpoint(catalog, GET_A, CHAMA)
    assert detail["direction"] == "sai" and detail["callers_total"] >= 2
    other = detail["counterpart"]
    assert other["available"] and other["served_by"] == SERVE
    assert [p["name"] for p in other["params"]] == ["cnpj"]
    assert "Table:public.t1" in other["reaches"]["tables"]


def test_cross_summary_needs_two_projects(catalog: ProjectCatalog, dbs: tuple[Path, Path]) -> None:
    out = tools.cross_summary(catalog)
    assert out["counts"]["casado"] >= 3 and out["counts"]["chamado_sem_servidor"] == 1
    orphans = tools.cross_summary(catalog, status="chamado_sem_servidor")
    assert [e["endpoint_id"] for e in orphans["endpoints"]] == [MISSING]
    capped = tools.cross_summary(catalog, limit=1)
    assert capped["truncated"] and len(capped["endpoints"]) == 1
    assert PRESTACAO in {e["endpoint_id"] for e in out["endpoints"]}
    with pytest.raises(QueryError, match="status"):
        tools.cross_summary(catalog, status="todos")
    single = ProjectCatalog.from_dbs([dbs[0]])
    with pytest.raises(QueryError, match="pelo menos 2"):
        tools.cross_summary(single)
    assert tools.communication(single, SERVE)["cross_note"] is not None


# --------------------------------------------------------------------------- código-fonte


@pytest.fixture
def sourced(tmp_path: Path, dbs: tuple[Path, Path]) -> ProjectCatalog:
    root = tmp_path / "src-ecidade"
    files = {
        "classes/x.php": "\n".join(f"linha {i}" for i in range(1, 101)),
        "libs/db_conn.php": "$DB_SENHA = 'x';",
        "application/configs/application.ini": "senha = x",
        "config/database.php": "x",
        ".env": "X=1",
        ".env.local": "X=1",
        "keys/a.pem": "x",
        "a/b.PFX": "x",
        "vendor/pkg/a.php": "x",
        "node_modules/m/a.js": "x",
        "app.ini.dist": "x",
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="latin-1")
    zend = tmp_path / "src-zend"
    (zend / "v").mkdir(parents=True)
    (zend / "v" / "a.phtml").write_bytes("emissão da nota\n".encode())
    data = {
        "projects": [
            {"slug": "e", "profile": "ecidade", "db": str(dbs[0]), "source": str(root)},
            {"slug": "n", "profile": "zend1", "db": str(dbs[1]), "source": str(zend)},
            {"slug": "sem", "profile": "ecidade", "db": str(dbs[0])},
        ]
    }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return ProjectCatalog.from_projects_file(path)


def test_source_window_around_line(sourced: ProjectCatalog) -> None:
    out = tools.source(sourced, "e", "classes/x.php", line=50, context=2)
    assert out["lines"] == [
        "48: linha 48",
        "49: linha 49",
        "50: linha 50",
        "51: linha 51",
        "52: linha 52",
    ]
    assert out["truncated"] and out["total_lines"] == 100
    head = tools.source(sourced, "e", "classes/x.php", context=3)
    assert head["start_line"] == 1 and len(head["lines"]) == 6


def test_source_decodes_utf8_for_zend1(sourced: ProjectCatalog) -> None:
    assert tools.source(sourced, "n", "v/a.phtml")["lines"] == ["1: emissão da nota"]


@pytest.mark.parametrize(
    "path",
    [
        "libs/db_conn.php",
        "application/configs/application.ini",
        "config/database.php",
        ".env",
        ".env.local",
        "keys/a.pem",
        "a/b.PFX",
        "vendor/pkg/a.php",
        "node_modules/m/a.js",
        "app.ini.dist",
        "LIBS\\DB_CONN.PHP",
    ],
)
def test_source_blocks_configuration_and_secrets(sourced: ProjectCatalog, path: str) -> None:
    with pytest.raises(QueryError, match="bloqueado"):
        tools.source(sourced, "e", path)


@pytest.mark.parametrize("path", ["../fora.php", "/etc/passwd", "C:\\Windows\\win.ini"])
def test_source_refuses_paths_outside_the_folder(sourced: ProjectCatalog, path: str) -> None:
    with pytest.raises(QueryError):
        tools.source(sourced, "e", path)


def test_source_follows_links_and_blocks_the_real_target(
    sourced: ProjectCatalog, tmp_path: Path
) -> None:
    link = tmp_path / "src-ecidade" / "inocente.txt"
    try:
        link.symlink_to(tmp_path / "src-ecidade" / "libs" / "db_conn.php")
    except OSError:
        pytest.skip("sem permissão para criar link simbólico neste sistema")
    with pytest.raises(QueryError, match="bloqueado"):
        tools.source(sourced, "e", "inocente.txt")


def test_source_without_folder(sourced: ProjectCatalog, catalog: ProjectCatalog) -> None:
    for cat, slug in ((sourced, "sem"), (catalog, SERVE)):
        with pytest.raises(QueryError, match="fonte não disponível"):
            tools.source(cat, slug, "classes/x.php")


def test_compact_cuts_long_texts_and_lists() -> None:
    value = {"sql": "x" * 1000, "items": list(range(100)), "deep": {"a": {"b": {"c": {"d": {}}}}}}
    out = tools.compact(value)
    assert len(out["sql"]) == tools.MAX_TEXT + 1 and out["items"][-1] == "… +70"


# --------------------------------------------------------------------------- fronteira


FORBIDDEN = {
    "networkx",
    "php_map.web.app",
    "php_map.web.schema",
    "php_map.web.state",
    "php_map.web.jobs",
}


def test_mcp_package_never_loads_the_whole_graph() -> None:
    """Sem `store.load` nem NetworkX (o e-cidade passa de 1,9 GB carregado)."""
    package = Path(tools.__file__).parent
    for module in package.glob("*.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for item in ast.walk(tree):
            if isinstance(item, ast.Import):
                names = {alias.name for alias in item.names}
            elif isinstance(item, ast.ImportFrom):
                base = item.module or ""
                names = {base} | {f"{base}.{alias.name}" for alias in item.names}
            elif isinstance(item, ast.Attribute) and item.attr == "load":
                assert not (isinstance(item.value, ast.Name) and item.value.id == "store"), module
                continue
            else:
                continue
            assert not names & FORBIDDEN, f"{module.name} importa {names & FORBIDDEN}"
            assert not any(n.startswith("networkx") for n in names), module.name
