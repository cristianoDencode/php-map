"""Dossiê de um item de menu (arquivos, funções com fórmulas, tabelas gravadas)."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import term_graphs as tg
from formula_graphs import MENU_TABRECJM, TABRECJM

from php_map.analysis import menu_dossier as md
from php_map.errors import QueryError
from php_map.graph import store


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tg.build_db(tmp_path)


def _dossier(db: Path, menu: str, **kwargs: int) -> md.MenuDossier:
    with closing(store.connect_readonly(db)) as conn:
        return md.menu_dossier(conn, menu, **kwargs)


def test_a_calculation_menu_lists_its_functions_with_the_formula_count(db: Path) -> None:
    dossier = _dossier(db, tg.MENU_CALC_ID)
    assert dossier.menu_id == tg.MENU_CALC_ID and dossier.menu_path == tg.MENU_CALC
    assert dossier.files == ("File:cad4_calciptu.php",)
    assert [(f.routine_id, f.formulas_total) for f in dossier.functions] == [
        (tg.CALCULOIPTU, 2),
        (tg.VERIFICA, 1),
    ]
    assert dossier.functions_total == 2 and dossier.functions_more == 0


def test_functions_are_ordered_by_formulas_then_name(db: Path) -> None:
    dossier = _dossier(db, tg.MENU_EXCLUI_ID)
    assert [(f.routine_id, f.formulas_total) for f in dossier.functions] == [
        (tg.CALCULOIPTU, 2),
        (tg.VERIFICA, 1),
        (tg.SEM_FORMULA, 0),
    ]


def test_the_function_cut_counts_the_rest(db: Path) -> None:
    dossier = _dossier(db, tg.MENU_EXCLUI_ID, max_functions=2)
    assert len(dossier.functions) == 2 and dossier.functions_more == 1
    assert dossier.functions_total == 3
    assert "e mais 1 função" in md.render_menu_dossier(dossier)


def test_a_dynamic_call_is_untraced_not_no_calculation(db: Path) -> None:
    dossier = _dossier(db, tg.MENU_CALC_ID)
    notes = {what: reason for what, reason in dossier.untraced}
    (what,) = [w for w in notes if w.startswith("fc_calculoiptu:")]
    assert "chamada dinâmica" in notes[what] and "execute" in notes[what]
    text = md.render_menu_dossier(dossier)
    assert text.index("## Não rastreado") < text.index("chamada dinâmica")
    assert "Nada ficou sem rastro" not in text


def test_a_registration_menu_lists_the_written_table_with_dictionary_fields(db: Path) -> None:
    dossier = _dossier(db, tg.MENU_JUROS_ID)
    assert dossier.menu_path == MENU_TABRECJM and dossier.functions == ()
    (table,) = dossier.tables_written
    assert table.table_id == TABRECJM and table.via == ("Method:cl_tabrecjm::incluir",)
    assert ("k02_juros", "juros") in table.columns and table.columns_more == 0
    assert dossier.untraced == ()
    text = md.render_menu_dossier(dossier)
    assert "### caixa.tabrecjm" in text and "`k02_juros` juros" in text
    assert "Nenhuma função da base é alcançada" in text


def test_tables_come_from_the_file_and_from_the_dao_without_the_audit_trail(db: Path) -> None:
    dossier = _dossier(db, tg.MENU_CALC_ID)
    by_table = {t.table_id: t for t in dossier.tables_written}
    assert set(by_table) == {tg.IPTUCALLOG, tg.IPTUNUMP}  # a auditoria não é da tela
    assert by_table[tg.IPTUCALLOG].via == ("File:cad4_calciptu.php",)
    assert by_table[tg.IPTUNUMP].via == ("Method:cl_iptunump::incluir",)


def test_a_table_shows_at_most_eight_fields_and_counts_the_rest(tmp_path: Path) -> None:
    builder = tg.build_builder()
    wide = {f"c{n:02d}": f"Campo {n}" for n in range(11)}
    from php_map.graph.model import Node, NodeLabel

    builder.add_node(
        Node(
            tg.IPTUNUMP,
            NodeLabel.TABLE,
            {"name": "iptunump", "schema": "cadastro", "columns_catalog": {
                k: {"label": v} for k, v in wide.items()
            }},
        )
    )  # fmt: skip
    from cross_graphs import save

    db = save(tmp_path, "largo", builder, {"project": tg.PROJECT, "source": "x"})
    tg.write_texts(db)
    table = {t.table_id: t for t in _dossier(db, tg.MENU_CALC_ID).tables_written}[tg.IPTUNUMP]
    assert len(table.columns) == md.MAX_COLUMNS and table.columns_more == 3
    assert "e mais 3" in md.render_menu_dossier(_dossier(db, tg.MENU_CALC_ID))


def test_a_menu_without_a_file_says_so(db: Path) -> None:
    dossier = _dossier(db, tg.MENU_ORFAO_ID)
    assert dossier.files == () and dossier.functions == () and dossier.tables_written == ()
    assert dossier.untraced == ((tg.MENU_ORFAO_ID, "nenhum arquivo implementa este menu"),)
    assert "Nenhum arquivo implementa este menu." in md.render_menu_dossier(dossier)


def test_a_menu_without_a_path_shows_the_id_and_the_file_not_an_invented_path(db: Path) -> None:
    dossier = _dossier(db, tg.MENU_SEM_CAMINHO_ID)
    assert dossier.menu_path is None
    text = md.render_menu_dossier(dossier)
    assert text.startswith(f"# Dossiê do menu: {tg.MENU_SEM_CAMINHO_ID} (cai1_tabrec001.php)")
    assert "sem caminho legível" in text


def test_the_menu_can_be_found_by_a_piece_of_the_path_without_accents(db: Path) -> None:
    with closing(store.connect_readonly(db)) as conn:
        assert md.resolve_menu(conn, "calculo iptu > exclusao de calculo parcial") == (
            tg.MENU_EXCLUI_ID
        )
        assert md.resolve_menu(conn, "Juros e Multas > Inclusão") == tg.MENU_JUROS_ID
        assert md.resolve_menu(conn, "menu:1533") == tg.MENU_CALC_ID
        assert md.resolve_menu(conn, "1533") == tg.MENU_CALC_ID
        assert md.resolve_menu(conn, tg.MENU_CALC) == tg.MENU_CALC_ID  # o caminho todo


def test_a_piece_that_matches_several_menus_is_ambiguous(db: Path) -> None:
    with closing(store.connect_readonly(db)) as conn, pytest.raises(md.AmbiguousMenuError) as err:
        md.resolve_menu(conn, "Cálculo Parcial")
    assert [c[0] for c in err.value.candidates] == [tg.MENU_CALC_ID, tg.MENU_EXCLUI_ID]
    assert tg.MENU_CALC_ID in str(err.value) and "use o id" in str(err.value)


def test_unknown_or_empty_menus_are_clear_errors(db: Path) -> None:
    with closing(store.connect_readonly(db)) as conn:
        with pytest.raises(QueryError, match="menu não encontrado"):
            md.resolve_menu(conn, "BusinessAction:menu:99999")
        with pytest.raises(QueryError, match="nenhum menu casa"):
            md.resolve_menu(conn, "Fantasma > Que não existe")
        with pytest.raises(QueryError, match="informe o menu"):
            md.resolve_menu(conn, "   ")


def test_an_old_map_without_queries_still_gives_the_dossier(tmp_path: Path) -> None:
    db = tg.build_db(tmp_path, "antigo")
    with closing(sqlite3.connect(db)) as raw, raw:
        raw.execute("DROP TABLE routine_queries")
    dossier = _dossier(db, tg.MENU_CALC_ID)
    assert [f.routine_id for f in dossier.functions] == [tg.CALCULOIPTU, tg.VERIFICA]
    assert not [w for w, _r in dossier.untraced if w.startswith("fc_calculoiptu")]


def test_the_json_has_the_same_facts_as_the_markdown(db: Path) -> None:
    dossier = _dossier(db, tg.MENU_CALC_ID)
    data = md.menu_dossier_json(dossier)
    assert data["menu_id"] == tg.MENU_CALC_ID
    assert data["functions"][0] == {"routine_id": tg.CALCULOIPTU, "formulas_total": 2}
    assert data["untraced"][0]["what"].startswith("fc_calculoiptu:")
    text = md.render_menu_dossier(dossier)
    assert "| `fc_calculoiptu` | 2 |" in text and "| `fc_verificacalculo` | 1 |" in text
