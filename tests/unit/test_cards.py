"""Ficha da função em português (`report/cards.py`)."""

import sqlite3
from pathlib import Path

import pytest
from card_graphs import (  # noqa: F401 - reexportados: outros testes importam daqui
    DDL,
    FUNC,
    GENERATED_DDL,
    READ_AT,
    TABLE,
    TRIGGER_DDL,
    _db,
)

from php_map.errors import QueryError
from php_map.report import cards


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return _db(tmp_path)


def test_function_card_sections_with_real_data(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        card = cards.function_card(conn, FUNC)
    assert card.kind == "function"
    assert card.purpose == ("Confere o saldo antes de calcular o valor final",)
    assert card.rules and card.rules[0].kind == "validation"
    assert card.rules[0].condition == "b < 0"
    assert card.rules[0].message == "Saldo % invalido"
    assert card.rules[0].params == ("b",)
    [table] = card.tables
    assert table.label == "Empenhos" and table.technical == "public.empempenho"
    assert ("e60_vlranu", "Valor anulado") in table.columns
    assert ("e60_numemp", None) in table.columns
    assert "write" in table.operations and "filter" in table.operations


def test_function_card_section_without_data_shows_a_reason(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        card = cards.function_card(conn, FUNC)
    md = cards.render_function_markdown(card)
    assert "Não chama nenhuma outra função da base." in md
    assert card.origin is None  # sem routine_origin nesta fixture
    assert "Sem a origem das funções da base no mapa" in md


def test_function_card_trigger_and_generated_kinds(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        trigger = cards.function_card(conn, "PgFunction:public.fc_trig()")
        generated = cards.function_card(conn, "PgFunction:public.fc_auditoria_tabela_abc()")
    assert trigger.kind == "trigger" and trigger.returns_pt == "gatilho (trigger)"
    assert generated.kind == "generated"
    assert "função de gatilho (trigger)" in cards.render_function_markdown(trigger)
    assert "gerada pela auditoria do banco" in cards.render_function_markdown(generated)


def test_type_translation_known_and_unknown() -> None:
    assert cards.type_pt("int4") == "número inteiro"
    assert cards.type_pt("date") == "data"
    assert cards.type_pt("int4[]") == "lista de número inteiro"
    assert cards.type_pt("tipo_esquisito") == "tipo_esquisito"  # desconhecido: como está


def test_condition_and_message_side_by_side(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        card = cards.function_card(conn, FUNC)
    md = cards.render_function_markdown(card)
    assert "Se `b < 0`, a mensagem é: «Saldo % invalido»" in md
    assert "`%` = b" in md


def test_unknown_function_raises_with_suggestions(db: Path) -> None:
    with sqlite3.connect(db) as conn, pytest.raises(QueryError, match="inexistente"):
        cards.function_card(conn, "PgFunction:public.fc_naoexiste()")


def test_markdown_is_byte_identical_across_two_runs(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        first = cards.render_function_markdown(cards.function_card(conn, FUNC))
        second = cards.render_function_markdown(cards.function_card(conn, FUNC))
    assert first == second
    assert hash(first) == hash(second)


def test_source_lines_zero_disables_the_excerpt(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        card = cards.function_card(conn, FUNC, source_lines=0)
    assert card.source.lines == ()
    md = cards.render_function_markdown(card)
    assert "veja em `routine` no MCP" in md
