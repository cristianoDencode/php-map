"""Fábricas de DAO e tipos de propriedades."""

from typing import Any

from conftest import PhpRecords

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder, StoredEdge
from php_map.ingest.records import PhpFileRecord
from php_map.resolve import resolve_php
from php_map.resolve.unresolved import UnresolvedWriter

SCOPE = "method:empenho::anular"


def _dao(php: PhpRecords, name: str, *methods: str) -> PhpFileRecord:
    cls = f"cl_{name}"
    return php.record(
        f"classes/db_{name}_classe.php",
        declares=[php.decl("class", cls)] + [php.decl("method", m, cls=cls) for m in methods],
    )


def _factory_call(
    php: PhpRecords, arg0: str | None, arg1: bool | None, argc: int, **kw: Any
) -> dict[str, Any]:
    call = php.call("static", "getdao", cls="db_utils", scope=SCOPE, **kw)
    return {**call, "arg0": arg0, "arg1": arg1, "argc": argc}


def _usar_dao(php: PhpRecords, arg0: str | None, arg1: bool | None, argc: int) -> dict[str, Any]:
    call = php.call("method", "usardao", var="this", receiver="empenho", scope=SCOPE)
    return {**call, "arg0": arg0, "arg1": arg1, "argc": argc}


def _origin(kind: str, **fields: Any) -> dict[str, Any]:
    return {"kind": kind, **fields}


def _run(records: list[PhpFileRecord]) -> tuple[dict[tuple[str, str, str], StoredEdge], Any, Any]:
    builder, writer = GraphBuilder(), UnresolvedWriter()
    stats = resolve_php(records, builder, writer, Settings())
    writer.close()
    return {(e.src, e.type.value, e.dst): e for e in builder.iter_edges()}, writer, stats


def _empenho(php: PhpRecords, calls: list[dict[str, Any]], **decl: Any) -> PhpFileRecord:
    return php.record(
        "classes/empenho.php",
        declares=[
            {**php.decl("class", "empenho"), **decl},
            php.decl("method", "anular", cls="empenho"),
            php.decl("method", "usardao", cls="empenho"),
        ],
        calls=calls,
    )


def test_get_dao_literal_instantiates_and_types_receiver(php: PhpRecords) -> None:
    origin = _origin(
        "static_call", **{"class": "db_utils"}, name="getdao", arg0="EmpNotaItem", argc=1
    )
    method_call = {
        **php.call("method", "sql_query_file", var="oDao", scope=SCOPE, line=7),
        "receiver_origin": origin,
    }
    records = [
        _dao(php, "empnotaitem", "sql_query_file"),
        _empenho(php, [_factory_call(php, "EmpNotaItem", None, 1, line=6), method_call]),
    ]
    edges, _writer, stats = _run(records)

    new = edges["Method:empenho::anular", "INSTANTIATES", "Class:cl_empnotaitem"]
    assert new.source == "PHP_AST" and new.evidence["via"] == "dao_factory:getdao"
    call = edges["Method:empenho::anular", "CALLS", "Method:cl_empnotaitem::sql_query_file"]
    assert call.confidence == 1.0 and call.evidence["via"] == "dao_factory:getdao"
    assert stats["dao_factory_resolved"] == 1 and stats["receiver_by_origin"] == 1


def test_non_literal_factory_is_dynamic_call_without_edge(php: PhpRecords) -> None:
    records = [_dao(php, "x"), _empenho(php, [_factory_call(php, None, None, 1)])]
    edges, writer, stats = _run(records)

    assert not any(t == "INSTANTIATES" for _s, t, _d in edges)
    assert [(r.kind, r.hint) for r in writer.records] == [("dynamic_call", "getdao")]
    assert stats["dao_factory_dynamic"] == 1


def test_usar_dao_without_instance_does_not_instantiate(php: PhpRecords) -> None:
    records = [
        _dao(php, "x"),
        _empenho(
            php,
            [
                _usar_dao(php, "x", False, 2),  # só require
                _usar_dao(php, "x", None, 1),  # padrão de usarDao: false
                _usar_dao(php, "x", None, 2),  # 2º argumento não literal: não arrisca
            ],
        ),
    ]
    edges, _writer, stats = _run(records)

    assert ("Method:empenho::anular", "INSTANTIATES", "Class:cl_x") not in edges
    assert stats["dao_factory_no_instance"] == 2 and stats["dao_factory_maybe"] == 1
    assert ("Method:empenho::anular", "CALLS", "Method:empenho::usardao") in edges


def test_usar_dao_true_types_the_variable(php: PhpRecords) -> None:
    origin = _origin(
        "method_call",
        receiver="this",
        receiver_class="empenho",
        name="usardao",
        arg0="x",
        arg1=True,
        argc=2,
    )
    incluir = {**php.call("method", "incluir", var="clx", scope=SCOPE), "receiver_origin": origin}
    records = [_dao(php, "x", "incluir"), _empenho(php, [_usar_dao(php, "x", True, 2), incluir])]
    edges, _writer, _stats = _run(records)

    assert edges["Method:empenho::anular", "INSTANTIATES", "Class:cl_x"].evidence["via"] == (
        "dao_factory:usardao"
    )
    assert ("Method:empenho::anular", "CALLS", "Method:cl_x::incluir") in edges


def test_factory_for_missing_dao_has_no_edge(php: PhpRecords) -> None:
    edges, _writer, stats = _run([_empenho(php, [_factory_call(php, "removida", None, 1)])])
    assert not any(d == "Class:cl_removida" for _s, _t, d in edges)
    assert stats["dao_factory_unknown_class"] == 1


def test_property_type_from_constructor(php: PhpRecords) -> None:
    prop = {"prop": "clx", "origin": _origin("new", **{"class": "cl_x"})}
    call = {**php.call("method", "sql_record", scope=SCOPE), "receiver_property": "clx"}
    records = [_dao(php, "x", "sql_record"), _empenho(php, [call], property_types=[prop])]
    edges, _writer, stats = _run(records)

    edge = edges["Method:empenho::anular", "CALLS", "Method:cl_x::sql_record"]
    assert edge.source == "PHP_AST" and edge.evidence["via"] == "property:clx"
    assert stats["receiver_by_property"] == 1


def test_property_with_two_types_is_ambiguous_and_inherited(php: PhpRecords) -> None:
    props = [
        {"prop": "clx", "origin": _origin("new", **{"class": "cl_x"})},
        {
            "prop": "clx",
            "origin": _origin(
                "static_call", **{"class": "db_utils"}, name="getdao", arg0="y", argc=1
            ),
        },
    ]
    call = {**php.call("method", "sql_record", scope="method:filha::m"), "receiver_property": "clx"}
    records = [
        _dao(php, "x", "sql_record"),
        _dao(php, "y", "sql_record"),
        php.record(
            "classes/filha.php",
            declares=[
                {**php.decl("class", "base"), "property_types": props},
                php.decl("class", "filha", extends="base"),
                php.decl("method", "m", cls="filha"),
            ],
            calls=[call],
        ),
    ]
    edges, _writer, _stats = _run(records)

    for cls in ("cl_x", "cl_y"):
        edge = edges["Method:filha::m", "CALLS", f"Method:{cls}::sql_record"]
        assert edge.source == "PHP_AST_AMBIGUOUS" and edge.confidence == 0.5
        assert edge.evidence["candidates"] == ["cl_x", "cl_y"]


def test_unknown_property_is_counted_with_readable_snippet(php: PhpRecords) -> None:
    call = {**php.call("method", "m", scope=SCOPE), "receiver_property": "nada"}
    _edges, writer, stats = _run([_empenho(php, [call])])
    assert stats["unknown_receiver"] == 1
    assert [r.snippet for r in writer.records if r.kind == "unknown_receiver"] == [
        "$this->nada->m()"
    ]


def test_includer_origin_from_factory(php: PhpRecords) -> None:
    origin = _origin("static_call", **{"class": "db_utils"}, name="getdao", arg0="x", argc=1)
    records = [
        _dao(php, "x", "sql_record"),
        php.record("tela.php", includes=[php.inc("forms/f.php", var_types={"clx": origin})]),
        php.record("forms/f.php", calls=[php.call("method", "sql_record", var="clx")]),
    ]
    edges, _writer, _stats = _run(records)

    edge = edges["File:forms/f.php", "CALLS", "Method:cl_x::sql_record"]
    assert edge.evidence["via_include"] == "tela.php"
    assert edge.evidence["via"] == "dao_factory:getdao"
