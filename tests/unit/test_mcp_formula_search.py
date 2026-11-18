"""Ferramenta MCP `formula_search` (`mcp/tools.py`)."""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from php_map.errors import QueryError
from php_map.graph import formula_index, routines, store
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Node, NodeLabel
from php_map.mcp import tools
from php_map.mcp.catalog import ProjectCatalog

PROJECT = "formulas-f"
READ_AT = "2026-11-09T10:00:00-03:00"

FC1 = "PgFunction:public.fc_desc1()"  # topic desconto, por nome; módulo iptu
FC2 = "PgFunction:public.fc_desc2()"  # topic desconto, por nome; módulo issqn
FC3 = "PgFunction:public.fc_desc3()"  # topic desconto, por nome; sem módulo
FC_LABEL = "PgFunction:public.fc_label()"  # topic correcao, pelo rótulo do dicionário
FC_MULTA = "PgFunction:public.fc_multa_fn()"  # topic multa, pela chamada a fc_multa
TX = "Table:public.tx"

FUNCTIONS = {
    FC1: "fc_desc1",
    FC2: "fc_desc2",
    FC3: "fc_desc3",
    FC_LABEL: "fc_label",
    FC_MULTA: "fc_multa_fn",
}


def _ddl(name: str, body: str) -> str:
    return (
        f"CREATE OR REPLACE FUNCTION public.{name}()\n"
        " RETURNS void\n LANGUAGE plpgsql\nAS $function$\nbegin\n"
        f"  {body}\nend;\n$function$\n"
    )


DDLS = {
    FC1: _ddl("fc_desc1", "update tx set desc1 = desc1 + 1 where 1 = 1;"),
    FC2: _ddl("fc_desc2", "update tx set desc2 = desc2 + 1 where 1 = 1;"),
    FC3: _ddl("fc_desc3", "update tx set desc3 = desc3 + 1 where 1 = 1;"),
    FC_LABEL: _ddl("fc_label", "update tx set valy = valy + 1 where 1 = 1;"),
    FC_MULTA: _ddl("fc_multa_fn", "return fc_multa(1) + 1;"),
}
# (routine_id, seq, kind, line, text do UPDATE) — a linha do `update` em cada DDL (sempre a 5ª)
UPDATE_TARGETS = {FC1: "desc1", FC2: "desc2", FC3: "desc3", FC_LABEL: "valy"}


def _builder() -> GraphBuilder:
    b = GraphBuilder()
    for node_id, name in FUNCTIONS.items():
        b.add_node(
            Node(node_id, NodeLabel.PG_FUNCTION, {"name": name, "schema": "public", "args": []})
        )
    b.add_node(
        Node(
            TX,
            NodeLabel.TABLE,
            {
                "name": "tx",
                "schema": "public",
                "columns": ["desc1", "desc2", "desc3", "valy"],
                "columns_catalog": {"valy": {"label": "Ajuste de correção monetária"}},
            },
        )
    )
    return b


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / PROJECT / "graph.db"
    store.save(_builder(), path, {"project": PROJECT, "source": "x"})
    rows = [
        routines.make_source(node_id, "public", name, "plpgsql", DDLS[node_id], READ_AT)
        for node_id, name in FUNCTIONS.items()
    ]
    routines.write_routines(path, rows)
    routines.write_rules(path, [])  # só o esquema de `routine_rules` (nenhuma função tem RAISE)
    routines.write_fields(path, [])  # só o esquema de `routine_queries`
    tables = json.dumps([TX], ensure_ascii=False)
    with sqlite3.connect(path) as conn, conn:
        conn.executemany(
            "INSERT INTO routine_queries "
            "(routine_id, seq, kind, line, end_line, text, tables, source, confidence) "
            "VALUES (?, 0, 'update', 5, 5, ?, ?, 'SQL_AST', 1.0)",
            [
                (node_id, f"update tx set {col} = {col} + 1 where 1 = 1", tables)
                for node_id, col in UPDATE_TARGETS.items()
            ],
        )
        # módulo das funções: só `analytics_routines.module` importa aqui.
        conn.execute("CREATE TABLE analytics_routines (routine_id TEXT PRIMARY KEY, module TEXT)")
        conn.executemany(
            "INSERT INTO analytics_routines VALUES (?, ?)",
            [(FC1, "Module:iptu"), (FC2, "Module:issqn")],
        )
    with sqlite3.connect(path) as conn:
        formula_index.build_formula_index(conn)
    yield path


@pytest.fixture
def catalog(db: Path) -> ProjectCatalog:
    return ProjectCatalog.from_dbs([db])


def test_requires_query_or_topic(catalog: ProjectCatalog) -> None:
    with pytest.raises(QueryError, match="query ou topic"):
        tools.formula_search(catalog, PROJECT)


def test_invalid_group_by(catalog: ProjectCatalog) -> None:
    with pytest.raises(QueryError, match="group_by"):
        tools.formula_search(catalog, PROJECT, topic="desconto", group_by="tabela")


def test_topic_by_key(catalog: ProjectCatalog) -> None:
    result = tools.formula_search(catalog, PROJECT, topic="desconto")
    assert result["total"] == 3
    assert {i["routine_id"] for i in result["items"]} == {FC1, FC2, FC3}
    assert tools.formula_index_module.WARNING in result["markdown"]


def test_topic_by_label(catalog: ProjectCatalog) -> None:
    result = tools.formula_search(catalog, PROJECT, topic="correção monetária")
    assert result["total"] == 1
    assert result["items"][0]["routine_id"] == FC_LABEL


def test_unknown_topic_raises(catalog: ProjectCatalog) -> None:
    with pytest.raises(QueryError, match="assunto desconhecido"):
        tools.formula_search(catalog, PROJECT, topic="inexistente")


def test_module_filter(catalog: ProjectCatalog) -> None:
    result = tools.formula_search(catalog, PROJECT, topic="desconto", module="Module:iptu")
    assert result["total"] == 1 and result["items"][0]["routine_id"] == FC1


def test_query_matches_column_label(catalog: ProjectCatalog) -> None:
    result = tools.formula_search(catalog, PROJECT, query="correção", group_by="formula")
    assert result["total"] == 1
    assert result["items"][0]["routine_id"] == FC_LABEL


def test_routine_limits_to_one_function(catalog: ProjectCatalog) -> None:
    result = tools.formula_search(
        catalog, PROJECT, topic="multa", routine="fc_multa_fn", group_by="formula"
    )
    assert result["total"] == 1
    assert result["items"][0]["routine_id"] == FC_MULTA
    assert result["items"][0]["calls"] == ["fc_multa"]


def test_routine_not_found(catalog: ProjectCatalog) -> None:
    with pytest.raises(QueryError, match="função inexistente"):
        tools.formula_search(catalog, PROJECT, topic="desconto", routine="fc_nao_existe")


def test_group_by_routine_pagination_by_offset(catalog: ProjectCatalog) -> None:
    page1 = tools.formula_search(catalog, PROJECT, topic="desconto", limit=2, offset=0)
    assert page1["total"] == 3 and len(page1["items"]) == 2
    assert page1["truncated"] is True and page1["next_offset"] == 2

    page2 = tools.formula_search(catalog, PROJECT, topic="desconto", limit=2, offset=2)
    assert len(page2["items"]) == 1
    assert page2["truncated"] is False and page2["next_offset"] is None

    seen = {i["routine_id"] for i in page1["items"]} | {i["routine_id"] for i in page2["items"]}
    assert seen == {FC1, FC2, FC3}


def test_group_by_formula_returns_formula_rows(catalog: ProjectCatalog) -> None:
    result = tools.formula_search(catalog, PROJECT, topic="desconto", group_by="formula")
    assert result["group_by"] == "formula"
    assert result["total"] == 3
    targets = {item["target"] for item in result["items"]}
    assert targets == {"tx.desc1", "tx.desc2", "tx.desc3"}


def test_list_projects_reports_formula_index(catalog: ProjectCatalog) -> None:
    items = tools.list_projects(catalog)["projects"]
    assert items[0]["slug"] == PROJECT and items[0]["formula_index"] is True


def test_without_the_index_raises_with_the_command(tmp_path: Path) -> None:
    path = tmp_path / "sem-indice" / "graph.db"
    store.save(_builder(), path, {"project": "sem-indice", "source": "x"})
    rows = [
        routines.make_source(node_id, "public", name, "plpgsql", DDLS[node_id], READ_AT)
        for node_id, name in FUNCTIONS.items()
    ]
    routines.write_routines(path, rows)
    without_index = ProjectCatalog.from_dbs([path])
    assert tools.list_projects(without_index)["projects"][0]["formula_index"] is False
    with pytest.raises(QueryError, match="php-map index-formulas"):
        tools.formula_search(without_index, "sem-indice", topic="desconto")


def test_routine_rules_topic_parameter(catalog: ProjectCatalog) -> None:
    """`routine_rules(topic=...)`: resolve chave/rótulo e devolve `topic` em cada
    item (aqui sempre `None`: as funções de teste não têm `RAISE` nenhuma)."""
    found = tools.routine_rules(catalog, PROJECT, topic="multa")
    assert found["total"] == 0  # nenhuma regra de verdade nesta fixture, mas não dá erro
    with pytest.raises(QueryError, match="assunto desconhecido"):
        tools.routine_rules(catalog, PROJECT, topic="nao-existe")
