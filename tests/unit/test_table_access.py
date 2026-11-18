"""Quem grava e quem lê a tabela no PHP (`analysis/table_access`), sobre
`tests/access_graphs.py`."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from access_graphs import (
    CADASTRO,
    CGMBASE_EXCLUI,
    CGMBASE_GET,
    CGMTIPO,
    CONTROLLER,
    DAO_FILE,
    DUPLOS,
    EMP4,
    FORM,
    FUNC_FILE,
    LARAVEL_MIGRATION,
    MENU_CADASTRO,
    MENU_EMP4,
    MENU_REDESIM,
    MIGRATION,
    MUITOS,
    REPOSITORY,
    SERVICE,
    SERVICE_PATH,
    VAZIA,
    build_db,
)

from php_map.analysis import table_access as T
from php_map.errors import QueryError


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(build_db(tmp_path))) as connection:
        yield connection


@pytest.fixture
def access(conn: sqlite3.Connection) -> T.TableAccess:
    return T.table_access(conn, CGMTIPO)


def _find(items: tuple[T.Toucher, ...], node: str, how: str | None = None) -> T.Toucher:
    return next(t for t in items if t.node == node and (how is None or t.how == how))


def test_groups_by_caller_and_sums_occurrences_and_sample_lines(access: T.TableAccess) -> None:
    service = _find(access.writers, SERVICE)
    assert service.how == T.HOW_DAO
    assert service.operations == ("INSERT",)
    assert service.dao_methods == ("incluir",)
    assert service.occurrences == 2
    assert [s.line for s in service.lines] == [2687, 2974]
    assert {s.path for s in service.lines} == {SERVICE_PATH}
    assert service.path == SERVICE_PATH
    # o nome da classe volta com a caixa do arquivo
    assert service.symbol == "RedeSimImportaService::processarcadastroempresa"

    cadastro = _find(access.writers, f"File:{CADASTRO}")
    assert cadastro.operations == ("INSERT", "DELETE")
    assert cadastro.dao_methods == ("incluir", "excluir")
    assert [s.line for s in cadastro.lines] == [725, 716]
    assert cadastro.occurrences == 2

    repository = _find(access.writers, REPOSITORY)
    assert repository.operations == ("INSERT", "UPDATE")
    assert [s.line for s in repository.lines] == [68, 64]
    # a confiança é a menor entre a chamada (1.0) e a do DAO (`alterar` é SQL_REGEX 0.7)
    assert repository.confidence == 1.0  # a maior entre os pares: `incluir` vale 1.0


def test_confidence_is_the_smaller_of_call_and_dao_edge(conn: sqlite3.Connection) -> None:
    only_update = T.table_access(conn, CGMTIPO)
    # `Repository::persistcgm` chama `alterar` (0.7) e `incluir` (1.0): o grupo fica com 1.0
    assert _find(only_update.writers, REPOSITORY).confidence == 1.0
    # o SQL direto herda a confiança da própria aresta
    assert _find(only_update.writers, f"File:{DUPLOS}").confidence == 1.0


def test_more_calls_than_stored_lines_keeps_the_total(access: T.TableAccess) -> None:
    many = _find(access.writers, MUITOS)
    assert many.occurrences == 7
    assert len(many.lines) == T.MAX_LINES
    assert "+2 chamadas" in T.render_table_access(access)


def test_the_dao_itself_never_appears(access: T.TableAccess) -> None:
    everything = (*access.writers, *access.migrations, *access.readers)
    assert not any(t.node.startswith("Method:cl_") for t in everything)
    assert not any(t.path == DAO_FILE for t in everything)
    assert not any("incluir_lote" in t.node for t in everything)


def test_migration_is_apart_and_out_of_the_production_count(access: T.TableAccess) -> None:
    assert sorted(t.path or "" for t in access.migrations) == sorted([LARAVEL_MIGRATION, MIGRATION])
    migration = next(t for t in access.migrations if t.path == MIGRATION)
    assert migration.layer == T.MIGRATION_LAYER
    assert migration.how == T.HOW_SQL
    assert migration.operations == ("INSERT", "DELETE")
    assert all(
        not (t.path or "").startswith(("db/migrations/", "database/migrations/"))
        for t in access.writers
    )
    assert access.writers_total == len(access.writers)
    assert access.writers_total == 7  # 5 pelo DAO + SQL direto + model; a migração não entra
    assert not any(
        (t.path or "").startswith(("db/migrations/", "database/migrations/"))
        for t in access.readers
    )


def test_eloquent_is_use_never_insert_and_listed_as_writer_and_reader(
    access: T.TableAccess,
) -> None:
    writer = _find(access.writers, CONTROLLER, T.HOW_ELOQUENT)
    reader = _find(access.readers, CONTROLLER, T.HOW_ELOQUENT)
    for toucher in (writer, reader):
        assert toucher.operations == (T.USE,)
        assert toucher.dao_methods == ()
        assert toucher.symbol == "CgmController::index"
    assert all("INSERT" not in t.operations for t in access.writers if t.how == T.HOW_ELOQUENT)
    # o model chamando a si mesmo e o arquivo que o declara ficam de fora
    assert [t.node for t in access.writers if t.how == T.HOW_ELOQUENT] == [CONTROLLER]
    assert "uso do model" in T.render_table_access(access)


def test_direct_menu_menu_by_layers_and_reason(access: T.TableAccess) -> None:
    assert _find(access.writers, f"File:{CADASTRO}").menus == (MENU_CADASTRO,)
    # método sem menu direto: a subida das camadas chega em `iss4_redesim.php`
    assert _find(access.writers, SERVICE).menus == (MENU_REDESIM,)
    repository = _find(access.writers, REPOSITORY)
    assert repository.menus == ()
    assert repository.menu_reason is not None and "nenhum fonte chama este método" in (
        repository.menu_reason
    )
    exclui = _find(access.writers, CGMBASE_EXCLUI)
    assert exclui.menu_reason == T.NO_MENU
    duplos = _find(access.writers, f"File:{DUPLOS}")
    assert duplos.menu_reason == T.NO_INCLUDER
    assert access.unresolved_menu == sum(1 for t in access.writers if not t.menus)
    assert access.checked_menus >= 4


def test_writers_order_screens_with_menu_first_then_layer_then_path(
    access: T.TableAccess,
) -> None:
    with_menu = [t for t in access.writers if t.menus]
    assert access.writers[: len(with_menu)] == tuple(with_menu)
    first = access.writers[0]
    assert first.layer == "tela" and first.menus


def test_readers_count_cut_order_and_layers(conn: sqlite3.Connection) -> None:
    full = T.table_access(conn, CGMTIPO, max_readers=500)
    assert full.readers_total == 7
    assert len(full.readers) == 7
    cut = T.table_access(conn, CGMTIPO, max_readers=3)
    assert cut.readers_total == 7
    assert len(cut.readers) == 3
    assert cut.readers[0].menus  # tela com menu primeiro
    assert all(t.menus for t in cut.readers[:2])
    assert any("leitor(es) ficaram de fora" in note for note in cut.notes)
    assert sum(cut.readers_by_layer.values()) == cut.readers_total  # a contagem é completa
    assert cut.readers_by_layer["tela"] >= 3
    nodes = {t.node for t in full.readers}
    assert {f"File:{FUNC_FILE}", f"File:{EMP4}", f"File:{FORM}", CGMBASE_GET} <= nodes
    func = _find(full.readers, f"File:{FUNC_FILE}")
    assert func.operations == ("SELECT",)
    assert func.occurrences == 4
    assert _find(full.readers, f"File:{EMP4}").menus == (MENU_EMP4,)


def test_readers_are_cut_at_500(tmp_path: Path) -> None:
    db = build_db(tmp_path, "muitos", extra_readers=510)
    with closing(sqlite3.connect(db)) as connection:
        result = T.table_access(connection, CGMTIPO, max_readers=10_000)
    assert result.readers_total == 7 + 510
    assert len(result.readers) == T.MAX_READERS


def test_max_menu_climbs_is_respected_and_said_in_notes(tmp_path: Path) -> None:
    db = build_db(tmp_path, "subidas", extra_writers=10)
    with closing(sqlite3.connect(db)) as connection:
        limited = T.table_access(connection, CGMTIPO, max_menu_climbs=3)
        none = T.table_access(connection, CGMTIPO, max_menu_climbs=0)
        wide = T.table_access(connection, CGMTIPO, max_menu_climbs=200)
    assert limited.checked_menus == 3
    assert any("ficaram sem a subida até o menu" in note for note in limited.notes)
    skipped = [t for t in limited.writers if t.menu_reason and "não foi tentada" in t.menu_reason]
    assert skipped and "limite de 3" in skipped[0].menu_reason  # type: ignore[operator]
    assert none.checked_menus == 0
    assert wide.checked_menus > limited.checked_menus
    assert not any("ficaram sem a subida" in note for note in wide.notes)


def test_table_without_php_touch_and_missing_table(conn: sqlite3.Connection) -> None:
    empty = T.table_access(conn, VAZIA)
    assert empty.writers == () and empty.migrations == () and empty.readers == ()
    assert empty.writers_total == 0 and empty.readers_total == 0
    assert "nenhum fonte PHP grava ou lê esta tabela pelo mapa" in empty.notes
    text = T.render_table_access(empty)
    assert "Nenhum fonte PHP grava ou lê esta tabela pelo mapa." in text
    with pytest.raises(QueryError, match="tabela não encontrada: protocolo.fantasma"):
        T.table_access(conn, "Table:protocolo.fantasma")


def test_graph_without_menu_items_says_why(tmp_path: Path) -> None:
    db = build_db(tmp_path, "sem-menu")
    with closing(sqlite3.connect(db)) as connection:
        connection.execute("DELETE FROM edges WHERE type = 'IMPLEMENTS_ACTION'")
        connection.execute("DELETE FROM nodes WHERE label = 'BusinessAction'")
        result = T.table_access(connection, CGMTIPO)
    assert all(not t.menus for t in result.writers)
    assert all(t.menu_reason == T.NO_ITEMS for t in result.writers)


def test_item_without_readable_path_says_catalog_is_missing(tmp_path: Path) -> None:
    db = build_db(tmp_path, "sem-caminho")
    with closing(sqlite3.connect(db)) as connection:
        connection.execute(
            "UPDATE nodes SET attrs = json_remove(json_remove(attrs, '$.menu_path'),"
            " '$.menu_paths') WHERE id = 'BusinessAction:menu:101'"
        )
        result = T.table_access(connection, CGMTIPO)
    cadastro = _find(result.writers, f"File:{CADASTRO}")
    assert cadastro.menus == ()
    assert cadastro.menu_reason == T.NO_CATALOG


def test_json_has_every_field_and_lines_as_path_line(access: T.TableAccess) -> None:
    data = T.table_access_json(access)
    assert set(data) == {
        "table_id",
        "writers",
        "migrations",
        "writers_total",
        "writers_by_how",
        "readers_total",
        "readers",
        "readers_by_layer",
        "unresolved_menu",
        "checked_menus",
        "notes",
        "fills_available",
        "fills_total",
    }
    service = next(w for w in data["writers"] if w["node"] == SERVICE)
    assert service["lines"] == [
        {"path": SERVICE_PATH, "line": 2687},
        {"path": SERVICE_PATH, "line": 2974},
    ]
    assert {
        "node",
        "path",
        "symbol",
        "layer",
        "how",
        "operations",
        "dao_methods",
        "occurrences",
        "confidence",
        "menus",
        "menu_reason",
        "fills",
    } <= set(service)
    assert data["writers_by_how"] == {"dao": 5, "eloquent": 1, "sql_direto": 1}
    json.dumps(data)  # serializa


def test_markdown_sections_and_wording(access: T.TableAccess) -> None:
    text = T.render_table_access(access)
    assert text.startswith("## Quem grava e quem lê no código PHP")
    assert (
        "7 fonte(s) gravam em produção (5 pelo DAO, 1 por SQL direto, 1 usam o model Eloquent)"
        in text
    )
    assert "2 migração(ões) gravam à parte, fora da contagem" in text
    assert "A gravação pelo DAO grava a tabela inteira: o mapa não diz coluna por coluna." in text
    for heading in ("### Quem grava", "### Migrações (fora da contagem)", "### Quem lê"):
        assert heading in text
    assert "2687, 2974 (2 chamadas)" in text
    assert "725, 716 (2 chamadas)" in text
    assert "— (sem menu: nenhum menu chega neste fonte pelo mapa)" in text
    # o corte por palavras tira leitores, nunca a contagem
    short = T.render_table_access(access, max_words=120)
    assert "fonte(s) leem" in short
    assert len(short.split()) < len(text.split())
    # cabeçalho de outro nível (o dossiê de campo usa `##`; a página pode usar `###`)
    assert T.render_table_access(access, heading="###").startswith("### Quem grava e quem lê")


def test_writers_table_is_cut_with_the_total_kept(tmp_path: Path) -> None:
    db = build_db(tmp_path, "cortes", extra_writers=80)
    with closing(sqlite3.connect(db)) as connection:
        result = T.table_access(connection, CGMTIPO, max_menu_climbs=5)
    assert result.writers_total == 87
    assert len(result.writers) == 87  # o JSON traz todos
    text = T.render_table_access(result)
    assert "_e mais 27 fonte(s) que gravam (a lista completa está no JSON)_" in text
    assert "87 fonte(s) gravam em produção" in text
