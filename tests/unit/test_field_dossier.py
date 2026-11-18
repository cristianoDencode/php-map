"""Dossiê de um campo (`analysis/formulas.field_dossier`) sobre `tests/field_graphs.py`.

Cobre: cadastro (menu e classe `cl_x`), fórmula da escrita, cópia direta, `select *`, corte em 10
funções com `more_writers`, coluna sem rótulo, "não rastreado" e os erros de tabela/coluna.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from field_graphs import (
    ARRECAD,
    ATUALIZA,
    CALC,
    COPIA,
    GRAVA,
    IPTUBASE,
    JUROS,
    LACO,
    LE_TUDO,
    LEITOR,
    MENU_IPTUBASE,
    TABRECJM,
    TMPCALC,
    build_db,
)
from formula_graphs import MENU_TABRECJM

from php_map.analysis import formulas as F
from php_map.errors import QueryError


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(build_db(tmp_path))) as connection:
        yield connection


@pytest.fixture
def conn26(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(build_db(tmp_path, "muitos", writers=26))) as connection:
        yield connection


def _write(dossier: F.FieldDossier, routine: str) -> F.FieldWrite:
    return next(w for w in dossier.computed_by if w.routine_id == routine)


# --------------------------------------------------------------------------- resolver o campo


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("caixa.arrecad.k00_valor", None),
        ("Table:caixa.arrecad", "k00_valor"),
        ("Table:caixa.arrecad#k00_valor", None),
        ("arrecad.k00_valor", None),
        ("caixa.arrecad", "K00_VALOR"),
        ("arrecad", "k00_valor"),
    ],
)
def test_the_field_is_found_by_every_spelling(
    conn: sqlite3.Connection, table: str, column: str | None
) -> None:
    assert F.resolve_field(conn, table, column) == (ARRECAD, "k00_valor")


def test_unknown_table_and_column_are_clear_errors(conn: sqlite3.Connection) -> None:
    with pytest.raises(QueryError, match="tabela não encontrada: caixa.fantasma"):
        F.resolve_field(conn, "caixa.fantasma.k00_valor")
    with pytest.raises(QueryError, match="tabela não encontrada: fantasma"):
        F.resolve_field(conn, "fantasma", "k00_valor")
    with pytest.raises(QueryError, match="coluna inexistente em caixa.arrecad: k00_xx") as error:
        F.resolve_field(conn, "caixa.arrecad.k00_xx")
    assert "k00_valor" in str(error.value), "sugere colunas parecidas"
    with pytest.raises(QueryError, match="informe a tabela e o campo"):
        F.resolve_field(conn, "arrecad")
    with pytest.raises(QueryError, match="tabela não encontrada"):
        F.field_dossier(conn, "caixa.fantasma", "x")


def test_a_table_name_in_two_schemas_asks_for_the_schema(tmp_path: Path) -> None:
    db = build_db(tmp_path, "duas")
    with closing(sqlite3.connect(db)) as connection:
        connection.execute(
            "INSERT INTO nodes (id, label, attrs) VALUES (?, 'Table', '{}')",
            ("Table:outro.arrecad",),
        )
        with pytest.raises(QueryError, match="mais de uma tabela chamada 'arrecad'"):
            F.resolve_field(connection, "arrecad.k00_valor")
        assert F.resolve_field(connection, "caixa.arrecad.k00_valor")[0] == ARRECAD


# --------------------------------------------------------------------------- cadastro


def test_registered_in_has_the_menu_the_screen_and_the_class(conn: sqlite3.Connection) -> None:
    dossier = F.field_dossier(conn, "caixa.tabrecjm.k02_juros")
    reg = dossier.registered_in
    assert (dossier.table_id, dossier.column, dossier.label) == (TABRECJM, "k02_juros", "juros")
    assert reg.menus == (MENU_TABRECJM,)
    assert reg.screens == ("File:cai1_recejm001.php",)
    assert reg.writers == ("Method:cl_tabrecjm::incluir",)
    text = F.render_field_dossier(dossier)
    assert f"- menu: {MENU_TABRECJM}" in text
    assert "a tela grava a **tabela inteira** (todos os campos) pela classe" in text
    assert "`cl_tabrecjm::incluir`" in text
    assert "Nada ficou sem rastro." in text.partition("## Não rastreado")[2]


def test_a_primary_key_has_its_menu_and_a_copy_without_formula(conn: sqlite3.Connection) -> None:
    dossier = F.field_dossier(conn, "cadastro.iptubase.j01_matric")
    assert dossier.registered_in.menus == (MENU_IPTUBASE,)
    (write,) = dossier.computed_by
    assert write.routine_id == ATUALIZA and write.formula is None
    assert write.expression == "matric" and write.copy_of == () and write.slice == ()
    assert "valor sem conta (variável, parâmetro ou literal): `matric`" in F.render_field_dossier(
        dossier
    )


# --------------------------------------------------------------------------- como é calculado


def test_the_write_formula_is_the_slice_that_leads_to_the_stored_value(
    conn: sqlite3.Connection,
) -> None:
    dossier = F.field_dossier(conn, "caixa.arrecad.k00_valor")
    write = _write(dossier, GRAVA)
    assert write.formula is not None and write.formula.kind == "update_set"
    assert write.formula.target == "arrecad.k00_valor"
    assert write.expression == "k00_valor * base" and write.line == 12
    assert [(f.target, f.line) for f in write.slice] == [("base", 11)], "só a conta da variável"
    inputs = {f"{b.table_id}#{b.column}": b for b in write.inputs}
    assert inputs["Table:caixa.tabrecjm#k02_juros"].menus == (MENU_TABRECJM,)
    assert "Table:caixa.arrecad#k00_valor" in inputs
    text = F.render_field_dossier(dossier)
    assert "- cálculo: arrecad.k00_valor = Valor [arrecad.k00_valor] × base" in text
    assert "- vem de: linha 11: base = juros [tabrecjm.k02_juros] ÷ 100 + 1" in text
    assert "taxa" not in "".join(f.expression for f in write.slice).replace("taxa / 100", "")


def test_the_whole_chain_of_a_write_with_two_statements_and_an_auxiliary_call(
    conn: sqlite3.Connection,
) -> None:
    dossier = F.field_dossier(conn, "public.tmpcalc.vtotal")
    assert dossier.label is None, "tabela fora do dicionário: sem rótulo inventado"
    assert [(w.line, w.formula.kind if w.formula else None) for w in dossier.computed_by] == [
        (32, "update_set"),
        (33, "insert_value"),
    ]
    first = dossier.computed_by[0]
    assert first.calls == (JUROS,), "a conta passa por uma função auxiliar: o link leva a ela"
    assert {f.target for f in first.slice} == {"juro", "correcao"}
    assert dossier.registered_in.menus == ()
    reasons = dict(dossier.untraced)
    assert reasons["public.tmpcalc"] == "tabela sem tela de cadastro no mapa"
    text = F.render_field_dossier(dossier)
    assert text.startswith("# Dossiê do campo: public.tmpcalc.vtotal\n"), "sem rótulo no título"
    assert "- chama: `public.fc_juros_t(int4,date,bool)`" in text


def test_a_direct_copy_names_the_source_column(conn: sqlite3.Connection) -> None:
    dossier = F.field_dossier(conn, "caixa.arrecad.k00_valor")
    write = _write(dossier, COPIA)
    assert write.formula is None and write.copy_of == ("caixa.disbanco.vlrpago",)
    assert write.expression == "vlrpago"
    (entry,) = write.inputs
    assert (entry.table_id, entry.column, entry.label) == (
        "Table:caixa.disbanco",
        "vlrpago",
        "Valor pago",
    )
    assert "cópia direta de `caixa.disbanco.vlrpago`: `vlrpago`" in F.render_field_dossier(dossier)


def test_writers_with_a_formula_come_before_the_copies_even_if_the_name_sorts_first(
    conn: sqlite3.Connection,
) -> None:
    dossier = F.field_dossier(conn, "caixa.arrecad.k00_valor")
    assert [w.routine_id for w in dossier.computed_by] == [GRAVA, COPIA]
    assert dossier.writers_total == 2 and dossier.more_writers == 0


def test_the_cut_keeps_ten_functions_and_says_how_many_were_left_out(
    conn26: sqlite3.Connection,
) -> None:
    dossier = F.field_dossier(conn26, "caixa.arrecad.k00_valor")
    assert dossier.writers_total == 28 and dossier.more_writers == 18
    routines = {w.routine_id for w in dossier.computed_by}
    assert len(routines) == 10 and GRAVA in routines
    assert COPIA not in routines, "a cópia, sem fórmula, é a primeira a sair do corte"
    text = F.render_field_dossier(dossier)
    assert "_e mais 18 gravação(ões) em outras funções (total: 28 função(ões))_" in text
    small = F.field_dossier(conn26, "caixa.arrecad.k00_valor", max_writers=3)
    assert len({w.routine_id for w in small.computed_by}) == 3 and small.more_writers == 25


def test_an_unreadable_write_is_listed_as_not_traced(tmp_path: Path) -> None:
    db = build_db(tmp_path, "semtexto")
    with closing(sqlite3.connect(db)) as connection:
        connection.execute("DELETE FROM routine_sources WHERE node_id = ?", (GRAVA,))
        dossier = F.field_dossier(connection, "caixa.arrecad.k00_valor")
    assert GRAVA not in {w.routine_id for w in dossier.computed_by}
    assert any("não tem texto no mapa" in reason for _what, reason in dossier.untraced)


# --------------------------------------------------------------------------- quem usa


def test_readers_by_name_and_the_select_star_are_listed_apart(conn: sqlite3.Connection) -> None:
    dossier = F.field_dossier(conn, "caixa.tabrecjm.k02_juros")
    assert [r for r, _roles in dossier.read_by] == [GRAVA, JUROS]
    assert dossier.read_by_total == 2
    assert dossier.star_readers == (LE_TUDO,) and dossier.star_readers_total == 1
    assert dossier.computed_by == () and dossier.writers_total == 0
    text = F.render_field_dossier(dossier)
    assert "Nenhuma função da base grava este campo." in text
    assert "Lê a tabela inteira (`select *`; pode usar o campo, o mapa não diz):" in text
    assert f"- `{LE_TUDO.partition(':')[2]}`" in text


def test_the_roles_of_each_reader_are_joined_in_a_fixed_order(conn: sqlite3.Connection) -> None:
    dossier = F.field_dossier(conn, "caixa.arrecad.k00_valor")
    roles = dict(dossier.read_by)
    assert roles[LEITOR] == "read, filter, sort"
    assert "- `public.fc_leitor_valor()`: read, filter, sort" in F.render_field_dossier(dossier)


def test_a_field_nobody_uses_says_so(conn: sqlite3.Connection) -> None:
    dossier = F.field_dossier(conn, "caixa.semrotulo.campo_a")
    assert dossier.read_by == () and dossier.star_readers == () and dossier.computed_by == ()
    text = F.render_field_dossier(dossier)
    assert "Nenhuma função da base usa este campo pelo nome." in text
    assert dict(dossier.untraced) == {"caixa.semrotulo": "tabela sem tela de cadastro no mapa"}


# --------------------------------------------------------------------------- saída


def test_the_json_has_the_same_content_as_the_markdown(conn: sqlite3.Connection) -> None:
    dossier = F.field_dossier(conn, "caixa.arrecad.k00_valor")
    data = F.field_dossier_json(dossier)
    assert data["table_id"] == ARRECAD and data["column"] == "k00_valor"
    assert data["registered_in"]["menus"] == []
    assert [w["routine_id"] for w in data["computed_by"]] == [GRAVA, COPIA]
    assert data["computed_by"][1]["formula"] is None
    assert data["read_by"][0] == {"routine_id": GRAVA, "roles": "read"}
    assert {"what": "caixa.arrecad", "reason": "tabela sem tela de cadastro no mapa"} in data[
        "untraced"
    ]


def test_a_big_field_dossier_is_cut_by_words_and_says_how_many_were_left_out(
    conn26: sqlite3.Connection,
) -> None:
    dossier = F.field_dossier(conn26, "caixa.arrecad.k00_valor")
    whole = F.render_field_dossier(dossier, max_words=100_000)
    cut = F.render_field_dossier(dossier, max_words=300)
    assert len(cut.split()) < len(whole.split())
    assert "gravação(ões) em outras funções" in cut
    assert "## Quem usa" in cut and "## Não rastreado" in cut, "as seções fixas continuam"


def test_a_map_without_the_function_queries_says_what_to_do(tmp_path: Path) -> None:
    import formula_graphs

    db = formula_graphs.plain_db(
        tmp_path, {CALC: formula_graphs.simple_ddl("fc_x", "integer", "return 1;")}
    )
    with closing(sqlite3.connect(db)) as connection:
        connection.execute(
            "INSERT INTO nodes (id, label, attrs) VALUES (?, 'Table', '{}')",
            (ARRECAD,),
        )
        connection.execute("DROP TABLE IF EXISTS routine_queries")
        with pytest.raises(QueryError, match="não tem as consultas das funções"):
            F.field_dossier(connection, "caixa.arrecad.k00_valor")
    assert IPTUBASE and TMPCALC  # nomes usados em outros testes do módulo


def test_a_value_taken_from_a_computed_loop_column_shows_that_account(
    conn: sqlite3.Connection,
) -> None:
    dossier = F.field_dossier(conn, "caixa.disbanco.vlrpago")
    (write,) = dossier.computed_by
    assert write.routine_id == LACO and write.formula is None and write.expression == "rcalc.dobro"
    (account,) = write.slice
    assert (account.kind, account.target, account.line) == ("loop_column", "rcalc.dobro", 9)
    assert account.expression == "k02_codigo * 2"
    reasons = dict(dossier.untraced)
    assert reasons["rcalc.dobro"] == "campo calculado na própria consulta do laço"
    text = F.render_field_dossier(dossier)
    assert "- vem de: linha 9: rcalc.dobro = k02_codigo * 2" in text
    assert [r for r, _roles in dossier.read_by] == [COPIA]


def test_a_write_with_an_account_behind_it_ranks_before_a_plain_copy(
    conn26: sqlite3.Connection,
) -> None:
    """O corte mantém quem tem conta (fórmula ou conta da fatia) e deixa a cópia pura por último."""
    dossier = F.field_dossier(conn26, "caixa.disbanco.vlrpago")
    assert dossier.writers_total == 1 and dossier.computed_by[0].routine_id == LACO


def test_an_insert_select_with_union_takes_the_value_from_the_first_select(
    conn: sqlite3.Connection,
) -> None:
    """`fc_baixabanco:900` (real) grava `recibopaga.k00_valor` por `insert … select … union`."""
    dossier = F.field_dossier(conn, "cadastro.iptubase.j01_numcgm")
    (write,) = dossier.computed_by
    assert write.routine_id == "PgFunction:public.fc_uniao()"
    assert write.formula is None and write.copy_of == ("caixa.disbanco.k00_numpre",)
    assert write.expression == "k00_numpre"
    assert not any("não pôde ser lido" in reason for _what, reason in dossier.untraced)
