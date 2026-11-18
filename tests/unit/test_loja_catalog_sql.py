"""Leitor do dicionário de dados e dos menus: `ingest/catalog_sql.py`."""

from dataclasses import replace
from pathlib import Path

import pytest

from php_map.ingest.catalog_sql import CatalogData, CatalogReadStats, as_bool, load_catalog

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/catalog/loja_mini.sql"


def _load(tmp_path: Path, text: str, prefilter: bool = True) -> CatalogData:
    path = tmp_path / "catalogo.sql"
    path.write_text(text, encoding="utf-8")
    return load_catalog([path], prefilter=prefilter)


def _items(data: CatalogData) -> dict[int, str | None]:
    return {item.id_item: item.description for item in data.menu_items}


def _without_stats(data: CatalogData) -> CatalogData:
    return replace(data, stats=CatalogReadStats())


# --------------------------------------------------------------------------- INSERT


def test_positional_insert_ignores_extra_values(tmp_path: Path) -> None:
    data = _load(
        tmp_path,
        "insert into db_itensmenu values( 9952, 'Ficha financeira', 'Ajuda', "
        "'mat2_fichafinanceira001.php', '1', '1', 'Tec', '1', 'valor a mais' );",
    )
    (item,) = data.menu_items
    assert (item.id_item, item.description, item.help, item.funcao) == (
        9952,
        "Ficha financeira",
        "Ajuda",
        "mat2_fichafinanceira001.php",
    )
    assert item.active and item.desctec == "Tec"
    assert item.evidence == {"path": "catalogo.sql", "line": 1}
    assert data.stats.read == {"db_itensmenu": 1}


def test_insert_with_columns_and_missing_values(tmp_path: Path) -> None:
    data = _load(
        tmp_path,
        "insert into db_menu ( id_item ,id_item_filho ,menusequencia ,modulo ) "
        "values ( 1100873 ,9948 ,5 ,1100747 );\n"
        "INSERT INTO configuracoes.db_sysarquivo (codarq, nomearq, sigla) "
        "VALUES (3783, 'ZonasSetorValor', 'j141');\n"
        'insert into "configuracoes"."db_sysmodulo" values (81, \'esocial\');',
    )
    (edge,) = data.menu_edges
    assert (edge.parent, edge.child, edge.sequence, edge.module) == (1100873, 9948, 5, 1100747)
    (table,) = data.tables
    assert (table.name, table.prefix, table.description, table.label) == (
        "zonassetorvalor",
        "j141",
        None,
        None,
    )
    assert data.modules[0].name == "esocial" and data.modules[0].description is None


def test_multiple_rows_and_repeated_key_last_wins(tmp_path: Path) -> None:
    data = _load(
        tmp_path,
        "insert into db_sysarqmod values (1, 10), (2, 10);\n"
        "insert into db_itensmenu values (5, 'Antigo', '', '', '1', '1', '');\n"
        "insert into db_itensmenu values (5, '  Novo  ', '', '', '1', '1', '');",
    )
    assert [(link.codmod, link.codarq) for link in data.table_modules] == [(1, 10), (2, 10)]
    assert _items(data) == {5: "Novo"}  # texto com strip()
    assert data.menu_items[0].evidence["line"] == 3


def test_insert_select_is_only_counted(tmp_path: Path) -> None:
    data = _load(
        tmp_path,
        "insert into db_itensmenu select * from w_up_5208_db_itensmenu where not exists "
        "(select 1 from db_itensmenu where db_itensmenu.id_item = w_up_5208_db_itensmenu.id_item);",
    )
    assert data.menu_items == () and data.stats.unsupported == 1
    assert "INSERT … SELECT em db_itensmenu" in data.stats.examples[0]


def test_other_tables_are_ignored(tmp_path: Path) -> None:
    data = _load(
        tmp_path,
        "insert into db_itensfilho (id_item, codfilho) values(10007,1);\n"
        "insert into db_menu_x values (1, 2, 3, 4);\n"
        "create table db_menu (id_item int4);",
    )
    assert data.menu_edges == () and data.stats.unsupported == 0
    assert data.stats.statements == 3


# --------------------------------------------------------------------------- fc_executa_ddl


def test_fc_executa_ddl_with_old_escape(tmp_path: Path) -> None:
    data = _load(
        tmp_path,
        "-- cabeçalho\n"
        "select fc_executa_ddl('insert into db_sysarquivo values (3783, \\'zonassetorvalor\\', "
        "\\'Estrutura d\\'\\'água\\', \\'j141\\', \\'2015-03-03\\', \\'Valores\\', 0, \\'f\\');\n"
        "insert into db_sysarqmod values (36, 3783);');",
    )
    (table,) = data.tables
    assert (table.name, table.prefix, table.label) == ("zonassetorvalor", "j141", "Valores")
    assert table.description == "Estrutura d'água"
    assert [link.codarq for link in data.table_modules] == [3783]
    assert data.stats.executed_ddl == 1 and data.stats.escaped_files == 1
    assert table.evidence["line"] == 2 and data.table_modules[0].evidence["line"] == 3


def test_fc_executa_ddl_without_catalog_tables_is_skipped(tmp_path: Path) -> None:
    data = _load(tmp_path, "select fc_executa_ddl('create table x (id int4)');")
    assert data.stats.executed_ddl == 0  # o pré-filtro nem manda para o pglast


# --------------------------------------------------------------------------- DELETE e UPDATE


def test_delete_in_and_partial_composite_key(tmp_path: Path) -> None:
    data = _load(
        tmp_path,
        "insert into db_itensmenu values (1, 'a', '', '', '1', '1', '');\n"
        "insert into db_itensmenu values (2, 'b', '', '', '1', '1', '');\n"
        "insert into db_itensmenu values (3, 'c', '', '', '1', '1', '');\n"
        "insert into db_menu values (10, 1, 1, 7), (10, 2, 2, 7), (11, 2, 1, 8);\n"
        "delete from db_itensmenu where id_item in (1, 3, 99);\n"
        "delete from db_menu where id_item_filho = 2 and modulo = 7;\n"
        "delete from db_itensmenu where id_item = 404;",
    )
    assert _items(data) == {2: "b"}
    assert [(e.parent, e.child, e.module) for e in data.menu_edges] == [(10, 1, 7), (11, 2, 8)]
    assert data.stats.no_match == 1 and data.stats.unsupported == 0


def test_update_changes_the_row_and_the_key(tmp_path: Path) -> None:
    data = _load(
        tmp_path,
        "insert into db_itensmenu values (9952, 'Antigo', '', 'x.php', '1', '1', '');\n"
        "update db_itensmenu set id_item = 9952 , descricao = 'Ficha financeira', "
        "itemativo = 'f' where id_item = 9952;\n"
        "insert into db_menu values (7962, 7968, 1, 28);\n"
        "update db_menu set id_item = 7941, menusequencia=6 "
        "where id_item_filho in (7968) and id_item = 7962;\n"
        "update db_itensmenu set descricao = 'x' where id_item = 1;",
    )
    (item,) = data.menu_items
    assert (item.description, item.active, item.evidence["line"]) == ("Ficha financeira", False, 2)
    (edge,) = data.menu_edges
    assert (edge.parent, edge.child, edge.sequence, edge.module) == (7941, 7968, 6, 28)
    assert data.stats.no_match == 1


@pytest.mark.parametrize(
    "statement",
    [
        "update db_itensmenu set libcliente = true where id_item in (select id_item from x);",
        "update db_syscampo set rotulo = 'Código' where nomecam = 'o124_sequencial';",
        "update db_menu dm set menusequencia = (select 1) from w where destino = id_item_filho;",
        "update db_itensmenu set descricao = upper(descricao) where id_item = 1;",
        "delete from db_itensmenu where id_item > 10;",
        "delete from db_itensmenu where id_item = 1 or id_item = 2;",
        "delete from db_itensmenu;",
    ],
)
def test_unsupported_where_or_value(tmp_path: Path, statement: str) -> None:
    data = _load(
        tmp_path, "insert into db_itensmenu values (1, 'a', '', '', '1', '1', '');\n" + statement
    )
    assert data.stats.unsupported == 1
    assert _items(data) == {1: "a"}  # nada foi alterado


# --------------------------------------------------------------------------- valores e robustez


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("t", True), (" TRUE ", True), (True, True), (1, True)]
    + [("2", False), ("f", False), ("false", False), (False, False), (None, False), (0, False)],
)
def test_as_bool(value: object, expected: bool) -> None:
    assert as_bool(value) is expected


def test_itemativo_variants(tmp_path: Path) -> None:
    rows = ", ".join(
        f"({n}, 'i', '', '', {value}, '1', '')"
        for n, value in enumerate(["'1'", "'t'", "'true'", "true", "'2'", "'f'", "false", "null"])
    )
    data = _load(tmp_path, f"insert into db_itensmenu values {rows};")
    assert [item.active for item in data.menu_items] == [True] * 4 + [False] * 4


def test_parse_error_and_copy_are_counted(tmp_path: Path) -> None:
    data = _load(
        tmp_path,
        # Parênteses balanceados: com um `(` aberto, o `parser.split` do pglast descarta o resto
        # do arquivo sem erro (bug de `split_statements`).
        "insert into db_itensmenu values (1, 'a',, 'b');\n"
        "COPY configuracoes.db_menu (id_item, id_item_filho, menusequencia, modulo) FROM stdin;\n"
        "insert into db_itensmenu values (2, 'b', '', '', '1', '1', '');",
    )
    assert data.stats.parse_errors == 1 and data.stats.unsupported == 1
    assert _items(data) == {2: "b"}


def test_incremental_order(tmp_path: Path) -> None:
    """`v2.3.9` antes de `v2.3.10`; dentro da versão, `up/pre` antes de `up/pos`."""
    for version, name, text in [
        ("v2.3.10", "up/pos_v2.3.10.sql", "delete from db_itensmenu where id_item = 1;"),
        (
            "v2.3.10",
            "up/pre_v2.3.10.sql",
            "update db_itensmenu set descricao = 'dez' where id_item in (1, 2);",
        ),
        (
            "v2.3.9",
            "up/pre_v2.3.9.sql",
            "insert into db_itensmenu values (1, 'nove', '', '', '1', '1', ''),"
            " (2, 'nove', '', '', '1', '1', '');",
        ),
    ]:
        target = tmp_path / "db" / version / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    data = load_catalog([tmp_path / "db"])
    assert _items(data) == {2: "dez"}
    assert data.menu_items[0].evidence == {"path": "v2.3.10/up/pre_v2.3.10.sql", "line": 1}


# --------------------------------------------------------------------------- fixture e pré-filtro


def test_fixture_final_state() -> None:
    data = load_catalog([FIXTURE])
    items = _items(data)
    assert items[7001] == "Cancelar Pedido"  # latin-1 + UPDATE posterior
    assert 7005 not in items  # DELETE … IN
    assert not {item.id_item: item for item in data.menu_items}[7002].active
    assert {t.name: t.prefix for t in data.tables} == {
        "pedido": "ped",
        "pedidocancelado": "can",
        "itempedido": "ite",
        "estoque": "est",
        "transportadora": "tra",
        "devolucao": "dev",
    }
    assert {c.name: c.label for c in data.columns}["ped01_situacao"] == "Situação"
    assert data.stats.unsupported == 0 and data.stats.executed_ddl == 1
    assert data.stats.parse_errors == 0


def _incrementals(root: Path) -> Path:
    """Diretório de incrementais com as formas reais: DDL, DML de outras tabelas, catálogo
    dentro e fora de `fc_executa_ddl`, `INSERT … SELECT`, `UPDATE` e `DELETE`."""
    files = {
        "v2.3.1/up/pre_v2.3.1.sql": FIXTURE.read_text(encoding="latin-1"),
        "v2.3.1/up/ddl_v2.3.1.sql": (
            "create table transportadora (tra01_sequencial int4);\n"
            "insert into transportadora values (1);\n"
            "select fc_executa_ddl('create table x (id int4); insert into x values (1)');\n"
        ),
        "v2.3.2/up/pre_v2.3.2.sql": (
            "select fc_executa_ddl('insert into db_itensmenu values (7000, \\'Nova\\', \\'\\', "
            "\\'ped4_cancelarpedido001.php\\', \\'1\\', \\'1\\', \\'\\');');\n"
            "insert into db_menu values (32, 7000, 9, 398);\n"
            "update db_itensmenu set descricao = 'Cancelamento' where id_item = 7001;\n"
            "update db_menu set menusequencia = 1 where id_item_filho = 7000 and id_item = 32;\n"
            "delete from db_sysarqmod where codmod = 11 and codarq in (105);\n"
            "insert into db_menu select * from w_up_db_menu;\n"
        ),
    }
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


@pytest.mark.parametrize("which", ["fixture", "incrementals"])
def test_prefilter_does_not_change_the_result(tmp_path: Path, which: str) -> None:
    """Um pré-filtro de desempenho já apagou arestas sem aviso."""
    paths = [FIXTURE] if which == "fixture" else [_incrementals(tmp_path / "db")]
    filtered, full = load_catalog(paths), load_catalog(paths, prefilter=False)
    assert _without_stats(filtered) == _without_stats(full)
    assert filtered.stats.read == full.stats.read
    assert filtered.stats.unsupported == full.stats.unsupported
    assert filtered.menu_items  # o teste não é trivial
    if which == "incrementals":
        assert _items(filtered)[7001] == "Cancelamento" and 7000 in _items(filtered)
        assert len(filtered.table_modules) == 5
