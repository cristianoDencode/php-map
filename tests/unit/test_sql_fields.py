"""Campos e consultas por instrução SQL (`ingest/sql_fields.py`)."""

from php_map.ingest.sql_fields import FieldUse, analyze_statement
from php_map.ingest.table_directory import TableDirectory

T1 = "Table:public.empempenho"
T2 = "Table:public.empanulado"


def _directory() -> TableDirectory:
    d = TableDirectory()
    d.add("public", "empempenho", columns=("e60_numemp", "e60_vlranu", "e60_instit"))
    d.add("public", "empanulado", columns=("e94_codanu", "e94_numemp", "e94_valor"))
    return d


def _by_role(fields: tuple[FieldUse, ...], role: str) -> set[tuple[str, str]]:
    return {(f.table, f.column) for f in fields if f.role == role}


def test_select_read_filter_join_sort() -> None:
    sql = (
        "select e.e60_numemp, a.e94_valor from empempenho e join empanulado a "
        "on e.e60_numemp = a.e94_numemp where e.e60_instit = 1 order by e.e60_numemp"
    )
    facts = analyze_statement(sql, directory=_directory())
    assert facts.kind == "select"
    assert set(facts.tables) == {T1, T2}
    assert _by_role(facts.fields, "read") == {(T1, "e60_numemp"), (T2, "e94_valor")}
    assert _by_role(facts.fields, "filter") == {(T1, "e60_instit")}
    assert _by_role(facts.fields, "join") == {(T1, "e60_numemp"), (T2, "e94_numemp")}
    assert _by_role(facts.fields, "sort") == {(T1, "e60_numemp")}


def test_insert_with_column_list_is_write() -> None:
    sql = "insert into empanulado (e94_codanu, e94_valor) values (1, 2.0)"
    facts = analyze_statement(sql, directory=_directory())
    assert facts.kind == "insert"
    assert _by_role(facts.fields, "write") == {(T2, "e94_codanu"), (T2, "e94_valor")}


def test_insert_without_column_list_is_write_star() -> None:
    sql = "insert into empanulado values (1, 2, 3.0)"
    facts = analyze_statement(sql, directory=_directory())
    assert facts.fields == (FieldUse(T2, "*", "write", 1.0),)


def test_insert_select_has_read_and_write() -> None:
    sql = "insert into empanulado (e94_codanu) select e60_numemp from empempenho"
    facts = analyze_statement(sql, directory=_directory())
    assert _by_role(facts.fields, "write") == {(T2, "e94_codanu")}
    assert _by_role(facts.fields, "read") == {(T1, "e60_numemp")}


def test_update_set_is_write_and_value_expression_is_read() -> None:
    sql = "update empempenho set e60_vlranu = e60_vlranu + 1 where e60_numemp = 1"
    facts = analyze_statement(sql, directory=_directory())
    assert facts.kind == "update"
    assert _by_role(facts.fields, "write") == {(T1, "e60_vlranu")}
    assert _by_role(facts.fields, "read") == {(T1, "e60_vlranu")}
    assert _by_role(facts.fields, "filter") == {(T1, "e60_numemp")}


def test_update_from_has_extra_table_as_filter() -> None:
    sql = (
        "update empempenho set e60_vlranu = a.e94_valor from empanulado a "
        "where empempenho.e60_numemp = a.e94_numemp"
    )
    facts = analyze_statement(sql, directory=_directory())
    assert set(facts.tables) == {T1, T2}
    assert _by_role(facts.fields, "filter") == {(T1, "e60_numemp"), (T2, "e94_numemp")}


def test_delete_using_has_extra_table() -> None:
    sql = (
        "delete from empanulado using empempenho "
        "where empanulado.e94_numemp = empempenho.e60_numemp"
    )
    facts = analyze_statement(sql, directory=_directory())
    assert facts.kind == "delete"
    assert set(facts.tables) == {T2, T1}
    assert _by_role(facts.fields, "filter") == {(T2, "e94_numemp"), (T1, "e60_numemp")}


def test_cte_passthrough_resolves_to_the_real_table() -> None:
    sql = "with x as (select * from empempenho) select x.e60_numemp from x"
    facts = analyze_statement(sql, directory=_directory())
    assert _by_role(facts.fields, "read") == {(T1, "e60_numemp")}


def test_cte_that_is_not_a_passthrough_has_no_table() -> None:
    sql = "with x as (select e60_numemp + 1 as n from empempenho) select x.n from x"
    facts = analyze_statement(sql, directory=_directory())
    assert facts.unresolved == ("x.n",)


def test_union_each_branch_has_its_own_scope() -> None:
    sql = "select e60_numemp from empempenho union select e94_numemp from empanulado"
    facts = analyze_statement(sql, directory=_directory())
    assert set(facts.tables) == {T1, T2}
    assert _by_role(facts.fields, "read") == {(T1, "e60_numemp"), (T2, "e94_numemp")}


def test_unqualified_column_ambiguous_across_two_tables() -> None:
    directory = TableDirectory()
    directory.add("public", "t1", columns=("a", "b"))
    directory.add("public", "t2", columns=("a", "c"))
    sql = "select a from t1, t2"
    facts = analyze_statement(sql, directory=directory)
    fields = [f for f in facts.fields if f.column == "a"]
    assert {f.table for f in fields} == {"Table:public.t1", "Table:public.t2"}
    assert all(f.confidence == 0.5 for f in fields)


def test_unqualified_column_unique_has_full_confidence() -> None:
    directory = TableDirectory()
    directory.add("public", "t1", columns=("a", "b"))
    directory.add("public", "t2", columns=("c",))
    sql = "select a from t1, t2"
    facts = analyze_statement(sql, directory=directory)
    assert facts.fields == (FieldUse("Table:public.t1", "a", "read", 1.0),)


def test_unqualified_column_not_found_is_unresolved() -> None:
    sql = "select nao_existe from empempenho"
    facts = analyze_statement(sql, directory=_directory())
    assert facts.fields == ()
    assert facts.unresolved == ("nao_existe",)


def test_variable_homonym_of_a_column_is_not_a_field() -> None:
    sql = "select valor from empempenho"
    directory = TableDirectory()
    directory.add("public", "empempenho", columns=("valor",))
    facts = analyze_statement(sql, directory=directory, variables=("valor",))
    assert facts.fields == ()
    assert facts.unresolved == ()


def test_star_and_qualified_star() -> None:
    facts_bare = analyze_statement("select * from empempenho", directory=_directory())
    assert facts_bare.fields == (FieldUse(T1, "*", "read", 1.0),)
    facts_qualified = analyze_statement("select e.* from empempenho e", directory=_directory())
    assert facts_qualified.fields == (FieldUse(T1, "*", "read", 1.0),)


def test_new_and_old_resolve_to_the_trigger_table() -> None:
    sql = "select 1 where new.e60_vlranu > old.e60_vlranu"
    facts = analyze_statement(sql, directory=_directory(), trigger_tables=(T1,))
    assert _by_role(facts.fields, "filter") == {(T1, "e60_vlranu")}


def test_new_old_with_two_trigger_tables_splits_confidence() -> None:
    sql = "select new.x"
    facts = analyze_statement(sql, directory=_directory(), trigger_tables=(T1, T2))
    assert {(f.table, f.confidence) for f in facts.fields} == {(T1, 0.5), (T2, 0.5)}


def test_column_inside_function_call_is_still_read() -> None:
    sql = "select sum(e60_vlranu), coalesce(e60_instit, 0) from empempenho"
    facts = analyze_statement(sql, directory=_directory())
    assert _by_role(facts.fields, "read") == {(T1, "e60_vlranu"), (T1, "e60_instit")}


def test_parse_failure_falls_back_to_dynamic() -> None:
    facts = analyze_statement("select * from where (((", directory=_directory())
    assert facts.kind == "dynamic"
    assert facts.tables == () and facts.fields == () and facts.unresolved == ()


def test_parse_failure_with_a_table_name_falls_back_to_regex() -> None:
    # Concatenação inválida para o pglast, mas com "from empempenho" reconhecível por regex.
    sql = "select {{VAR}} from empempenho where x = {{VAR}}"
    facts = analyze_statement(sql, directory=_directory())
    assert facts.kind == "parse_fallback"
    assert facts.source == "SQL_REGEX" and facts.confidence == 0.5
    assert facts.tables == (T1,)
    assert facts.fields == () and facts.unresolved == ()


def test_statement_facts_default_source_is_sql_ast() -> None:
    facts = analyze_statement("select 1 from empempenho", directory=_directory())
    assert facts.source == "SQL_AST" and facts.confidence == 1.0


def test_pure_function_has_no_side_effects_or_db_access() -> None:
    # Só precisa de um TableDirectory em memória; não importa banco nem I/O.
    directory = TableDirectory()
    directory.add("public", "x", columns=("a",))
    facts = analyze_statement("select a from x", directory=directory)
    assert facts.fields == (FieldUse("Table:public.x", "a", "read", 1.0),)


def test_text_is_normalized_and_truncated() -> None:
    sql = "select   1\n\nfrom   empempenho"
    facts = analyze_statement(sql, directory=_directory())
    assert facts.text == "select 1 from empempenho"
