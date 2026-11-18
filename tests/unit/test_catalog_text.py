"""Texto do catálogo com UTF-8 gravado duas vezes numa base LATIN1.

Os textos imitam os de uma base real (escritos à mão; nenhum dado vem de um banco).
"""

from typing import Any

import pytest

from php_map.ingest.catalog_db import load_catalog_db
from php_map.ingest.catalog_sql import repair_text

DSN = "host=db.exemplo dbname=ecidade user=leitor password=x"
Rows = dict[str, list[dict[str, Any]]]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("AlteraÃ§Ã£o", "Alteração"),
        ("ConferÃªncia da PrevisÃ£o da Despesa", "Conferência da Previsão da Despesa"),
        ("Esfera orÃ§amentÃ¡ria", "Esfera orçamentária"),
        # misto: um acento certo (Latin-1) e outro corrompido no mesmo texto
        ("Valor Máximo DÃ©bito", "Valor Máximo Débito"),
        ("Liquidações por PerÃ\xadodo-desativado", "Liquidações por Período-desativado"),
        # o acento se perdeu antes de chegar ao banco: sobra o caractere de substituição
        ("Identificaï¿½ï¿½o dos advogados", "Identifica��o dos advogados"),
    ],
)  # fmt: skip
def test_repairs_double_encoded_text(text: str, expected: str) -> None:
    assert repair_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "RETENÇÃO",
        "Bolsa Família \r\n1-NÃO\r\n 2-SIM",
        "Define se o pedido é urgente ou não.\r\n2 - NÃo.",
        "Nº do empenho",
        "Anulação de  Empenho",
        "Valor em € → acima de \xff",  # Unicode de verdade não é tocado
        "",
    ],
)
def test_keeps_legitimate_text(text: str) -> None:
    assert repair_text(text) == text


def test_catalog_repairs_free_text_and_counts(pilot_rows: Rows) -> None:
    pilot_rows["db_itensmenu"][4].update(
        descricao="AnulaÃ§Ã£o", help="InclusÃ£o", desctec="Cï¿½digo"
    )
    pilot_rows["db_sysarquivo"][0].update(descricao="Empenhos", rotulo="RelatÃ³rios")
    pilot_rows["db_syscampo"][2].update(descricao="Valor Máximo DÃ©bito")
    pilot_rows["db_modulos"][0].update(nome_modulo="EmpenhÃ£o", descr_modulo="PrevisÃ£o")
    pilot_rows["db_sysmodulo"][0].update(nomemod="empenhÃ£o")
    data = load_catalog_db(DSN, fetch=lambda table: pilot_rows[table])

    item = next(i for i in data.menu_items if i.id_item == 9001)
    assert (item.description, item.help, item.desctec) == ("Anulação", "Inclusão", "C�digo")
    assert data.tables[0].label == "Relatórios"
    assert data.columns[2].description == "Valor Máximo Débito"
    menu_module = next(m for m in data.menu_modules if m.id_item == 398)
    assert menu_module.description == "Previsão"
    # nomes são chave (equivalência de módulos): não mudam
    assert menu_module.name == "EmpenhÃ£o"
    assert data.modules[0].name == "empenhÃ£o"
    assert data.stats.text_repaired == 5  # 2 do item, 1 da tabela, 1 da coluna, 1 do módulo
    assert data.stats.text_lost == 1  # o `desctec` do item
