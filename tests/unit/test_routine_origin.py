"""Origem das funções da base (menu, rota, endpoint, arquivo) e o motivo."""

import sqlite3
from typing import Any

from php_map.analysis import routine_origin as ro
from php_map.analysis.routine_origin import EdgeRecord, NodeRecord


class G:
    """Grafo de teste: `n()` cria nós e `e()` arestas, como tuplas simples."""

    def __init__(self) -> None:
        self.nodes: list[NodeRecord] = []
        self.edges: list[EdgeRecord] = []

    def n(self, node_id: str, **attrs: Any) -> str:
        self.nodes.append((node_id, node_id.split(":", 1)[0], attrs))
        return node_id

    def e(self, src: str, dst: str, kind: str, confidence: float = 1.0) -> None:
        self.edges.append((src, dst, kind, confidence))

    def run(self, **kwargs: Any) -> ro.RoutineOrigins:
        return ro.compute_origins(self.nodes, self.edges, **kwargs)


def fn(g: G, name: str, **attrs: Any) -> str:
    return g.n(f"PgFunction:public.{name}()", name=name, **attrs)


def menu(g: G, file: str, number: int, path: str, confidence: float = 1.0) -> str:
    action = g.n(f"BusinessAction:menu:{number}", menu_path=path)
    g.e(file, action, "IMPLEMENTS_ACTION", confidence)
    return action


def row(res: ro.RoutineOrigins, node_id: str) -> ro.RoutineRow:
    return next(r for r in res.routines if r.routine_id == node_id)


def entries(res: ro.RoutineOrigins, node_id: str, kind: str | None = None) -> list[ro.OriginEntry]:
    return [e for e in res.entries if e.routine_id == node_id and kind in (None, e.kind)]


def test_called_by_a_file_with_menu_is_distance_one() -> None:
    g = G()
    f = fn(g, "fc_a")
    screen = g.n("File:cad1_tela.php")
    g.e(screen, f, "CALLS_RPC")
    action = menu(g, screen, 1, "Cadastro > Procedimentos > Tela")

    res = g.run()

    assert row(res, f).status == ro.COM_ORIGEM
    (menu_entry,) = entries(res, f, "menu")
    assert (menu_entry.target, menu_entry.distance, menu_entry.label) == (
        action,
        1,
        "Cadastro > Procedimentos > Tela",
    )
    assert menu_entry.via == (screen, f) and menu_entry.confidence == 1.0
    assert [e.kind for e in entries(res, f)] == ["menu", "file"]  # o arquivo também é entrada
    assert (row(res, f).callers_php, row(res, f).callers_routines, row(res, f).triggers) == (
        1,
        0,
        0,
    )


def test_route_through_a_method() -> None:
    g = G()
    f = fn(g, "fc_a")
    method = g.n("Method:app\\controller::index")
    route = g.n("Route:GET /api/x", uri="/api/x")
    g.e(method, f, "CALLS_RPC")
    g.e(route, method, "ROUTES_TO")

    res = g.run()

    (route_entry,) = entries(res, f, "route")
    assert (route_entry.target, route_entry.distance, route_entry.label) == (route, 1, "/api/x")
    assert row(res, f).status == ro.COM_ORIGEM


def test_climb_continues_past_the_route_to_the_menu() -> None:
    """Método → rota (`ROUTES_TO`) → JS (`CALLS_HTTP`) → tela (`REFERENCES`) → menu.

    A rota continua sendo uma origem (`route`, distância 1), mas a subida não para nela."""
    g = G()
    f = fn(g, "fc_a")
    method = g.n("Method:app\\controller::index")
    route = g.n("Route:GET /api/x", uri="/api/x")
    js = g.n("File:scripts/x.js", kind="js")
    screen = g.n("File:cad1_tela.php")
    g.e(method, f, "CALLS_RPC")
    g.e(route, method, "ROUTES_TO")
    g.e(js, route, "CALLS_HTTP", 0.9)
    g.e(screen, js, "REFERENCES", 0.9)
    action = menu(g, screen, 7, "Cadastro > Tela")

    res = g.run()

    (route_entry,) = entries(res, f, "route")
    assert (route_entry.target, route_entry.distance) == (route, 1)
    (menu_entry,) = entries(res, f, "menu")
    assert (menu_entry.target, menu_entry.distance, menu_entry.confidence) == (action, 4, 0.9)
    assert menu_entry.via == (screen, js, route, method, f)
    assert row(res, f).status == ro.COM_ORIGEM


def test_route_without_a_caller_stays_a_route_origin() -> None:
    g = G()
    f = fn(g, "fc_a")
    method = g.n("Method:app\\controller::index")
    route = g.n("Route:GET /api/x", uri="/api/x")
    g.e(method, f, "CALLS_RPC")
    g.e(route, method, "ROUTES_TO")

    res = g.run()

    assert [e.kind for e in entries(res, f)] == ["route"]
    assert row(res, f).status == ro.COM_ORIGEM


def test_file_that_serves_an_endpoint() -> None:
    g = G()
    f = fn(g, "fc_a")
    rpc = g.n("File:cai3_x.RPC.php")
    endpoint = g.n("Endpoint:ecidade:rpc:cai3_x.RPC.php#acao", name="cai3_x.RPC.php#acao")
    g.e(rpc, f, "CALLS_RPC")
    g.e(rpc, endpoint, "SERVES")

    res = g.run()

    (found,) = entries(res, f, "endpoint")
    assert found.target == endpoint and found.label == "cai3_x.RPC.php#acao"
    assert row(res, f).status == ro.COM_ORIGEM


def test_through_another_function_is_distance_two_and_the_chain_is_listed() -> None:
    g = G()
    inner, outer = fn(g, "fc_inner"), fn(g, "fc_outer")
    screen = g.n("File:cad1_tela.php")
    g.e(outer, inner, "CALLS_RPC")
    g.e(screen, outer, "CALLS_RPC")
    menu(g, screen, 7, "Cadastro > Tela")

    res = g.run()

    (found,) = entries(res, inner, "menu")
    assert found.distance == 2 and found.via == (screen, outer, inner)
    assert row(res, inner).callers_routines == 1 and row(res, inner).callers_php == 0


def test_trigger_function_climbs_by_the_table_and_only_through_writers() -> None:
    g = G()
    trigger = fn(g, "fc_on_insert")
    table = g.n("Table:cadastro.iptubase")
    writer = g.n("File:cad1_grava.php")
    reader = g.n("File:cad1_lista.php")
    g.e(table, trigger, "TRIGGERS")
    g.e(writer, table, "INSERTS")
    g.e(reader, table, "QUERIES")
    menu(g, writer, 1, "Cadastro > Grava")
    menu(g, reader, 2, "Cadastro > Lista")

    res = g.run()

    found = entries(res, trigger, "menu")
    assert [e.label for e in found] == ["Cadastro > Grava"]  # a tela que só lê não aparece
    assert found[0].distance == 2 and found[0].via == (writer, table, trigger)
    assert {e.target for e in entries(res, trigger, "file")} == {writer}
    assert row(res, trigger).triggers == 1 and row(res, trigger).callers_php == 0


def test_orphans_and_chains_without_origin_get_a_status_and_a_reason() -> None:
    g = G()
    orphan, only_by_orphan = fn(g, "fc_orfa"), fn(g, "fc_so_por_orfa")
    caller = fn(g, "fc_chamadora")
    g.e(caller, only_by_orphan, "CALLS_RPC")

    res = g.run()

    assert row(res, orphan).status == ro.SEM_CHAMADOR
    assert "SQL dinâmico" in row(res, orphan).reason
    assert row(res, only_by_orphan).status == ro.CADEIA_SEM_ORIGEM
    assert row(res, caller).status == ro.SEM_CHAMADOR
    assert all(r.reason for r in res.routines)  # nenhuma função com o motivo vazio
    assert entries(res, orphan) == [] and entries(res, only_by_orphan) == []


def test_only_a_file_without_menu_route_or_endpoint() -> None:
    g = G()
    f = fn(g, "fc_a")
    g.e(g.n("File:lib.php"), f, "CALLS_RPC")

    res = g.run()

    assert row(res, f).status == ro.SO_ARQUIVO and entries(res, f, "menu") == []
    assert [e.kind for e in entries(res, f)] == ["file"]


def test_audit_function_is_generated_and_points_to_the_table_and_its_writers() -> None:
    g = G()
    audit = g.n("PgFunction:almox.fc_auditoria_tabela_ab12()", name="fc_auditoria_tabela_ab12")
    table = g.n("Table:almox.pedido")
    writer = g.n("File:alm1_pedido.php")
    g.e(table, audit, "TRIGGERS")
    g.e(writer, table, "UPDATES")
    menu(g, writer, 3, "Almoxarifado > Pedido")

    res = g.run()

    r = row(res, audit)
    assert r.status == ro.GERADA and r.generated is True
    assert r.reason == "auditoria da tabela almox.pedido"
    assert [e.label for e in entries(res, audit, "menu")] == ["Almoxarifado > Pedido"]


def test_confidence_of_the_origin_is_the_lowest_along_the_path() -> None:
    g = G()
    f = fn(g, "fc_a")
    middle = fn(g, "fc_meio")
    screen = g.n("File:cad1_tela.php")
    g.e(middle, f, "CALLS_RPC", 0.5)  # sobrecarga ambígua (1/n)
    g.e(screen, middle, "CALLS_RPC", 1.0)
    menu(g, screen, 1, "Cadastro > Tela", confidence=0.9)

    res = g.run()

    assert entries(res, f, "menu")[0].confidence == 0.5
    assert entries(res, middle, "menu")[0].confidence == 0.9  # a menor entre a chamada e o menu


def test_edges_below_the_minimum_confidence_are_not_climbed() -> None:
    g = G()
    f = fn(g, "fc_a")
    g.e(g.n("File:fraco.php"), f, "CALLS_RPC", 0.3)

    res = g.run()

    assert row(res, f).status == ro.CADEIA_SEM_ORIGEM  # há chamador, mas a aresta é fraca
    assert row(res, f).callers_php == 1 and entries(res, f) == []  # conta, mas não sobe


def test_per_kind_keeps_the_nearest_and_breaks_ties_by_id() -> None:
    g = G()
    f = fn(g, "fc_muito_usada")
    for i in (5, 3, 1, 4, 2, 6):  # seis telas diretas, mesma distância
        screen = g.n(f"File:t{i}.php")
        g.e(screen, f, "CALLS_RPC")
        menu(g, screen, i, f"Menu {i}")
    far = fn(g, "fc_longe")
    g.e(far, f, "CALLS_RPC")
    far_screen = g.n("File:longe.php")
    g.e(far_screen, far, "CALLS_RPC")
    menu(g, far_screen, 99, "Menu longe")

    res = g.run()

    menus = entries(res, f, "menu")
    assert [e.target for e in menus] == [f"BusinessAction:menu:{i}" for i in (1, 2, 3, 4, 5)]
    assert {e.distance for e in menus} == {1}  # o de distância 2 e o 6º ficam de fora
    assert row(res, f).callers_php == 6  # o total aparece na contagem, não na lista
    assert row(res, f).entries == len(entries(res, f))


def test_depth_limit_stops_the_climb() -> None:
    g = G()
    chain = [fn(g, f"fc_{i}") for i in range(4)]
    for callee, caller in zip(chain, chain[1:], strict=False):
        g.e(caller, callee, "CALLS_RPC")
    screen = g.n("File:tela.php")
    g.e(screen, chain[-1], "CALLS_RPC")
    menu(g, screen, 1, "Menu")

    assert entries(g.run(max_depth=4), chain[0], "menu")[0].distance == 4
    assert entries(g.run(max_depth=3), chain[0], "menu") == []


def test_infra_nodes_are_not_crossed() -> None:
    g = G()
    f = fn(g, "fc_a")
    wrapper = g.n("Method:db_query", is_infra=True)
    screen = g.n("File:tela.php")
    g.e(wrapper, f, "CALLS_RPC")
    g.e(screen, wrapper, "CALLS")
    menu(g, screen, 1, "Menu")

    res = g.run()

    assert row(res, f).status == ro.CADEIA_SEM_ORIGEM and entries(res, f) == []


def test_method_climbs_to_its_class_and_a_homonym_symbol_is_not_an_origin() -> None:
    g = G()
    f = fn(g, "fc_a")
    cls = g.n("Class:cl_dao")
    method = g.n("Method:cl_dao::buscar")
    lib = g.n("File:classes/db_cl_dao.php")
    screen = g.n("File:cad1_tela.php")
    g.e(lib, cls, "DECLARES")
    g.e(cls, method, "DECLARES")
    g.e(method, f, "CALLS_RPC")
    g.e(screen, cls, "INSTANTIATES")
    menu(g, screen, 1, "Menu")

    helper = g.n("Function:imprimirrodape")  # declarada em duas telas: homônimo
    other = fn(g, "fc_b")
    g.e(helper, other, "CALLS_RPC")
    for number in (2, 3):
        homonym = g.n(f"File:tela{number}.php")
        g.e(homonym, helper, "DECLARES")
        menu(g, homonym, number, f"Menu {number}")

    res = g.run()

    (found,) = entries(res, f, "menu")
    assert found.distance == 2 and found.via == (screen, cls, method, f)
    assert {e.target for e in entries(res, f, "file")} == {lib, screen}
    assert entries(res, other) == []  # nenhum menu inventado pelo homônimo
    assert row(res, other).status == ro.CADEIA_SEM_ORIGEM


def test_module_by_weighted_vote_and_null_without_modules() -> None:
    g = G()
    f = fn(g, "fc_a")
    near, far1, far2 = g.n("File:perto.php"), g.n("File:longe1.php"), g.n("File:longe2.php")
    mid = fn(g, "fc_meio")
    g.e(near, f, "CALLS_RPC")  # distância 1, peso 1/2
    g.e(mid, f, "CALLS_RPC")
    g.e(far1, mid, "CALLS_RPC")  # distância 2, peso 1/3 cada
    g.e(far2, mid, "CALLS_RPC")
    g.e(near, "Module:caixa", "BELONGS_TO")
    g.e(far1, "Module:cadastro", "BELONGS_TO")
    g.e(far2, "Module:cadastro", "BELONGS_TO")

    r = row(g.run(), f)

    assert r.module == "Module:cadastro"  # 2/3 contra 1/2
    assert r.module_confidence == round((2 / 3) / (2 / 3 + 1 / 2), 4)

    plain = G()
    only = fn(plain, "fc_b")
    plain.e(plain.n("File:x.php"), only, "CALLS_RPC")
    assert (row(plain.run(), only).module, row(plain.run(), only).module_confidence) == (None, None)


def test_placeholders_and_non_functions_are_not_listed() -> None:
    g = G()
    real = fn(g, "fc_real")
    g.n("PgFunction:public.fantasma()", name="fantasma", resolved=False)
    g.n("Table:public.t")

    assert [r.routine_id for r in g.run().routines] == [real]


def test_summary_counts_every_status() -> None:
    g = G()
    ok, orphan = fn(g, "fc_ok"), fn(g, "fc_orfa")
    screen = g.n("File:tela.php")
    g.e(screen, ok, "CALLS_RPC")
    menu(g, screen, 1, "Menu")
    g.e(screen, "Module:cadastro", "BELONGS_TO")

    summary = g.run().summary()

    assert summary["total"] == 2 == sum(summary[s] for s in ro.STATUSES)
    assert (summary[ro.COM_ORIGEM], summary[ro.SEM_CHAMADOR]) == (1, 1)
    assert summary["with_module"] == 1 and summary["truncated"] == 0
    assert orphan in {r.routine_id for r in g.run().routines}


# --------------------------------------------------------------------------- menções


def _texts_db(rows: list[tuple[str, str, str, int]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE search_texts (file_id, kind, text, line, scope)")
    conn.executemany("INSERT INTO search_texts VALUES (?, ?, ?, ?, '')", rows)
    return conn


def test_mentions_only_for_functions_without_origin_and_outside_native_ones() -> None:
    g = G()
    used = fn(g, "fc_usada")
    orphan = fn(g, "fc_formatadata")
    short = fn(g, "abc")
    native = fn(g, "array_sort")
    chain = fn(g, "fc_cadeia")
    g.e(g.n("File:tela.php"), used, "CALLS_RPC")
    g.e(orphan, chain, "CALLS_RPC")
    res = g.run()
    conn = _texts_db(
        [
            ("File:a.php", "literal", "select fc_formatadata(x)", 10),
            ("File:b.php", "comment", "usa FC_FORMATADATA para a data", 4),
            ("File:b.php", "comment", "de novo fc_formatadata", 9),  # mesmo arquivo: 1ª linha
            ("File:c.php", "html", "fc_formatadata no html", 1),  # só literal e comentário
            ("File:d.php", "literal", "fc_formatadata_outra()", 2),  # palavra inteira
            ("File:e.php", "literal", "fc_usada abc array_sort fc_cadeia", 3),
            ("PgFunction:public.x()", "literal", "fc_formatadata", 1),  # só arquivos
        ]
    )

    mentions = ro.find_mentions(conn, res, builtin_names=frozenset({"array_sort"}))

    by_routine = {(m.routine_id, m.file_id, m.line) for m in mentions}
    assert by_routine == {
        (orphan, "File:a.php", 10),
        (orphan, "File:b.php", 4),
        (chain, "File:e.php", 3),
    }
    assert all(m.confidence == 0.5 and m.source == "LITERAL_MENTION" for m in mentions)
    assert not any(m.routine_id in (used, short, native) for m in mentions)


def test_mentions_are_limited_per_function_and_need_the_texts_table() -> None:
    g = G()
    orphan = fn(g, "fc_formatadata")
    res = g.run()
    rows = [(f"File:f{i}.php", "literal", "fc_formatadata", i) for i in range(9)]

    assert len(ro.find_mentions(_texts_db(rows), res)) == ro.MENTION_FILES
    assert ro.find_mentions(sqlite3.connect(":memory:"), res) == ()  # sem search_texts
    assert orphan in {r.routine_id for r in res.routines}
