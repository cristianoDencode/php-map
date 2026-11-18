from pathlib import PureWindowsPath

import pytest

from php_map.errors import GraphContractError
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    NodeLabel,
    business_action_id,
    class_id,
    file_id,
    function_id,
    label_of,
    method_id,
    module_id,
    pgfunction_id,
    rpccall_id,
    table_id,
    validate_edge,
)

EVIDENCE = {"path": "emp4_anularempenho001.php", "line": 10}


def test_table_ids() -> None:
    assert table_id("EmpEmpenho") == "Table:public.empempenho"
    assert table_id("exemplo", "issqn") == "Table:issqn.exemplo"
    assert table_id("ISSQN.Exemplo") == "Table:issqn.exemplo"


def test_symbol_ids_are_lowercase() -> None:
    assert function_id("DB_Query") == "Function:db_query"
    assert class_id("\\Cl_EmpEmpenho") == "Class:cl_empempenho"
    assert method_id("db_utils", "getDao") == "Method:db_utils::getdao"
    assert rpccall_id("FC_Verifica_Lancamento") == "RpcCall:fc_verifica_lancamento"


def test_pgfunction_id_matches_gabarito() -> None:
    expected = "PgFunction:public.fc_verifica_lancamento(int4,date,int4,float8)"
    assert pgfunction_id(None, "fc_verifica_lancamento", ["int4", "date", "int4", "float8"]) == (
        expected
    )
    assert pgfunction_id("public", "fc_x", []) == "PgFunction:public.fc_x()"


def test_file_id_keeps_case_and_uses_slash() -> None:
    assert file_id("forms/db_frmEmp.php") == "File:forms/db_frmEmp.php"
    assert file_id(PureWindowsPath(r"libs\db_stdlib.php")) == "File:libs/db_stdlib.php"
    assert file_id("./x.php") == "File:x.php"


def test_module_id_normalizes_the_name() -> None:
    assert module_id("e-Social") == module_id("esocial") == "Module:esocial"
    assert module_id(" Tributário ") == "Module:tributario"
    assert module_id("Recursos Humanos") == "Module:recursoshumanos"
    with pytest.raises(GraphContractError):
        module_id(" - ")


def test_business_action_id() -> None:
    assert business_action_id("menu:9952") == "BusinessAction:menu:9952"
    assert label_of(business_action_id("menu:1")) is NodeLabel.BUSINESS_ACTION


def test_label_of() -> None:
    assert label_of("PgFunction:public.f()") is NodeLabel.PG_FUNCTION
    for bad in ("Tabela:x", "semprefixo", "File:"):
        with pytest.raises(GraphContractError):
            label_of(bad)


def _edge(
    source: EdgeSource, confidence: float, type_: EdgeType = EdgeType.CALLS, **evidence: object
) -> Edge:
    return Edge(
        "File:a.php",
        "Function:f",
        type_,
        source,
        confidence,
        evidence or EVIDENCE,
    )


@pytest.mark.parametrize(
    ("source", "confidence", "type_"),
    [
        (EdgeSource.STRING_REF, 1.0, EdgeType.REFERENCES),
        (EdgeSource.PHP_AST, 0.5, EdgeType.CALLS),
        (EdgeSource.SQL_AST, 0.5, EdgeType.QUERIES),
        (EdgeSource.SQL_REGEX, 0.8, EdgeType.QUERIES),
        (EdgeSource.PHP_AST_AMBIGUOUS, 1.0, EdgeType.CALLS),
        (EdgeSource.NAMING_RULE, 1.0, EdgeType.BELONGS_TO),
        (EdgeSource.ECIDADE_CATALOG, 0.99, EdgeType.BELONGS_TO),  # sempre 1.0
        (EdgeSource.ECIDADE_CATALOG, 0.5, EdgeType.IMPLEMENTS_ACTION),
        (EdgeSource.DOCTRINE_ANNOTATION, 0.9, EdgeType.MAPS_TO),  # só 1.0 ou 0.8 (convenção)
        (EdgeSource.DOCTRINE_ANNOTATION, 0.5, EdgeType.QUERIES),
        (EdgeSource.PHP_AST, 1.5, EdgeType.CALLS),
        (EdgeSource.HEURISTIC_AI, -0.1, EdgeType.CALLS_DYNAMIC),
        (EdgeSource.HEURISTIC_AI, float("nan"), EdgeType.CALLS_DYNAMIC),
        (EdgeSource.JS_URL, 1.0, EdgeType.CALLS_HTTP),  # só 0.9 ou 0.9/n
        (EdgeSource.JS_URL, 0.5, EdgeType.CALLS_HTTP),
        (EdgeSource.JS_URL, 0.0, EdgeType.CALLS_HTTP),
    ],
)
def test_invalid_confidence_is_rejected(
    source: EdgeSource, confidence: float, type_: EdgeType
) -> None:
    with pytest.raises(GraphContractError):
        validate_edge(_edge(source, confidence, type_))


@pytest.mark.parametrize(
    ("source", "confidence", "type_"),
    [
        (EdgeSource.PHP_AST, 1.0, EdgeType.CALLS),
        (EdgeSource.PHP_AST_AMBIGUOUS, 0.25, EdgeType.CALLS),
        (EdgeSource.STRING_REF, 0.9, EdgeType.REFERENCES),
        (EdgeSource.SQL_REGEX, 0.7, EdgeType.QUERIES),
        (EdgeSource.SQL_AST, 0.5, EdgeType.CALLS_RPC),  # sobrecargas: 1/n
        (EdgeSource.NAMING_RULE, 0.6, EdgeType.BELONGS_TO),
        (EdgeSource.ECIDADE_CATALOG, 1.0, EdgeType.BELONGS_TO),
        (EdgeSource.DOCTRINE_ANNOTATION, 1.0, EdgeType.MAPS_TO),  # @Table(name=)
        (EdgeSource.DOCTRINE_ANNOTATION, 0.8, EdgeType.MAPS_TO),  # tabela pelo nome da classe
        (EdgeSource.DOCTRINE_ANNOTATION, 0.8, EdgeType.QUERIES),  # DQL de entidade por convenção
        (EdgeSource.JS_URL, 0.9, EdgeType.CALLS_HTTP),  # uma rota
        (EdgeSource.JS_URL, 0.45, EdgeType.CALLS_HTTP),  # dois verbos
        (EdgeSource.JS_URL, 0.9 / 3, EdgeType.CALLS_HTTP),
    ],
)
def test_valid_confidence_is_accepted(
    source: EdgeSource, confidence: float, type_: EdgeType
) -> None:
    validate_edge(_edge(source, confidence, type_))


def test_evidence_is_required() -> None:
    with pytest.raises(GraphContractError, match="evidence"):
        validate_edge(Edge("File:a.php", "Function:f", EdgeType.CALLS, EdgeSource.PHP_AST, 1.0, {}))
    validate_edge(_edge(EdgeSource.HEURISTIC_AI, 0.4, EdgeType.CALLS_DYNAMIC, cache_key="abc"))


def test_edge_endpoints_must_be_node_ids() -> None:
    edge = Edge("a.php", "Function:f", EdgeType.CALLS, EdgeSource.PHP_AST, 1.0, EVIDENCE)
    with pytest.raises(GraphContractError, match="a.php"):
        validate_edge(edge)
