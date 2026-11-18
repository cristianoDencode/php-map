"""Fórmulas das funções da base e dossiê de cálculo (`analysis/formulas.py`).

O mapa de teste (`tests/formula_graphs.py`) reproduz em miniatura trechos da `fc_calcula` e da
`fc_juros`; as funções pequenas dos testes lexicais passam por `plain_db` (só o texto).
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from formula_graphs import (
    AMB_DATE,
    AMB_TEXT,
    ARRECAD,
    CALC,
    DELEGA_A,
    DELEGA_B,
    FUNCTIONS,
    INFLA,
    JUROS,
    MENU_TABRECJM,
    TABREC,
    TABRECJM,
    TRIGGER,
    build_db,
    plain_db,
    simple_ddl,
)

from php_map.analysis import formulas as F
from php_map.errors import QueryError


def _id(name: str, args: str = "") -> str:
    return f"PgFunction:public.{name}({args})"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(build_db(tmp_path))) as connection:
        yield connection


def _by_line(formulas: list[F.Formula]) -> dict[int, F.Formula]:
    return {f.line: f for f in formulas}


def _one(tmp_path: Path, body: str, declare: str = "", args: str = "integer") -> list[F.Formula]:
    """As fórmulas de uma função pequena (só o texto, sem tabelas)."""
    node = _id("fc_t", "int4")
    db = plain_db(tmp_path, {node: simple_ddl("fc_t", args, body, declare)})
    with closing(sqlite3.connect(db)) as connection:
        return F.extract_formulas(connection, node)


# --------------------------------------------------------------------------- o que é fórmula


def test_arithmetic_and_calc_calls_are_formulas_but_plain_assignments_are_not(
    conn: sqlite3.Connection,
) -> None:
    formulas = F.extract_formulas(conn, CALC)
    targets = {f.target for f in formulas}
    assert {"juro", "valor_hist", "correcao", "total", "RETURN"} <= targets
    # `total := 0`, `total := correcao`, `fator := dtvenc::numeric(20,10)`, `texto := 'a * b + c'`
    lines = {f.line for f in formulas if f.kind == "assign" and f.target in ("total", "fator")}
    assert lines == {35}, "só `total := fc_amb(...) + 1` é fórmula"


def test_the_five_kinds_are_found_with_line_and_target(conn: sqlite3.Connection) -> None:
    found = {(f.kind, f.target, f.line) for f in F.extract_formulas(conn, CALC)}
    assert ("assign", "juro", 27) in found
    assert ("select_into", "correcao", 31) in found
    assert ("update_set", "tmpcalc.vtotal", 32) in found
    assert ("insert_value", "tmpcalc.vtotal", 33) in found
    assert ("return", "RETURN", 36) in found


def test_multiline_formula_keeps_first_line_and_end_line(conn: sqlite3.Connection) -> None:
    juro = _by_line(F.extract_formulas(conn, CALC))[27]
    assert (juro.line, juro.end_line) == (27, 28)
    assert juro.expression.startswith("round((correcao + valor_receita)")
    assert "\n" not in juro.expression and "  " not in juro.expression


def test_operators_in_strings_and_comments_do_not_count(tmp_path: Path) -> None:
    formulas = _one(
        tmp_path,
        "  -- x := a * b - c\n  texto := 'a * b + c';\n  texto := 'a' || 'b';\n"
        "  /* y := 1 + 2 */\n  y := 5;",
        declare="  texto text;\n  y integer;\n",
    )
    assert formulas == []


def test_cast_is_not_arithmetic_but_a_calc_function_still_counts(tmp_path: Path) -> None:
    formulas = _one(
        tmp_path,
        "  a := b::numeric(20,10);\n  c := d::double precision;\n  e := round(f::numeric(8,2), 2);",
        declare="".join(f"  {name} numeric;\n" for name in "abcdef"),
    )
    assert [f.target for f in formulas] == ["e"]
    assert "::" not in formulas[0].readable, "o cast sai da forma legível"


def test_unary_minus_and_count_star_are_not_arithmetic(tmp_path: Path) -> None:
    formulas = _one(
        tmp_path,
        "  a := -1;\n  b := (-c);\n  select count(*) into n from t;\n  d := c - 1;",
        declare="  a integer;\n  b integer;\n  c integer;\n  d integer;\n  n integer;\n",
    )
    assert [f.target for f in formulas] == ["d"]


def test_case_expression_with_then_and_else_is_read_whole(tmp_path: Path) -> None:
    formulas = _one(
        tmp_path,
        "  if x > 0 then\n    v := case when x > 10 then x * 2 else x + 1 end;\n  end if;",
        declare="  x integer;\n  v integer;\n",
    )
    assert [(f.target, f.line) for f in formulas] == [("v", 10)]
    assert formulas[0].expression == "case when x > 10 then x * 2 else x + 1 end"


def test_select_into_with_case_is_one_formula(tmp_path: Path) -> None:
    formulas = _one(
        tmp_path,
        "  select case when a != 0 then a * 2 else b end\n    into v\n    from t where id = 1;",
        declare="  v integer;\n",
    )
    assert [(f.kind, f.target) for f in formulas] == [("select_into", "v")]


def test_alias_in_select_list_is_not_part_of_the_expression(tmp_path: Path) -> None:
    formulas = _one(
        tmp_path, "  select round(sum(a), 2) as total into v from t;", declare="  v numeric;\n"
    )
    assert formulas[0].expression == "round(sum(a), 2)"


# --------------------------------------------------------------------------- entradas


def test_loop_record_over_a_subquery_resolves_to_the_table_column(conn: sqlite3.Connection) -> None:
    """A consulta do laço não tem tabela resolvida no mapa (como na `fc_calcula`): a tabela vem
    do `FROM` da subconsulta."""
    juro = _by_line(F.extract_formulas(conn, CALC))[27]
    columns = {(i.name, i.table_id, i.column) for i in juro.inputs if i.table_id}
    assert ("valor_receita", ARRECAD, "k00_valor") in columns
    assert ("receita", ARRECAD, "k00_receit") in columns
    resolved = next(i for i in juro.inputs if i.name == "valor_receita")
    assert resolved.kind == "column" and resolved.via_line == 26
    assert "Valor [arrecad.k00_valor]" in juro.readable


def test_direct_record_field_keeps_its_name(tmp_path: Path) -> None:
    formulas = _one(
        tmp_path,
        "  for r in select k00_valor, k00_dtvenc from arrecad loop\n    v := v + r.k00_valor;\n"
        "  end loop;",
        declare="  v numeric;\n",
    )
    assert formulas[0].inputs[-1].kind == "record_field"
    assert formulas[0].inputs[-1].name == "r.k00_valor"


def test_accumulator_input_is_itself_and_does_not_loop(conn: sqlite3.Connection) -> None:
    acc = _by_line(F.extract_formulas(conn, CALC))[29]
    assert acc.target == "valor_hist"
    self_input = next(i for i in acc.inputs if i.name == "valor_hist")
    assert self_input.kind == "variable" and self_input.table_id is None


def test_parameters_and_variables_without_data_stay_what_they_are(conn: sqlite3.Connection) -> None:
    formulas = _by_line(F.extract_formulas(conn, CALC))
    assert {(i.kind, i.name) for i in formulas[35].inputs} >= {
        ("parameter", "numpre"),
        ("parameter", "dtvenc"),
    }
    assert ("variable", "correcao") in {(i.kind, i.name) for i in formulas[27].inputs}


def test_literal_initialisation_is_skipped_when_following_a_variable(tmp_path: Path) -> None:
    body = "  v := p;" + chr(10) + "  v := 0;" + chr(10) + "  w := v * 2;"
    declare = "  p alias for $1;" + chr(10) + "  v numeric;" + chr(10) + "  w numeric;" + chr(10)
    formulas = _one(tmp_path, body, declare=declare)
    assert [(i.kind, i.name) for i in formulas[-1].inputs] == [("parameter", "v")], (
        "`v := 0` só inicializa: o dado vem de `v := p`"
    )


def test_trigger_new_field_is_listed_as_not_traced(conn: sqlite3.Connection) -> None:
    formulas = F.extract_formulas(conn, TRIGGER)
    assert formulas[0].inputs[0].kind == "record_field"
    dossier = F.calc_dossier(conn, TRIGGER)
    assert any("NEW/OLD" in reason for _what, reason in dossier.untraced)


def test_update_and_insert_resolve_columns_and_variables(conn: sqlite3.Connection) -> None:
    formulas = _by_line(F.extract_formulas(conn, CALC))
    update = formulas[32]
    assert (TABRECJM, "k02_corr") in {(i.table_id, i.column) for i in update.inputs}
    insert = formulas[33]
    assert [(i.kind, i.name) for i in insert.inputs] == [("variable", "juro")]


# --------------------------------------------------------------------------- funções auxiliares


def test_called_function_becomes_a_function_input_with_its_node(conn: sqlite3.Connection) -> None:
    juro = _by_line(F.extract_formulas(conn, CALC))[27]
    called = next(i for i in juro.inputs if i.kind == "function")
    assert (called.name, called.routine_id) == ("fc_juros_t", JUROS)


def test_ambiguous_auxiliary_is_not_traced(conn: sqlite3.Connection) -> None:
    total = _by_line(F.extract_formulas(conn, CALC))[35]
    ambiguous = next(i for i in total.inputs if i.kind == "function")
    assert ambiguous.routine_id is None
    dossier = F.calc_dossier(conn, CALC)
    reasons = dict(dossier.untraced)
    assert "função ambígua" in reasons["fc_amb(...)"]
    assert AMB_DATE.partition(":")[2] in reasons["fc_amb(...)"]
    assert AMB_TEXT.partition(":")[2] in reasons["fc_amb(...)"]
    assert AMB_DATE not in dossier.functions


def _chain_db(tmp_path: Path, size: int, formulas_each: int = 1) -> tuple[Path, list[str]]:
    """`fc_c1` chama `fc_c2`, que chama `fc_c3`..., cada uma com `formulas_each` contas."""
    ids = [_id(f"fc_c{n}", "int4") for n in range(1, size + 1)]
    functions = {}
    for n, node in enumerate(ids, start=1):
        lines = [f"  x{k} := x{k} + {k};" for k in range(formulas_each)]
        if n < size:
            lines.append(f"  y := fc_c{n + 1}(x0) + 1;")
        decl = "  y numeric;\n" + "".join(f"  x{k} numeric;\n" for k in range(formulas_each))
        functions[node] = simple_ddl(f"fc_c{n}", "integer", "\n".join(lines), decl)
    return plain_db(tmp_path, functions, "cadeia"), ids


def test_depth_function_and_formula_limits_set_truncated(tmp_path: Path) -> None:
    db, ids = _chain_db(tmp_path, 5)
    with closing(sqlite3.connect(db)) as connection:
        full = F.calc_dossier(connection, ids[0], depth=4)
        assert full.functions == tuple(ids) and not full.truncated
        by_depth = F.calc_dossier(connection, ids[0], depth=2)
        assert by_depth.functions == tuple(ids[:3])
        assert by_depth.truncated and by_depth.functions_omitted == 1
        by_count = F.calc_dossier(connection, ids[0], depth=4, max_functions=2)
        assert by_count.functions == tuple(ids[:2]) and by_count.truncated
        by_formulas = F.calc_dossier(connection, ids[0], depth=4, max_formulas=3)
        assert len(by_formulas.formulas) == 3 and by_formulas.truncated
        assert by_formulas.formulas_total == 9


def test_formula_budget_is_shared_between_the_root_and_the_auxiliaries(tmp_path: Path) -> None:
    db, ids = _chain_db(tmp_path, 2, formulas_each=6)
    with closing(sqlite3.connect(db)) as connection:
        dossier = F.calc_dossier(connection, ids[0], max_formulas=6)
    owners = [f.routine_id for f in dossier.formulas]
    assert owners.count(ids[0]) == 4 and owners.count(ids[1]) == 2, "a raiz leva 60%"


# --------------------------------------------------------------------------- dossiê


def test_dossier_has_the_base_inputs_with_label_and_menu(conn: sqlite3.Connection) -> None:
    dossier = F.calc_dossier(conn, CALC)
    assert dossier.functions == (CALC, JUROS)
    by_key = {(b.table_id, b.column): b for b in dossier.inputs}
    corr = by_key[(TABRECJM, "k02_corr")]
    assert corr.label == "inflator para correcao"
    assert corr.menus == (MENU_TABRECJM,)
    assert corr.writers == ("Method:cl_tabrecjm::incluir",)
    assert corr.screens == ("File:cai1_recejm001.php",)
    assert (CALC, 27) in by_key[(ARRECAD, "k00_valor")].used_in
    assert (TABRECJM, "k02_juros") in by_key, "campo resolvido dentro da função auxiliar"
    assert (TABREC, "k02_codigo") in by_key, "campo de filtro que a auxiliar lê"


def test_untraced_has_the_three_reasons(conn: sqlite3.Connection) -> None:
    reasons = dict(F.calc_dossier(conn, CALC).untraced)
    assert reasons["caixa.arrecad"] == "tabela sem tela de cadastro no mapa"
    assert reasons["caixa.tabrec"].startswith("menu sem caminho")
    assert "--catalog-db" in reasons["caixa.tabrec"]
    assert reasons["inflatores.infla"].startswith("tela de cadastro sem item de menu")
    assert "inf1_infla001.php" in reasons["inflatores.infla"]
    assert "caixa.tabrecjm" not in reasons, "com menu e caminho não é lacuna"


def test_rendering_has_the_four_sections_and_the_cut_message(conn: sqlite3.Connection) -> None:
    dossier = F.calc_dossier(conn, CALC)
    text = F.render_calc_dossier(dossier)
    for heading in (
        "## Cálculos de public.fc_calc_total(int4,date)",
        "## Funções auxiliares",
        "## Dados de entrada e onde são cadastrados",
        "## Não rastreado",
    ):
        assert heading in text
    assert f"| `caixa.tabrecjm.k02_corr` | inflator para correcao | {MENU_TABRECJM} |" in text
    assert "linha 27: juro = round(" in text
    assert "cortado" not in text
    cut = F.render_calc_dossier(dossier, max_words=300)
    assert len(cut.split()) < len(text.split())
    assert "cortado: " in cut and f"de {len(dossier.formulas)} fórmulas" in cut


def test_rendering_says_when_the_dossier_hit_the_limits(tmp_path: Path) -> None:
    db, ids = _chain_db(tmp_path, 3)
    with closing(sqlite3.connect(db)) as connection:
        text = F.render_calc_dossier(F.calc_dossier(connection, ids[0], depth=1))
    assert "O dossiê bateu nos limites" in text and "1 função(ões) chamada(s)" in text


def test_json_has_the_same_content_as_the_dossier(conn: sqlite3.Connection) -> None:
    dossier = F.calc_dossier(conn, CALC)
    data = F.dossier_json(dossier)
    assert data["root"] == CALC and data["functions"] == [CALC, JUROS]
    assert len(data["formulas"]) == len(dossier.formulas)
    assert {"what": "caixa.tabrec", "reason": dict(dossier.untraced)["caixa.tabrec"]} in data[
        "untraced"
    ]
    json.dumps(data)  # serializável
    assert data["formulas"][0]["inputs"][0]["kind"] in {"column", "variable", "function"}


def test_dossier_does_not_release_a_secret(tmp_path: Path) -> None:
    node = _id("fc_t", "int4")
    ddl = simple_ddl(
        "fc_t",
        "integer",
        "  x := y * 2 + length('AKIAIOSFODNN7EXAMPLE');",
        "  x integer;\n  y integer;\n",
    )
    db = plain_db(tmp_path, {node: ddl})
    with closing(sqlite3.connect(db)) as connection:
        dossier = F.calc_dossier(connection, node)
        with pytest.raises(QueryError, match="segredo"):
            F.render_calc_dossier(dossier)


# --------------------------------------------------------------------------- erros


def test_unknown_function_is_a_clear_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(QueryError, match="função não encontrada: fc_nada"):
        F.calc_dossier(conn, "fc_nada")


def test_unknown_function_suggests_similar_names(conn: sqlite3.Connection) -> None:
    with pytest.raises(QueryError, match=r"Parecidas: public\.fc_calc_total"):
        F.resolve_routine(conn, "fc_calc")


def test_ambiguous_name_lists_the_signatures(conn: sqlite3.Connection) -> None:
    with pytest.raises(F.AmbiguousRoutineError) as error:
        F.resolve_routine(conn, "fc_amb")
    assert error.value.candidates == (AMB_DATE, AMB_TEXT)
    assert "public.fc_amb(int4,date)" in str(error.value)


def test_name_and_id_resolve_to_the_same_function(conn: sqlite3.Connection) -> None:
    assert F.resolve_routine(conn, "fc_juros_t") == JUROS
    assert F.resolve_routine(conn, JUROS) == JUROS
    assert F.resolve_routine(conn, "public.fc_juros_t") == JUROS
    # a assinatura sem o schema (como na linha de comando do aceite da tarefa)
    assert F.resolve_routine(conn, "fc_juros_t(int4, date, bool)") == JUROS
    assert F.resolve_routine(conn, "fc_amb(int4,text)") == AMB_TEXT
    with pytest.raises(QueryError, match="função não encontrada"):
        F.resolve_routine(conn, "fc_juros_t(int4)")


def test_map_without_function_text_says_how_to_fix_it(tmp_path: Path) -> None:
    from cross_graphs import save

    from php_map.graph.builder import GraphBuilder

    db = save(tmp_path, "sem-texto", GraphBuilder(), {"project": "x", "source": "x"})
    with closing(sqlite3.connect(db)) as connection, pytest.raises(QueryError, match="rode o scan"):
        F.calc_dossier(connection, "fc_calc_total")


def test_every_fixture_function_has_text() -> None:
    assert set(FUNCTIONS) == {CALC, JUROS, AMB_DATE, AMB_TEXT, TRIGGER, DELEGA_A, DELEGA_B}
    assert INFLA.startswith("Table:")


# --------------------------------------------------------------------------- repasse


def test_a_name_that_only_delegates_resolves_to_the_destination(conn: sqlite3.Connection) -> None:
    """`fc_delega` tem 2 assinaturas; a de 1 argumento só repassa (`CALLS_RPC` interna) para a
    de 2, que faz a conta de verdade."""
    assert F.resolve_routine(conn, "fc_delega") == DELEGA_B
    # pedir a assinatura exata que só repassa continua indo nela (sem repasse)
    assert F.resolve_routine(conn, DELEGA_A) == DELEGA_A
    assert F.resolve_routine(conn, DELEGA_B) == DELEGA_B


def test_calc_dossier_marks_how_the_root_was_resolved(conn: sqlite3.Connection) -> None:
    dossier = F.calc_dossier(conn, "fc_delega")
    assert dossier.root == DELEGA_B
    assert dossier.resolved_by == "repasse"
    assert dossier.resolved_chain == (DELEGA_A, DELEGA_B)
    assert dossier.resolved_others == (DELEGA_A,)
    # função resolvida direto (sem repasse): os campos ficam no padrão
    direct = F.calc_dossier(conn, JUROS)
    assert direct.resolved_by == "unico"
    assert direct.resolved_chain == () and direct.resolved_others == ()


def test_render_calc_dossier_opens_with_the_delegation_note(conn: sqlite3.Connection) -> None:
    dossier = F.calc_dossier(conn, "fc_delega")
    markdown = F.render_calc_dossier(dossier)
    assert "`fc_delega` tem 2 assinaturas" in markdown
    assert "a de 1 argumento só repassa para esta" in markdown
    assert DELEGA_B.partition(":")[2] in markdown
    direct = F.render_calc_dossier(F.calc_dossier(conn, JUROS))
    assert "só repassa para esta" not in direct


def test_dossier_json_carries_the_resolution(conn: sqlite3.Connection) -> None:
    data = F.dossier_json(F.calc_dossier(conn, "fc_delega"))
    assert data["resolved_by"] == "repasse"
    assert data["resolved_chain"] == [DELEGA_A, DELEGA_B]
    assert data["resolved_others"] == [DELEGA_A]


# --------------------------------------------------------------------------- corte repartido (72)


def _big_dossier() -> F.CalcDossier:
    root = "PgFunction:public.fc_grande(int4)"
    formulas = tuple(
        F.Formula(
            root, f"v{n}", f"a{n} * b{n} + c{n}", n, n, "assign", (),
            f"Valor {n} [t.a{n}] × Taxa {n} [t.b{n}] + Fator {n} [t.c{n}] arredondado", (),
        )
        for n in range(1, 61)
    )  # fmt: skip
    inputs = tuple(
        F.BaseInput(
            "Table:caixa.t", f"c{n}", f"campo {n}", None, (), (), (),
            () if n <= 150 else ("Arrecadação > Cadastros > Tabela",),
        )
        for n in range(1, 201)
    )  # fmt: skip
    untraced = tuple((f"caixa.t.c{n}", "tabela sem tela de cadastro no mapa") for n in range(1, 41))
    return F.CalcDossier(root, (root,), formulas, inputs, untraced, False, 60)


def test_the_cut_shares_the_room_between_formulas_inputs_and_untraced() -> None:
    dossier = _big_dossier()
    whole = F.render_calc_dossier(dossier, max_words=100_000)
    assert "cortado" not in whole and whole.count("- linha") == 60
    text = F.render_calc_dossier(dossier, max_words=1200)
    assert len(text.split()) <= 1200
    assert 0 < text.count("- linha") < 60, "as fórmulas não somem nem ficam todas"
    assert text.count("| `caixa.t.c") > 0, "os campos de entrada também ficam"
    for what in ("fórmulas", "campos"):
        assert f"de {60 if what == 'fórmulas' else 200} {what})_" in text
    shown = text.count("| `caixa.t.c")
    assert shown < 200 and "(cortado: " in text


def test_the_inputs_with_a_menu_come_first_so_the_cut_drops_the_ones_without() -> None:
    text = F.render_calc_dossier(_big_dossier(), max_words=1200)
    rows = [line for line in text.splitlines() if line.startswith("| `caixa.t.c")]
    assert rows and "Arrecadação > Cadastros > Tabela" in rows[0]
    assert all("ver não rastreado" not in row for row in rows[:50]), "50 com menu na frente"


def test_a_small_dossier_is_never_cut_by_the_shared_cut() -> None:
    dossier = _big_dossier()
    small = F.CalcDossier(
        dossier.root, dossier.functions, dossier.formulas[:3], dossier.inputs[:3],
        dossier.untraced[:2], False, 3,
    )  # fmt: skip
    text = F.render_calc_dossier(small, max_words=3000)
    assert "cortado" not in text and text.count("- linha") == 3
