"""Catálogo no grafo: `resolve/catalog.py`."""

from collections import Counter
from pathlib import Path

import pytest

from php_map.config import MODULE_ALIASES
from php_map.graph.builder import GraphBuilder, StoredEdge
from php_map.graph.model import Node, NodeLabel, file_id, normalize_module_name, table_id
from php_map.ingest.catalog_sql import CatalogData, load_catalog
from php_map.resolve.catalog import (
    MAX_MENU_DEPTH,
    CatalogStats,
    apply_catalog,
    split_funcao,
)
from php_map.resolve.modules import apply_naming_modules

MODULES = (
    "insert into db_sysmodulo values (38, 'empenho', 'Módulo de empenho', '2005-01-01', 't');\n"
    "insert into db_modulos values (398, 'Empenho', 'Empenho do menu', '', 'true');\n"
    "insert into db_modulos values (28, 'Compras', 'Compras', '', 'true');\n"
    "insert into db_itensmenu values (32, 'Procedimentos', '', '', '1', '1', '');\n"
    "insert into db_itensmenu values (31, 'Consultas', '', '', '1', '1', '');\n"
    "insert into db_menu values (398, 32, 1, 398), (398, 31, 2, 398), (28, 31, 1, 28);\n"
)


def _catalog(tmp_path: Path, text: str) -> CatalogData:
    path = tmp_path / "catalogo.sql"
    path.write_text(text, encoding="utf-8")
    return load_catalog([path])


def _builder(*files: str, tables: tuple[str, ...] = ()) -> GraphBuilder:
    builder = GraphBuilder()
    for path in files:
        builder.add_node(Node(file_id(path), NodeLabel.FILE, {"path": path}))
    for table in tables:
        schema, name = table.split(".")
        builder.add_node(Node(table_id(name, schema), NodeLabel.TABLE, {"name": name}))
    return builder


def _apply(tmp_path: Path, text: str, builder: GraphBuilder) -> CatalogStats:
    return apply_catalog(_catalog(tmp_path, text), builder)


def _edges(builder: GraphBuilder) -> dict[tuple[str, str, str], StoredEdge]:
    return {(e.src, e.type.value, e.dst): e for e in builder.iter_edges()}


# --------------------------------------------------------------------------- módulos


def test_modules_merge_by_normalized_name(tmp_path: Path) -> None:
    builder = GraphBuilder()
    stats = _apply(
        tmp_path,
        MODULES
        + "insert into db_sysmodulo values (81,'esocial','Módulo para o e-Social','2016-03-24');\n"
        "insert into db_modulos values (10216, 'e-Social', 'e-Social', '', 'true');\n"
        "insert into db_modulos values (1, ' - ', '', '', 'true');",
        builder,
    )
    esocial = builder.node_attrs("Module:esocial")
    # o nome de exibição é o do menu; o do dicionário fica em `names`.
    assert esocial["name"] == "e-Social" and esocial["names"] == ["e-Social", "esocial"]
    assert esocial["aliases"] == []  # fundido pelo nome, não pela equivalência
    assert (esocial["codmod"], esocial["menu_id"]) == (81, 10216)
    assert esocial["description"] == "Módulo para o e-Social"
    assert builder.node_attrs("Module:compras")["codmod"] is None
    assert (stats.modules, stats.modules_merged, stats.modules_invalid) == (3, 2, 1)
    assert (stats.modules_dictionary, stats.modules_menu) == (2, 3)


# --------------------------------------------------------------------------- tabelas


TABLES = (
    "insert into db_sysarquivo values (889, 'empempenho', 'Empenhos', 'e60', '2005-01-01', "
    "'Empenhos', 0);\n"
    "insert into db_sysarquivo values (3807, 'empenhocotamensal', 'Cotas', 'e05', '2015-04-27', "
    "'Cotas', 0);\n"
    "insert into db_sysarqmod values (38, 889), (38, 3807), (99, 889);\n"
    "insert into db_syscampo values(2, 'e60_vlranu', 'float8', 'Valor anulado', '0', "
    "'Valor Anulado', 15);\n"
    "insert into db_syscampo values(1, 'e60_numemp', 'int4', 'Número', '0', 'Número', 10);\n"
    "insert into db_sysarqcamp values (889, 2, 2, 0), (889, 1, 1, 0);\n"
)


def test_table_gets_module_and_catalog_attrs_in_every_schema(tmp_path: Path) -> None:
    builder = _builder(tables=("public.empempenho", "empenho.empempenho", "public.outra"))
    builder.ensure_node("Table:public.orcdotacao")  # placeholder que o catálogo não cita
    stats = _apply(tmp_path, MODULES + TABLES, builder)
    edges = _edges(builder)
    for table in ("Table:public.empempenho", "Table:empenho.empempenho"):
        edge = edges[(table, "BELONGS_TO", "Module:empenho")]
        assert (edge.source.value, edge.confidence) == ("ECIDADE_CATALOG", 1.0)
        assert edge.evidence == {"path": "catalogo.sql", "line": 9}
        attrs = builder.node_attrs(table)
        assert attrs["catalog"] == {
            "codarq": 889,
            "description": "Empenhos",
            "prefix": "e60",
            "label": "Empenhos",
        }
        assert list(attrs["columns_catalog"]) == ["e60_numemp", "e60_vlranu"]  # por seqarq
        assert attrs["columns_catalog"]["e60_vlranu"] == {
            "label": "Valor Anulado",
            "description": "Valor anulado",
        }
    assert stats.tables_unreferenced == 1  # empenhocotamensal
    assert stats.tables_with_module == 2 and stats.tables_annotated == 2
    assert stats.table_modules_unknown == 1  # codmod 99
    assert "catalog" not in builder.node_attrs("Table:public.outra")


def test_placeholder_table_stays_placeholder(tmp_path: Path) -> None:
    builder = GraphBuilder()
    builder.ensure_node("Table:public.empempenho")
    _apply(tmp_path, MODULES + TABLES, builder)
    attrs = builder.node_attrs("Table:public.empempenho")
    assert attrs["resolved"] is False and attrs["catalog"]["prefix"] == "e60"
    assert ("Table:public.empempenho", "BELONGS_TO", "Module:empenho") in _edges(builder)


# --------------------------------------------------------------------------- menus


@pytest.mark.parametrize(
    ("funcao", "expected"),
    [
        (
            "com4_anulcompregistro001.php?formacontrole=2",
            ("com4_anulcompregistro001.php", "formacontrole=2"),
        ),
        ("  ./Emp4_X001.php ", ("Emp4_X001.php", None)),
        ("forms\\db_frm.php?", ("forms/db_frm.php", None)),
        ("/func_x.php?a=1&b=2", ("func_x.php", "a=1&b=2")),
    ],
)
def test_split_funcao(funcao: str, expected: tuple[str, str | None]) -> None:
    assert split_funcao(funcao) == expected


def test_menu_item_creates_action_and_module_edges(tmp_path: Path) -> None:
    builder = _builder("emp4_anularempenho001.php", "func_empempenho.php")
    before = Counter(node.label.value for node in builder.iter_nodes())
    stats = _apply(
        tmp_path,
        MODULES + "insert into db_itensmenu values (9001, 'Anulação de Empenho', 'Ajuda', "
        "'emp4_anularempenho001.php', '1', '1', 'Técnico');\n"
        "insert into db_itensmenu values (9003, 'Empenhos', '', "
        "' func_empempenho.php?funcao_js=parent.js_mostra', 'f', '1', '');\n"
        "insert into db_itensmenu values (9004, 'Inexistente', '', 'emp1_x001.php', "
        "'1', '1', '');\n"
        "insert into db_menu values (32, 9001, 1, 398), (31, 9003, 1, 398), (31, 9003, 4, 28);\n",
        builder,
    )
    action = builder.node_attrs("BusinessAction:menu:9001")
    assert action["name"] == "Anulação de Empenho"
    assert action["menu_path"] == "Empenho > Procedimentos > Anulação de Empenho"
    assert (action["help"], action["desctec"], action["active"], action["params"]) == (
        "Ajuda",
        "Técnico",
        True,
        None,
    )
    consult = builder.node_attrs("BusinessAction:menu:9003")
    assert consult["menu_paths"] == [
        "Compras > Consultas > Empenhos",
        "Empenho > Consultas > Empenhos",
    ]
    assert (consult["active"], consult["params"]) == (False, "funcao_js=parent.js_mostra")

    edges = _edges(builder)
    for key in [
        ("File:emp4_anularempenho001.php", "IMPLEMENTS_ACTION", "BusinessAction:menu:9001"),
        ("File:emp4_anularempenho001.php", "BELONGS_TO", "Module:empenho"),
        ("File:func_empempenho.php", "IMPLEMENTS_ACTION", "BusinessAction:menu:9003"),
        ("File:func_empempenho.php", "BELONGS_TO", "Module:empenho"),
        ("File:func_empempenho.php", "BELONGS_TO", "Module:compras"),
    ]:
        assert (edges[key].source.value, edges[key].confidence) == ("ECIDADE_CATALOG", 1.0)
    # Evidências: a linha do item (IMPLEMENTS_ACTION) e a do db_menu que o pendura (BELONGS_TO).
    assert edges[
        ("File:emp4_anularempenho001.php", "IMPLEMENTS_ACTION", "BusinessAction:menu:9001")
    ].evidence == {"path": "catalogo.sql", "line": 7}
    assert edges[("File:emp4_anularempenho001.php", "BELONGS_TO", "Module:empenho")].evidence == {
        "path": "catalogo.sql",
        "line": 10,
    }

    assert "BusinessAction:menu:9004" not in builder
    assert stats.menu_missing_files == 1 and stats.menu_missing_examples == ["9004: emp1_x001.php"]
    assert (stats.menu_items_with_file, stats.menu_actions, stats.menu_items_inactive) == (3, 2, 1)
    after = Counter(node.label.value for node in builder.iter_nodes())
    assert after - before == Counter({"Module": 2, "BusinessAction": 2})


def test_one_action_per_menu_item_even_for_the_same_file(tmp_path: Path) -> None:
    builder = _builder("emp4_x001.php")
    _apply(
        tmp_path,
        MODULES + "insert into db_itensmenu values (1, 'Inclusão', '', 'emp4_x001.php?op=1', "
        "'1', '1', '');\n"
        "insert into db_itensmenu values (2, 'Inclusão', '', 'emp4_x001.php?op=2', "
        "'1', '1', '');\n"
        "insert into db_menu values (32, 1, 1, 398), (32, 2, 2, 398);\n",
        builder,
    )
    assert "BusinessAction:menu:1" in builder and "BusinessAction:menu:2" in builder
    module_edge = _edges(builder)[("File:emp4_x001.php", "BELONGS_TO", "Module:empenho")]
    assert module_edge.attrs["occurrences"] == 2  # agregada pelo builder


def test_item_without_menu_and_unknown_module(tmp_path: Path) -> None:
    builder = _builder("a.php", "b.php")
    stats = _apply(
        tmp_path,
        MODULES + "insert into db_itensmenu values (1, 'Solto', '', 'a.php', '1', '1', '');\n"
        "insert into db_itensmenu values (2, 'Outro', '', 'b.php', '1', '1', '');\n"
        "insert into db_menu values (500, 2, 1, 500);\n",
        builder,
    )
    solto = builder.node_attrs("BusinessAction:menu:1")
    assert solto["menu_path"] is None and solto["menu_paths"] == []
    assert builder.node_attrs("BusinessAction:menu:2")["menu_path"] == "[500] > Outro"
    assert stats.menu_without_path == 1 and stats.menu_unknown_modules == 1
    assert not any(key[1] == "BELONGS_TO" for key in _edges(builder))


def test_menu_cycle_is_cut(tmp_path: Path) -> None:
    builder = _builder("a.php")
    stats = _apply(
        tmp_path,
        MODULES + "insert into db_itensmenu values (1, 'Item', '', 'a.php', '1', '1', '');\n"
        "insert into db_itensmenu values (10, 'Pai', '', '', '1', '1', '');\n"
        "insert into db_itensmenu values (11, 'Avô', '', '', '1', '1', '');\n"
        "insert into db_menu values (10, 1, 1, 398), (11, 10, 1, 398), (10, 11, 1, 398);\n",
        builder,
    )
    assert builder.node_attrs("BusinessAction:menu:1")["menu_paths"] == [
        "Empenho > Avô > Pai > Item"
    ]
    assert stats.menu_truncated == 1


def test_menu_depth_is_limited(tmp_path: Path) -> None:
    builder = _builder("a.php")
    depth = MAX_MENU_DEPTH + 5
    chain = ", ".join(f"({n + 1}, {n}, 1, 398)" for n in range(1, depth))
    items = "\n".join(
        f"insert into db_itensmenu values ({n}, 'n{n}', '', '{'a.php' if n == 1 else ''}', "
        "'1', '1', '');"
        for n in range(1, depth + 1)
    )
    stats = _apply(tmp_path, f"{MODULES}{items}\ninsert into db_menu values {chain};", builder)
    (path,) = builder.node_attrs("BusinessAction:menu:1")["menu_paths"]
    assert len(path.split(" > ")) == MAX_MENU_DEPTH + 1  # prefixo + 15 níveis
    assert stats.menu_truncated == 1


def test_several_parents_give_several_sorted_paths(tmp_path: Path) -> None:
    builder = _builder("a.php")
    parents = ", ".join(f"({100 + n}, 1, 1, 398)" for n in range(7))
    items = "\n".join(
        f"insert into db_itensmenu values ({100 + n}, 'Menu {n}', '', '', '1', '1', '');"
        for n in range(7)
    )
    _apply(
        tmp_path,
        MODULES
        + "insert into db_itensmenu values (1, 'Item', '', 'a.php', '1', '1', '');\n"
        + items
        + f"\ninsert into db_menu values {parents};",
        builder,
    )
    attrs = builder.node_attrs("BusinessAction:menu:1")
    assert attrs["menu_path"] == "Empenho > Menu 0 > Item"
    assert attrs["menu_paths"] == [f"Empenho > Menu {n} > Item" for n in range(5)]  # até 5


def test_shared_root_does_not_leak_into_other_modules(tmp_path: Path) -> None:
    """`Consultas` (31) está no Empenho e no Compras: o item de Compras não sobe pelo Empenho."""
    builder = _builder("com_x.php")
    _apply(
        tmp_path,
        MODULES + "insert into db_itensmenu values (1, 'Item', '', 'com_x.php', '1', '1', '');\n"
        "insert into db_menu values (31, 1, 1, 28);\n",
        builder,
    )
    assert builder.node_attrs("BusinessAction:menu:1")["menu_paths"] == [
        "Compras > Consultas > Item"
    ]
    edges = _edges(builder)
    assert ("File:com_x.php", "BELONGS_TO", "Module:compras") in edges
    assert ("File:com_x.php", "BELONGS_TO", "Module:empenho") not in edges


# --------------------------------------------------------------------------- equivalência (24)

DIVIDA = (
    "insert into db_sysmodulo values (8, 'divida', 'Dívida Ativa', '2005-01-01', 't');\n"
    "insert into db_sysmodulo values (46, 'tributario', 'Tributario', '2005-01-01', 't');\n"
    "insert into db_sysarquivo values (100, 'divida', 'Dívidas', 'v01', '2005-01-01', '', 0);\n"
    "insert into db_sysarqmod values (8, 100);\n"
    "insert into db_modulos values (81, 'Dívida Ativa', 'Dívida Ativa', '', 'true');\n"
    "insert into db_modulos values (308, 'Contatos', '', '', 'true');\n"
    "insert into db_itensmenu values (29, 'Cadastros', '', '', '1', '1', '');\n"
    "insert into db_itensmenu values (500, 'Inclusão', '', 'div1_divida001.php', '1', '1', '');\n"
    "insert into db_menu values (81, 29, 1, 81), (29, 500, 1, 81);\n"
)


def test_alias_merges_the_dictionary_module_into_the_menu_module(tmp_path: Path) -> None:
    """`divida` (dicionário) → `dividaativa` (menu); tela, tabela e DAO no mesmo nó."""
    builder = _builder(
        "div1_divida001.php", "classes/db_divida_classe.php", tables=("public.divida",)
    )
    stats = _apply(tmp_path, DIVIDA, builder)
    naming = apply_naming_modules(builder)

    assert "Module:divida" not in builder
    module = builder.node_attrs("Module:dividaativa")
    assert module["name"] == "Dívida Ativa"
    assert module["names"] == ["Dívida Ativa", "divida"]
    assert module["aliases"] == ["divida"]
    assert (module["codmod"], module["menu_id"]) == (8, 81)
    assert module["description"] == "Dívida Ativa"  # a do dicionário (a do menu é reserva)
    edges = _edges(builder)
    for src in ("Table:public.divida", "File:div1_divida001.php"):
        assert (src, "BELONGS_TO", "Module:dividaativa") in edges
    dao = edges[("File:classes/db_divida_classe.php", "BELONGS_TO", "Module:dividaativa")]
    assert dao.source == "NAMING_RULE" and naming.daos_classified == 1
    assert (stats.modules, stats.modules_merged, stats.modules_aliased) == (3, 1, 1)
    assert stats.modules_dictionary_only == ["tributario"]
    assert stats.modules_menu_only == ["contatos"]
    assert len(stats.aliases_unused) == len(MODULE_ALIASES) - 1
    assert "divida→dividaativa" not in stats.aliases_unused


def test_alias_without_the_menu_side_creates_nothing(tmp_path: Path) -> None:
    builder = _builder(tables=("public.divida",))
    stats = _apply(tmp_path, DIVIDA.split("insert into db_modulos")[0], builder)

    assert "Module:dividaativa" not in builder
    assert builder.node_attrs("Module:divida")["name"] == "divida"
    assert ("Table:public.divida", "BELONGS_TO", "Module:divida") in _edges(builder)
    assert "divida→dividaativa" in stats.aliases_unused and stats.modules_aliased == 0
    assert stats.modules_dictionary_only == ["divida", "tributario"]


def test_custom_aliases_and_description_fallback(tmp_path: Path) -> None:
    builder = GraphBuilder()
    stats = apply_catalog(
        _catalog(
            tmp_path,
            "insert into db_sysmodulo values (3, 'issqn', null, '2005-01-01', 't');\n"
            "insert into db_modulos values (40, 'Mercantil', 'Cadastro mercantil', '', 'true');\n",
        ),
        builder,
        {"issqn": "mercantil"},
    )

    module = builder.node_attrs("Module:mercantil")
    assert module["description"] == "Cadastro mercantil"  # sem a do dicionário, fica a do menu
    assert stats.aliases_unused == [] and stats.modules == 1
    assert apply_catalog(_catalog(tmp_path, ""), GraphBuilder(), {}).aliases_unused == []


def test_module_aliases_table() -> None:
    """Os 15 pares de módulos equivalentes, já normalizados e sem encadeamento."""
    assert len(MODULE_ALIASES) == 15
    for source, target in MODULE_ALIASES.items():
        assert normalize_module_name(source) == source and normalize_module_name(target) == target
        assert source != target and target not in MODULE_ALIASES
    assert MODULE_ALIASES["divida"] == "dividaativa" and MODULE_ALIASES["issqn"] == "mercantil"
