"""Rotas do Laravel → `Route` e `ROUTES_TO`, sem Docker.

Registros à mão no formato do extrator (schema_version 7): cada instrução de rota é uma cadeia
`Route::a(...)->b(...)` com argumentos simbólicos (`{t: "s", v}` texto, `{t: "fn", b}` closure,
`{t: "f", n: "base_path", a}`…). Os testes montam só o necessário com os atalhos abaixo.
"""

from typing import Any

from conftest import PhpRecords

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import EdgeType, Node, NodeLabel, method_id
from php_map.ingest.records import PhpFileRecord
from php_map.resolve.laravel_routes import apply_laravel_routes
from php_map.resolve.unresolved import UnresolvedWriter

BS = chr(92)


def s(value: str) -> dict[str, Any]:
    return {"t": "s", "v": value}


def var(name: str) -> dict[str, Any]:
    return {"t": "v", "n": name}


def cls(name: str) -> dict[str, Any]:
    return {"t": "k", "v": name}


def concat(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"t": "c", "p": list(parts)}


def base_path(path: str) -> dict[str, Any]:
    return {"t": "f", "n": "base_path", "a": [s(path)]}


def fn(*items: dict[str, Any]) -> dict[str, Any]:
    return {"t": "fn", "b": list(items)}


def arr(*items: tuple[dict[str, Any] | None, dict[str, Any]]) -> dict[str, Any]:
    return {"t": "a", "i": [{"k": k, "v": v} for k, v in items]}


def route(*chain: tuple[str, list[dict[str, Any]]], line: int = 1) -> dict[str, Any]:
    return {
        "kind": "route",
        "line": line,
        "chain": [{"name": name, "args": args} for name, args in chain],
    }


def assign(name: str, expr: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "assign", "var": name, "expr": expr, "line": 1}


def _record(php: PhpRecords, path: str, *items: dict[str, Any]) -> PhpFileRecord:
    return php.record(path, routes=list(items))


def _run(
    records: list[PhpFileRecord], builder: GraphBuilder | None = None
) -> tuple[GraphBuilder, UnresolvedWriter, Any]:
    builder = builder or GraphBuilder()
    unresolved = UnresolvedWriter()
    stats = apply_laravel_routes(records, builder, unresolved, Settings(laravel_routes=True))
    unresolved.close()
    return builder, unresolved, stats


def _routes(builder: GraphBuilder) -> dict[str, Any]:
    return {n.id: n.attrs for n in builder.iter_nodes() if n.label == NodeLabel.ROUTE}


def _provider(php: PhpRecords, prefix: str, include: str, **extra: Any) -> PhpFileRecord:
    """Raiz de teste: `Route::prefix(<prefix>)->group(base_path(<include>))`."""
    chain = [("prefix", [s(prefix)])] + list(extra.get("modifiers", []))
    chain.append(("group", [base_path(include)]))
    return _record(php, "app/Providers/RouteServiceProvider.php", route(*chain))


def _with_controller(builder: GraphBuilder, fqcn: str, *methods: str) -> None:
    for method in methods:
        node = method_id(fqcn, method)
        builder.add_node(Node(node, NodeLabel.METHOD, {"name": method}))


# ------------------------------------------------------------------ prefixos, URI e namespace


def test_prefix_is_accumulated_through_files_and_nested_groups(php: PhpRecords) -> None:
    api = _record(
        php,
        "routes/api.php",
        route(
            ("prefix", [s("assistencia")]),
            ("middleware", [arr((None, s("auth:api")))]),
            (
                "group",
                [
                    fn(
                        route(
                            ("prefix", [s("habitacao")]), ("group", [base_path("routes/api/h.php")])
                        )
                    )
                ],
            ),
        ),
    )
    habitacao = _record(php, "routes/api/h.php", route(("get", [s("lista"), s("Habitacao@index")])))

    builder, _unresolved, stats = _run([_provider(php, "v4/api", "routes/api.php"), api, habitacao])

    assert list(_routes(builder)) == ["Route:GET /v4/api/assistencia/habitacao/lista"]
    attrs = _routes(builder)["Route:GET /v4/api/assistencia/habitacao/lista"]
    assert attrs["middleware"] == ["auth:api"] and attrs["area"] == "api"
    assert (stats.roots, stats.reached_files, stats.routes) == (1, 3, 1)


def test_namespace_accumulates_and_a_leading_backslash_makes_it_absolute(php: PhpRecords) -> None:
    api = _record(
        php,
        "routes/api.php",
        route(
            ("prefix", [s("financeiro")]),
            ("namespace", [s(f"App{BS}Domain{BS}Financeiro{BS}{BS}")]),
            (
                "group",
                [
                    fn(
                        route(
                            ("prefix", [s("empenho")]),
                            ("namespace", [s(f"Empenho{BS}Controller{BS}{BS}")]),
                            (
                                "group",
                                [fn(route(("post", [s("update"), s("EmpenhoController@update")])))],
                            ),
                        ),
                        route(
                            ("namespace", [s(f"{BS}Outro{BS}Raiz")]),
                            ("group", [fn(route(("get", [s("x"), s("XController@show")])))]),
                        ),
                    )
                ],
            ),
        ),
    )
    builder = GraphBuilder()
    _with_controller(
        builder,
        f"App{BS}Domain{BS}Financeiro{BS}Empenho{BS}Controller{BS}EmpenhoController",
        "update",
    )
    _with_controller(builder, f"Outro{BS}Raiz{BS}XController", "show")

    builder, _unresolved, stats = _run([_provider(php, "api", "routes/api.php"), api], builder)

    targets = {e.dst for e in builder.iter_edges() if e.type == EdgeType.ROUTES_TO}
    assert targets == {
        f"Method:app{BS}domain{BS}financeiro{BS}empenho{BS}controller{BS}empenhocontroller::update",
        f"Method:outro{BS}raiz{BS}xcontroller::show",  # `\Outro\Raiz` ignora o namespace do pai
    }
    assert stats.routes_to == 2


def test_dynamic_segments_become_params_and_optional_ones_are_not_required(php: PhpRecords) -> None:
    api = _record(
        php,
        "routes/api.php",
        route(("get", [s("turma/{codigo}/aluno/{id?}"), s("TurmaController@buscar")])),
    )

    builder, _unresolved, _stats = _run([_provider(php, "v4", "routes/api.php"), api])

    [attrs] = _routes(builder).values()
    assert attrs["params"] == [
        {"name": "codigo", "required": True},
        {"name": "id", "required": False},
    ]
    assert attrs["uri"] == "/v4/turma/{codigo}/aluno/{id?}"


def test_same_file_included_with_two_prefixes_makes_one_route_per_final_uri(
    php: PhpRecords,
) -> None:
    provider = _record(
        php,
        "app/Providers/RouteServiceProvider.php",
        route(("prefix", [s("a")]), ("group", [base_path("routes/c.php")])),
        route(("prefix", [s("b")]), ("group", [base_path("routes/c.php")])),
    )
    common = _record(php, "routes/c.php", route(("get", [s("lista"), s("X@y")])))

    builder, _unresolved, stats = _run([provider, common])

    assert sorted(_routes(builder)) == ["Route:GET /a/lista", "Route:GET /b/lista"]
    assert stats.routes == 2


# ------------------------------------------------------------------ arquivos


def test_missing_base_path_file_is_a_pending_not_a_failure(php: PhpRecords) -> None:
    api = _record(
        php,
        "routes/api.php",
        route(("prefix", [s("custos")]), ("group", [base_path("routes/api/custos.php")]), line=33),
        route(("get", [s("ok"), s("A@b")])),
    )

    builder, unresolved, stats = _run([_provider(php, "v4", "routes/api.php"), api])

    assert "Route:GET /v4/ok" in _routes(builder)  # o resto do arquivo segue
    [pending] = [r for r in unresolved.records if r.kind == "route_file_missing"]
    assert (pending.path, pending.line, pending.snippet) == (
        "routes/api.php",
        33,
        "routes/api/custos.php",
    )
    assert stats.missing_files == 1


def test_inclusion_cycle_is_ignored(php: PhpRecords) -> None:
    a = _record(
        php,
        "routes/a.php",
        route(("group", [base_path("routes/b.php")])),
        route(("get", [s("a"), s("A@a")])),
    )
    b = _record(
        php,
        "routes/b.php",
        route(("group", [base_path("routes/a.php")])),
        route(("get", [s("b"), s("B@b")])),
    )

    builder, _unresolved, stats = _run([_provider(php, "x", "routes/a.php"), a, b])

    assert sorted(_routes(builder)) == ["Route:GET /x/a", "Route:GET /x/b"]
    assert stats.cycles == 1


def test_a_routes_file_no_root_reaches_is_counted_and_makes_no_route(php: PhpRecords) -> None:
    orphan = _record(php, "routes/orfa.php", route(("get", [s("solta"), s("O@o")])))

    builder, _unresolved, stats = _run([orphan])

    assert not _routes(builder)  # sem o prefixo de quem a inclui a URI não é conhecida
    assert (stats.roots, stats.unreached_files) == (0, 1)


# ------------------------------------------------------------------ destino da rota


def test_variable_controller_array_action_and_closure(php: PhpRecords) -> None:
    api = _record(
        php,
        "routes/api.php",
        assign("path", s(f"{BS}App{BS}Domain{BS}X{BS}{BS}")),
        assign("controller", concat(var("path"), s("YController"))),
        route(("post", [s("usuario"), concat(var("controller"), s("@entra"))])),
        route(
            (
                "get",
                [s("lista"), arr((None, cls(f"App{BS}Lib{BS}ZController")), (None, s("index")))],
            )
        ),
        route(("get", [s("ping"), fn()])),
    )
    builder = GraphBuilder()
    _with_controller(builder, f"App{BS}Domain{BS}X{BS}YController", "entra")
    _with_controller(builder, f"App{BS}Lib{BS}ZController", "index")

    builder, _unresolved, stats = _run([_provider(php, "v4", "routes/api.php"), api], builder)

    edges = {e.src: e.dst for e in builder.iter_edges() if e.type == EdgeType.ROUTES_TO}
    assert edges == {
        "Route:POST /v4/usuario": f"Method:app{BS}domain{BS}x{BS}ycontroller::entra",
        "Route:GET /v4/lista": f"Method:app{BS}lib{BS}zcontroller::index",
    }
    assert _routes(builder)["Route:GET /v4/ping"]["action"] == "closure"
    assert (stats.routes_to, stats.closures) == (2, 1)


def test_route_group_array_form_and_the_controller_modifier(php: PhpRecords) -> None:
    api = _record(
        php,
        "routes/api.php",
        route(
            (
                "group",
                [
                    arr((s("prefix"), s("auth")), (s("middleware"), arr((None, s("api"))))),
                    fn(route(("post", [s("usuario"), s("ClientController@entra")]))),
                ],
            )
        ),
        route(
            ("controller", [cls(f"App{BS}ProgressoController")]),
            ("prefix", [s("p")]),
            ("group", [fn(route(("get", [s("lista"), s("index")])))]),
        ),
    )
    builder = GraphBuilder()
    _with_controller(builder, "ClientController", "entra")
    _with_controller(builder, f"App{BS}ProgressoController", "index")

    builder, _unresolved, _stats = _run([_provider(php, "v4", "routes/api.php"), api], builder)

    assert _routes(builder)["Route:POST /v4/auth/usuario"]["middleware"] == ["api"]
    assert {e.src for e in builder.iter_edges() if e.type == EdgeType.ROUTES_TO} == {
        "Route:POST /v4/auth/usuario",
        "Route:GET /v4/p/lista",  # `index` + `controller()` do grupo
    }


def test_middleware_after_the_verb_is_only_for_that_route_and_never_repeats(
    php: PhpRecords,
) -> None:
    provider = _record(
        php,
        "app/Providers/RouteServiceProvider.php",
        route(
            ("prefix", [s("v4")]),
            ("middleware", [s("api")]),
            (
                "group",
                [
                    fn(
                        route(
                            ("put", [s("logout"), s("L@out")]),
                            ("middleware", [arr((None, s("api")), (None, s("auth:api")))]),
                        ),
                        route(("put", [s("kill"), s("L@kill")])),
                    )
                ],
            ),
        ),
    )

    builder, _unresolved, _stats = _run([provider])

    routes = _routes(builder)
    assert routes["Route:PUT /v4/logout"]["middleware"] == ["api", "auth:api"]  # `api` uma vez só
    assert routes["Route:PUT /v4/kill"]["middleware"] == ["api"]


def test_resource_and_api_resource_expand_to_the_standard_actions(php: PhpRecords) -> None:
    api = _record(
        php,
        "routes/api.php",
        route(("resource", [s("planos"), s("PlanoController")])),
        route(("apiResource", [s("itens"), s("ItemController")])),
        route(
            (
                "resource",
                [
                    s("so"),
                    s("SoController"),
                    arr((s("only"), arr((None, s("index")), (None, s("show"))))),
                ],
            )
        ),
    )

    builder, _unresolved, _stats = _run([_provider(php, "v4", "routes/api.php"), api])

    ids = sorted(_routes(builder))
    assert len([i for i in ids if "/planos" in i]) == 7
    assert len([i for i in ids if "/itens" in i]) == 5  # sem create/edit
    assert [i for i in ids if "/so" in i] == ["Route:GET /v4/so", "Route:GET /v4/so/{id}"]
    assert "Route:PUT /v4/planos/{id}" in ids


# ------------------------------------------------------------------ o que não se resolve


def test_non_literal_uri_or_prefix_is_a_pending_and_no_route(php: PhpRecords) -> None:
    api = _record(
        php,
        "routes/api.php",
        route(("get", [var("rotaDinamica"), s("A@b")]), line=9),
        route(("prefix", [var("p")]), ("group", [fn(route(("get", [s("x"), s("A@b")])))]), line=12),
        route(("get", [s("ok"), var("destinoDinamico")]), line=15),
    )

    builder, unresolved, stats = _run([_provider(php, "v4", "routes/api.php"), api])

    assert list(_routes(builder)) == ["Route:GET /v4/ok"]  # a rota existe; o destino não resolve
    assert unresolved.counts["route_dynamic"] == 3
    assert (stats.dynamic, stats.target_unresolved) == (2, 1)


def test_controller_the_code_does_not_declare_is_a_pending_and_no_edge(php: PhpRecords) -> None:
    api = _record(php, "routes/api.php", route(("get", [s("x"), s("FantasmaController@index")])))

    builder, unresolved, stats = _run([_provider(php, "v4", "routes/api.php"), api])

    assert "Route:GET /v4/x" in _routes(builder)
    assert not [e for e in builder.iter_edges() if e.type == EdgeType.ROUTES_TO]
    assert unresolved.counts["route_controller_missing"] == 1 and stats.controller_missing == 1


def test_a_method_inherited_from_the_base_controller_is_still_routed(php: PhpRecords) -> None:
    """`index/show/store/update` do CRUD vêm do controller-base: o `ROUTES_TO` segue o `EXTENDS`."""
    from php_map.graph.model import Edge, EdgeSource, class_id

    child, parent = f"App{BS}Notificacao", f"App{BS}Base"
    builder = GraphBuilder()
    _with_controller(builder, parent, "show")
    _with_controller(builder, child, "gerar")
    builder.add_edge(
        Edge(
            class_id(child),
            class_id(parent),
            EdgeType.EXTENDS,
            EdgeSource.PHP_AST,
            1.0,
            {"path": "x"},
        )
    )
    api = _record(
        php,
        "routes/api.php",
        route(("get", [s("n/{id}"), s(f"{child}@show")])),
        route(("get", [s("g"), s(f"{child}@gerar")])),
        route(("get", [s("z"), s(f"{child}@naoexiste")])),
    )

    builder, _unresolved, stats = _run([_provider(php, "v4", "routes/api.php"), api], builder)

    edges = {e.src: e for e in builder.iter_edges() if e.type == EdgeType.ROUTES_TO}
    assert edges["Route:GET /v4/n/{id}"].dst == f"Method:app{BS}base::show"  # do pai
    assert dict(edges["Route:GET /v4/n/{id}"].evidence)["inherited_from"] == f"app{BS}base"
    assert "inherited_from" not in edges["Route:GET /v4/g"].evidence  # do próprio controller
    assert "Route:GET /v4/z" not in edges  # nem ele nem os pais declaram
    assert (stats.routes_to, stats.inherited, stats.controller_missing) == (2, 1, 1)


def test_an_existing_route_file_with_no_routes_is_not_a_missing_file(php: PhpRecords) -> None:
    api = _record(php, "routes/api.php", route(("group", [base_path("routes/vazio.php")])))
    empty = php.record("routes/vazio.php")  # existe e não declara nenhuma rota

    _builder, unresolved, stats = _run([_provider(php, "v4", "routes/api.php"), api, empty])

    assert (stats.empty_files, stats.missing_files) == (1, 0)
    assert "route_file_missing" not in unresolved.counts
