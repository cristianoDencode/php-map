import pytest
from pglast.parser import ParseError

from php_map.graph.model import EdgeType
from php_map.sqlutil import (
    FuncCallRef,
    TableOp,
    analyze_sql,
    fitting_overloads,
    is_sequence_name,
)

Q, INS, U, D = EdgeType.QUERIES, EdgeType.INSERTS, EdgeType.UPDATES, EdgeType.DELETES


def test_select_into_variable_is_not_a_table() -> None:
    assert analyze_sql("select e54_instit into v_instit from empautoriza") == [
        TableOp(Q, "empautoriza")
    ]


def test_cte_names_are_excluded_and_schema_kept() -> None:
    refs = analyze_sql(
        "with x as (select * from a) insert into b select * from x join issqn.c on true"
    )
    assert set(refs) == {TableOp(INS, "b"), TableOp(Q, "a"), TableOp(Q, "c", "issqn")}


def test_mutation_targets_and_other_relations() -> None:
    assert set(analyze_sql("update t set v = 1 from u where t.id = u.id")) == {
        TableOp(U, "t"),
        TableOp(Q, "u"),
    }
    assert set(analyze_sql("delete from t using u where t.id = u.id")) == {
        TableOp(D, "t"),
        TableOp(Q, "u"),
    }
    assert analyze_sql("truncate t5") == [TableOp(D, "t5")]


def test_function_calls_with_schema_and_case() -> None:
    refs = analyze_sql("select FC_Verifica(1), public.fc_x(), nextval('s')")
    assert refs == [
        FuncCallRef("fc_verifica", nargs=1),
        FuncCallRef("fc_x", "public", nargs=0),
        FuncCallRef("nextval", nargs=1),
    ]


def test_repeated_references_are_unique() -> None:
    assert analyze_sql("select * from a join a b on true") == [TableOp(Q, "a")]


def test_invalid_sql_raises() -> None:
    with pytest.raises(ParseError):
        analyze_sql("select from where")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("empempenho_e60_numemp_seq", True),
        ("public.cancdebitos_k20_codigo_seq", True),
        ("X_SEQ", True),
        ("empempenho", False),
        ("seq_tabela", False),
        ("sequencia", False),
    ],
)
def test_is_sequence_name(name: str, expected: bool) -> None:
    assert is_sequence_name(name) is expected


def test_fitting_overloads_by_arity() -> None:
    arity = {"a(text)": (1, 1.0), "b(text,bool)": (2, 2.0), "c(variadic)": (0, float("inf"))}
    ids = sorted(arity)
    assert fitting_overloads(ids, arity, 2) == ["b(text,bool)", "c(variadic)"]
    assert fitting_overloads(ids, arity, None) == ids
    assert fitting_overloads(["a(text)", "b(text,bool)"], arity, 5) == ["a(text)", "b(text,bool)"]
    assert fitting_overloads(["a(text)"], arity, 9) == ["a(text)"]
