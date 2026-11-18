"""Ferramenta MCP `value_origin` ("de onde vem este valor?")."""

from pathlib import Path

import pytest
from test_value_origin import REP_A, REP_B, TABELA_A, TMP_CALC, _db, delegated_db

from php_map.errors import QueryError
from php_map.mcp import tools
from php_map.mcp.catalog import ProjectCatalog


@pytest.fixture
def catalog(tmp_path: Path) -> ProjectCatalog:
    return ProjectCatalog.from_dbs([_db(tmp_path)])


def test_value_origin_without_targets_lists_candidates(catalog: ProjectCatalog) -> None:
    result = tools.value_origin(catalog, "g", "valor m2 do item", "fc_root")
    assert result["function"] is not None
    assert result["reach"] == 2
    assert result["candidates"]
    assert "coluna" in result["next"] or "targets" in result["next"]


def test_value_origin_with_targets_returns_the_dossier(catalog: ProjectCatalog) -> None:
    result = tools.value_origin(
        catalog, "g", "valor m2 do item", "fc_root", targets=[f"{TMP_CALC}#resultado"]
    )
    assert "dossie" in result or "dossier" in result
    assert "cadastro encontrado" in result["dossier"]
    assert result["words"] > 0


def test_value_origin_unknown_function_raises(catalog: ProjectCatalog) -> None:
    with pytest.raises(QueryError, match="função não encontrada"):
        tools.value_origin(catalog, "g", "x", "fc_fantasma")


def test_value_origin_invalid_target_is_a_warning(catalog: ProjectCatalog) -> None:
    result = tools.value_origin(
        catalog, "g", "valor m2 do item", "fc_root", targets=["nao e um alvo"]
    )
    assert result["warnings"]


def test_value_origin_direct_column_shows_hypothesis(catalog: ProjectCatalog) -> None:
    result = tools.value_origin(
        catalog, "g", "valor m2 do item", "fc_root", targets=[f"{TABELA_A}#valor"]
    )
    assert "hipótese" in result["dossier"]


def test_value_origin_delegated_name_resolves_to_the_destination(tmp_path: Path) -> None:
    """`fc_rep` (1 arg) só repassa para a de 2 (`CALLS_RPC` interna à família); o
    alcance usa o destino, que faz o trabalho de verdade."""
    catalog = ProjectCatalog.from_dbs([delegated_db(tmp_path)])
    result = tools.value_origin(catalog, "rep", "x", "fc_rep")
    assert result["function"] == REP_B
    direct = tools.value_origin(catalog, "rep", "x", REP_A)
    assert direct["function"] == REP_A
