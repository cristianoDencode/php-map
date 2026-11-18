"""symbols, calls e references sobre registros sintéticos."""

from typing import Any

from conftest import PhpRecords

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder, StoredEdge
from php_map.ingest.records import PhpFileRecord
from php_map.profiles.ecidade import CLASS_RESOLVERS
from php_map.resolve import resolve_php
from php_map.resolve.calls import origin_id
from php_map.resolve.includes import IncludeGraph, ResolvedInclude
from php_map.resolve.symbols import SymbolIndex
from php_map.resolve.unresolved import UnresolvedWriter


def _run(records: list[PhpFileRecord]) -> tuple[dict[tuple[str, str, str], StoredEdge], Any]:
    builder, writer = GraphBuilder(), UnresolvedWriter()
    stats = resolve_php(records, builder, writer, Settings())
    writer.close()
    edges = {(e.src, e.type.value, e.dst): e for e in builder.iter_edges()}
    return edges, (builder, writer, stats)


def test_origin_by_scope() -> None:
    assert origin_id("a.php", "") == "File:a.php"
    assert origin_id("a.php", "function:DB_Query") == "Function:db_query"
    assert origin_id("a.php", "method:cl_x::Incluir") == "Method:cl_x::incluir"


def test_function_resolution_rules(php: PhpRecords) -> None:
    index = SymbolIndex()
    for path in ("libs/a.php", "libs/b.php"):
        index.add_record(php.record(path, declares=[php.decl("function", "dup")]))
    index.add_record(php.record("libs/c.php", declares=[php.decl("function", "Unica")]))
    includes = IncludeGraph()
    includes.add(ResolvedInclude("tela.php", "libs/b.php", 1, {}))

    single = index.resolve_function("UNICA", "tela.php", includes)
    assert single is not None and single.candidates == ("libs/c.php",)
    by_closure = index.resolve_function("dup", "tela.php", includes)
    assert by_closure is not None and by_closure.candidates == ("libs/b.php",)
    ambiguous = index.resolve_function("dup", "dao.php", includes)
    assert ambiguous is not None and ambiguous.ambiguous and ambiguous.confidence == 0.5
    assert index.resolve_function("strlen", "tela.php", includes) is None


def test_class_autoload_tiebreak(php: PhpRecords) -> None:
    index = SymbolIndex(class_resolvers=CLASS_RESOLVERS)
    for path in ("classes/db_x_classe.php", "legacy/db_x_classe.php"):
        index.add_record(php.record(path, declares=[php.decl("class", "cl_x")]))
    for path in ("model/Conta.model.php", "model/Conta.service.php", "outro/conta.php"):
        index.add_record(php.record(path, declares=[php.decl("class", "Conta")]))
    for path in ("a/Dup.php", "b/Dup.php"):
        index.add_record(php.record(path, declares=[php.decl("class", "dup")]))
    includes = IncludeGraph()

    dao = index.resolve_class("CL_X", "tela.php", includes)
    assert dao is not None and dao.candidates == ("classes/db_x_classe.php",)
    model = index.resolve_class("conta", "tela.php", includes)
    assert model is not None and model.candidates == ("model/Conta.model.php",)
    dup = index.resolve_class("dup", "tela.php", includes)
    assert dup is not None and dup.ambiguous


def test_find_method_walks_inheritance_and_stops_on_cycles(php: PhpRecords) -> None:
    index = SymbolIndex()
    index.add_record(
        php.record(
            "a.php",
            declares=[
                php.decl("class", "base"),
                php.decl("method", "salvar", cls="base"),
                php.decl("class", "filha", extends="base"),
                php.decl("class", "ciclo1", extends="ciclo2"),
                php.decl("class", "ciclo2", extends="ciclo1"),
            ],
        )
    )
    assert index.find_method("Filha", "salvar") == "base"
    assert index.find_method("filha", "apagar") is None
    assert index.find_method("ciclo1", "x") is None


def test_calls_new_static_and_inheritance(php: PhpRecords) -> None:
    records = [
        php.record(
            "classes/base.php",
            declares=[
                php.decl("class", "base"),
                php.decl("method", "salvar", cls="base"),
                php.decl("class", "filha", extends="base", implements=["iface", "nativa"]),
                php.decl("interface", "iface"),
            ],
            calls=[
                php.call("method", "salvar", var="this", receiver="filha", scope="method:filha::x")
            ],
        ),
        php.record(
            "tela.php",
            calls=[
                php.call("new", "__construct", cls="Filha", line=3),
                php.call("static", "salvar", cls="filha", line=4),
                php.call("static", "naoexiste", cls="filha", line=5),
                php.call("func", "strlen", line=6),
                php.call("new", "__construct", cls="exception", line=7),
            ],
            dynamic=[{"kind": "variable_function", "snippet": "$f()", "line": 8, "scope": ""}],
        ),
    ]
    edges, (builder, writer, stats) = _run(records)

    assert ("File:tela.php", "INSTANTIATES", "Class:filha") in edges
    assert ("File:tela.php", "CALLS", "Method:base::salvar") in edges
    assert ("Method:filha::x", "CALLS", "Method:base::salvar") in edges
    assert ("Class:filha", "EXTENDS", "Class:base") in edges
    assert ("Class:filha", "IMPLEMENTS", "Class:iface") in edges
    assert not any(
        dst in ("Class:nativa", "Function:strlen", "Class:exception") for _, _, dst in edges
    )
    assert "Function:strlen" not in builder
    assert stats["unknown_function"] == 1 and stats["unknown_method"] == 1
    assert [(r.kind, r.snippet) for r in writer.records] == [("variable_function", "$f()")]


def test_method_via_includers(php: PhpRecords) -> None:
    dao = php.record(
        "classes/db_x_classe.php",
        declares=[php.decl("class", "cl_x"), php.decl("method", "sql_record", cls="cl_x")],
    )
    other = php.record(
        "classes/db_y_classe.php",
        declares=[php.decl("class", "cl_y"), php.decl("method", "sql_record", cls="cl_y")],
    )
    form_call = php.call("method", "sql_record", var="clx", line=9)
    unknown = php.call("method", "incluir", var="clz", line=10)
    records = [
        dao,
        other,
        php.record("tela1.php", includes=[php.inc("forms/f.php", var_types={"clx": "cl_x"})]),
        php.record("tela2.php", includes=[php.inc("forms/f.php", var_types={"clx": "cl_x"})]),
        php.record("tela3.php", includes=[php.inc("forms/g.php", var_types={"clx": "cl_x"})]),
        php.record("tela4.php", includes=[php.inc("forms/g.php", var_types={"clx": "cl_y"})]),
        php.record("forms/f.php", calls=[form_call, unknown]),
        php.record("forms/g.php", calls=[form_call]),
    ]
    edges, (_builder, writer, _stats) = _run(records)

    agreed = edges["File:forms/f.php", "CALLS", "Method:cl_x::sql_record"]
    assert agreed.source == "PHP_AST" and agreed.confidence == 1.0
    assert agreed.evidence["via_include"] == "tela1.php"

    for cls in ("cl_x", "cl_y"):
        edge = edges["File:forms/g.php", "CALLS", f"Method:{cls}::sql_record"]
        assert edge.source == "PHP_AST_AMBIGUOUS" and edge.confidence == 0.5
        assert edge.evidence["candidates"] == ["cl_x", "cl_y"]

    receivers = [r for r in writer.records if r.kind == "unknown_receiver"]
    assert [(r.path, r.snippet, r.count) for r in receivers] == [
        ("forms/f.php", "$clz->incluir()", 1)
    ]


def test_references_and_dispatch(php: PhpRecords) -> None:
    records = [
        php.record("rpc.php"),
        php.record(
            "forms/f.php",
            string_refs=[
                {"target": "rpc.php", "line": 3, "origin": "inline_html"},
                {"target": "rpc.php", "line": 9, "origin": "php_string"},
                {"target": "f.php", "line": 4, "origin": "php_string"},  # autorreferência
                {"target": "nao_existe.php", "line": 5, "origin": "php_string"},
            ],
            dispatch=[
                {
                    "subject": "$oParam->exec",
                    "cases": ["salvar", "excluir"],
                    "line": 1,
                    "scope": "",
                },
                {"subject": "$oParam->exec", "cases": ["salvar"], "line": 20, "scope": ""},
            ],
        ),
    ]
    edges, (builder, _writer, _stats) = _run(records)

    ref = edges["File:forms/f.php", "REFERENCES", "File:rpc.php"]
    assert ref.confidence == 0.9 and ref.attrs["occurrences"] == 2
    assert [key for key in edges if key[1] == "REFERENCES"] == [
        ("File:forms/f.php", "REFERENCES", "File:rpc.php")
    ]
    assert builder.node_attrs("File:forms/f.php")["dispatch_actions"] == ["salvar", "excluir"]


def test_property_typed_call_between_namespaced_layers(php: PhpRecords) -> None:
    """`$this->repository->index()` no service chega no repository (FQCN)."""
    repo = "app\\domain\\redesimimportarepository"
    service = "app\\domain\\redesimimportaservice"
    call = {
        **php.call("method", "index", var=None, scope=f"method:{service}::index"),
        "receiver_property": "repository",
    }
    records = [
        php.record(
            "Repository/RedeSimImportaRepository.php",
            declares=[php.decl("class", repo), php.decl("method", "index", cls=repo)],
        ),
        php.record(
            "Services/RedeSimImportaService.php",
            declares=[
                {
                    **php.decl("class", service),
                    "property_types": [
                        {"prop": "repository", "origin": {"kind": "new", "class": repo}}
                    ],
                },
                php.decl("method", "index", cls=service),
            ],
            calls=[call],
        ),
    ]
    edges, _ = _run(records)

    edge = edges[f"Method:{service}::index", "CALLS", f"Method:{repo}::index"]
    assert edge.source == "PHP_AST" and edge.confidence == 1.0
    assert edge.evidence["via"] == "property:repository"


def test_typed_call_with_unresolvable_class_becomes_unresolved(php: PhpRecords) -> None:
    """Tipo conhecido mas fora do índice não pode sumir em silêncio."""
    service = "app\\svc\\servico"
    call = {
        **php.call("method", "buscar", scope=f"method:{service}::run", line=12),
        "receiver_property": "repo",
    }
    records = [
        php.record(
            "Servico.php",
            declares=[
                {
                    **php.decl("class", service),
                    "property_types": [
                        {"prop": "repo", "origin": {"kind": "new", "class": "app\\repo\\sumido"}}
                    ],
                },
                php.decl("method", "run", cls=service),
            ],
            calls=[call],
        )
    ]
    _edges, (_builder, writer, _stats) = _run(records)

    [pending] = [r for r in writer.records if r.kind == "unknown_receiver"]
    assert (pending.path, pending.line, pending.snippet) == (
        "Servico.php",
        12,
        "$this->repo->buscar()",
    )
    assert pending.hint == "app\\repo\\sumido"


# ------------------------------------------------- injeção de dependência

CTRL = "app\\http\\ctrl"
SVC = "app\\svc\\servico"


def _param(cls: str, name: str = "s", method: str | None = "__construct") -> dict[str, Any]:
    return {"kind": "param", "class": cls, "name": name, "method": method}


def _di_records(php: PhpRecords, *extra: PhpFileRecord, **ctrl: Any) -> list[PhpFileRecord]:
    """Service com `index` + controller com o que o teste passar (declares/calls/bindings)."""
    return [
        php.record(
            "Svc/Servico.php",
            declares=[php.decl("class", SVC), php.decl("method", "index", cls=SVC)],
        ),
        *extra,
        php.record("Http/Ctrl.php", **ctrl),
    ]


def _ctrl_class(php: PhpRecords, origin: dict[str, Any], extends: str | None = None) -> Any:
    return {
        **php.decl("class", CTRL, extends=extends),
        "property_types": [{"prop": "servico", "origin": origin}],
    }


def _prop_call(php: PhpRecords, method: str = "index", owner: str = CTRL) -> dict[str, Any]:
    return {
        **php.call("method", "index", scope=f"method:{owner}::{method}", line=9),
        "receiver_property": "servico",
    }


def test_constructor_injection_types_the_property(php: PhpRecords) -> None:
    """`__construct(Servico $s)` + `$this->servico = $s` → `$this->servico->index()` é `CALLS`."""
    records = _di_records(
        php,
        declares=[_ctrl_class(php, _param(SVC)), php.decl("method", "index", cls=CTRL)],
        calls=[_prop_call(php)],
    )
    edges, (_b, writer, _s) = _run(records)

    edge = edges[f"Method:{CTRL}::index", "CALLS", f"Method:{SVC}::index"]
    assert edge.source == "PHP_AST" and edge.confidence == 1.0
    assert edge.evidence["via"] == "di:constructor" and edge.evidence["property"] == "servico"
    assert writer.records == []


def test_setter_injection_is_di_param(php: PhpRecords) -> None:
    """O parâmetro veio de outro método (`setServico`): a via é `di:param`, não construtor."""
    records = _di_records(
        php,
        declares=[_ctrl_class(php, _param(SVC, method="setservico"))],
        calls=[_prop_call(php)],
    )
    edges, _ = _run(records)
    assert edges[f"Method:{CTRL}::index", "CALLS", f"Method:{SVC}::index"].evidence["via"] == (
        "di:param"
    )


def test_inherited_constructor_injection(php: PhpRecords) -> None:
    """Controller herda o construtor tipado do `BaseController` (`property_origins` sobe)."""
    base = "app\\http\\basecontroller"
    records = _di_records(
        php,
        php.record("Http/Base.php", declares=[_ctrl_class(php, _param(SVC)) | {"name": base}]),
        declares=[php.decl("class", CTRL, extends=base)],
        calls=[_prop_call(php)],
    )
    edges, _ = _run(records)
    assert edges[f"Method:{CTRL}::index", "CALLS", f"Method:{SVC}::index"].evidence["via"] == (
        "di:constructor"
    )


def test_method_injection_and_container(php: PhpRecords) -> None:
    """`show(Servico $s)` + `$s->index()` → `di:param`; `app(Servico::class)->index()` →
    `di:container`."""
    calls = [
        {
            **php.call("method", "index", var="s", scope=f"method:{CTRL}::show", line=3),
            "receiver_origin": _param(SVC, method="show"),
        },
        {
            **php.call("method", "index", scope=f"method:{CTRL}::buscar", line=5),
            "receiver_origin": {"kind": "container", "class": SVC},
        },
    ]
    edges, _ = _run(_di_records(php, declares=[php.decl("class", CTRL)], calls=calls))

    show = edges[f"Method:{CTRL}::show", "CALLS", f"Method:{SVC}::index"]
    buscar = edges[f"Method:{CTRL}::buscar", "CALLS", f"Method:{SVC}::index"]
    assert (show.source, show.confidence, show.evidence["via"]) == ("PHP_AST", 1.0, "di:param")
    assert (buscar.source, buscar.evidence["via"]) == ("PHP_AST", "di:container")


def test_vendor_type_is_ignored_without_pending(php: PhpRecords) -> None:
    """`Request $request` (fora do código escaneado): nem aresta nem pendência."""
    call = {
        **php.call("method", "input", var="request", scope=f"method:{CTRL}::show"),
        "receiver_origin": _param("illuminate\\http\\request", "request", "show"),
    }
    edges, (_b, writer, stats) = _run(_di_records(php, calls=[call]))
    assert not [k for k in edges if k[0] == f"Method:{CTRL}::show"]
    assert writer.records == [] and stats["di_external_type"] == 1


IFACE = "app\\contracts\\fila"


def _iface_call(php: PhpRecords) -> dict[str, Any]:
    return {
        **php.call("method", "index", scope=f"method:{CTRL}::run", line=7),
        "receiver_origin": {"kind": "container", "class": IFACE},
    }


def _impl(php: PhpRecords, name: str, path: str) -> PhpFileRecord:
    return php.record(
        path,
        declares=[
            php.decl("class", name, implements=[IFACE]),
            php.decl("method", "index", cls=name),
        ],
    )


def _iface(php: PhpRecords) -> PhpFileRecord:
    return php.record(
        "Contracts/Fila.php",
        declares=[php.decl("interface", IFACE), php.decl("method", "index", cls=IFACE)],
    )


def test_interface_with_literal_bind(php: PhpRecords) -> None:
    """`->bind(Fila::class, FilaRedis::class)` em qualquer arquivo → 1,0, `via = di:bind`."""
    redis, sqs = "app\\fila\\redis", "app\\fila\\sqs"
    provider = php.record(
        "Providers/App.php", bindings=[{"abstract": IFACE, "concrete": redis, "line": 21}]
    )
    records = _di_records(
        php,
        _iface(php),
        _impl(php, redis, "Fila/Redis.php"),
        _impl(php, sqs, "Fila/Sqs.php"),
        provider,
        calls=[_iface_call(php)],
    )
    edges, _ = _run(records)

    edge = edges[f"Method:{CTRL}::run", "CALLS", f"Method:{redis}::index"]
    assert (edge.source, edge.confidence) == ("PHP_AST", 1.0)
    assert edge.evidence["via"] == "di:bind" and edge.evidence["injected"] == "di:container"
    assert edge.evidence["bound_in"] == "Providers/App.php:21"
    assert edge.evidence["abstract"] == IFACE
    assert (f"Method:{CTRL}::run", "CALLS", f"Method:{sqs}::index") not in edges


def test_interface_without_literal_bind_uses_implementers(php: PhpRecords) -> None:
    """Bind com closure (`concrete = null`): as concretas (inclusive filha de uma abstrata) 1/n."""
    redis, base, sqs = "app\\fila\\redis", "app\\fila\\base", "app\\fila\\sqs"
    abstract_base = php.record(
        "Fila/Base.php",
        declares=[{**php.decl("class", base, implements=[IFACE]), "abstract": True}],
    )
    child = php.record(
        "Fila/Sqs.php",
        declares=[php.decl("class", sqs, extends=base), php.decl("method", "index", cls=sqs)],
    )
    closure = php.record(
        "Svc/Salvar.php", bindings=[{"abstract": IFACE, "concrete": None, "line": 21}]
    )
    records = _di_records(
        php,
        _iface(php),
        _impl(php, redis, "Fila/Redis.php"),
        abstract_base,
        child,
        closure,
        calls=[_iface_call(php)],
    )
    edges, _ = _run(records)

    for impl in (redis, sqs):
        edge = edges[f"Method:{CTRL}::run", "CALLS", f"Method:{impl}::index"]
        assert (edge.source, edge.confidence) == ("PHP_AST_AMBIGUOUS", 0.5)
        assert edge.evidence["candidates"] == [redis, sqs]
        assert edge.evidence["via"] == "di:container" and edge.evidence["abstract"] == IFACE
    assert (f"Method:{CTRL}::run", "CALLS", f"Method:{base}::index") not in edges


def test_interface_with_single_implementer_is_never_certain(php: PhpRecords) -> None:
    """Uma concreta só, sem bind literal: `PHP_AST_AMBIGUOUS` 0,5 (o contrato veta 1,0)."""
    redis = "app\\fila\\redis"
    records = _di_records(
        php, _iface(php), _impl(php, redis, "Fila/Redis.php"), calls=[_iface_call(php)]
    )
    edges, _ = _run(records)
    edge = edges[f"Method:{CTRL}::run", "CALLS", f"Method:{redis}::index"]
    assert (edge.source, edge.confidence, edge.evidence["candidates"]) == (
        "PHP_AST_AMBIGUOUS",
        0.5,
        [redis],
    )


def test_interface_without_bind_nor_implementer_is_pending(php: PhpRecords) -> None:
    """Sem binding literal nem concreta: `di_unbound_interface` (e não `unknown_receiver`)."""
    edges, (_b, writer, _s) = _run(_di_records(php, _iface(php), calls=[_iface_call(php)]))

    assert not [k for k in edges if k[0] == f"Method:{CTRL}::run"]
    [pending] = writer.records
    assert (pending.kind, pending.path, pending.line, pending.hint, pending.candidates) == (
        "di_unbound_interface",
        "Http/Ctrl.php",
        7,
        IFACE,
        [],
    )
    assert pending.snippet == "<expr>->index()"


def test_interface_candidates_keep_only_concretes_with_the_method(php: PhpRecords) -> None:
    """Das concretas, só as que declaram (ou herdam) o método chamado entram no `1/n`."""
    redis, sqs, memoria = "app\\fila\\redis", "app\\fila\\sqs", "app\\fila\\memoria"
    sem_index = php.record(
        "Fila/Memoria.php",
        declares=[
            php.decl("class", memoria, implements=[IFACE]),
            php.decl("method", "outro", cls=memoria),
        ],
    )
    records = _di_records(
        php,
        _iface(php),
        _impl(php, redis, "Fila/Redis.php"),
        _impl(php, sqs, "Fila/Sqs.php"),
        sem_index,
        calls=[_iface_call(php)],
    )
    edges, (_b, _w, stats) = _run(records)

    targets = sorted(k[2] for k in edges if k[0] == f"Method:{CTRL}::run")
    assert targets == [f"Method:{redis}::index", f"Method:{sqs}::index"]
    edge = edges[f"Method:{CTRL}::run", "CALLS", f"Method:{redis}::index"]
    assert (edge.confidence, edge.evidence["candidates"]) == (0.5, [redis, sqs])
    assert stats["unknown_method"] == 0
