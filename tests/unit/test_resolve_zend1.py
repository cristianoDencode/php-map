"""Módulo por diretório, `Route` e `RENDERS` do perfil `zend1`.

Registros construídos à mão (sem Docker): `apply_zend1_structure` só olha `record.path` e
`record.declares`, então um `PhpFileRecord` mínimo é suficiente — mais rápido e mais preciso do
que extrair de verdade para testar só esta camada. `declares[].name`/`class` em **minúsculas**,
como o extrator de verdade sempre devolve (`CollectorVisitor::enterMethod`, PHP é
case-insensitive): é por isso que `apply_zend1_structure` tira o nome do controller do caminho
do arquivo, não da classe — ver o módulo para o porquê. Aqui `source=None` (sem reler o arquivo
fonte): como as actions de teste são de uma palavra só, o resultado é igual com ou sem a caixa
original (só nomes com mais de uma palavra perderiam o `kebab`, e isso é testado em
`tests/integration/test_zend1_fixture.py`, com o extrator de verdade).
"""

from pathlib import Path

from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    EdgeType,
    Node,
    NodeLabel,
    file_id,
    method_id,
    module_id,
    route_id,
)
from php_map.ingest.records import Declare, PhpFileRecord
from php_map.resolve.zend1 import apply_zend1_structure, kebab


def _record(path: str, declares: list[Declare] = []) -> PhpFileRecord:  # noqa: B006 (só leitura)
    return PhpFileRecord(
        schema_version=4,
        path=path,
        status="ok",
        errors=[],
        sha1=None,
        lines=1,
        declares=declares,
        includes=[],
        calls=[],
        dynamic=[],
        sql_strings=[],
        string_refs=[],
        dispatch=[],
    )


def _method(class_name: str, method_name: str, line: int = 5) -> Declare:
    return Declare(
        kind="method",
        name=method_name,
        class_=class_name,
        line_start=line,
        line_end=line + 2,
        extends=None,
        implements=[],
    )


def test_kebab_case_matches_the_real_zend1_examples() -> None:
    assert kebab("CancelamentoPedido") == "cancelamento-pedido"
    assert kebab("ZendApp") == "zend-app"
    assert kebab("Dms") == "dms"


def test_belongs_to_by_directory_for_each_module() -> None:
    builder = GraphBuilder()
    records = [
        _record("application/modules/fiscal/controllers/PedidoController.php"),
        _record("application/modules/webservice/models/Sample.php"),
        _record("application/entidades/Financeira/ArquivoAlerta.php"),
    ]
    stats = apply_zend1_structure(records, builder, None)

    assert stats.modules == 2
    assert stats.files_in_module == 2
    assert stats.files_outside_modules == 1
    assert module_id("fiscal") in builder
    assert module_id("webservice") in builder
    assert builder.node_attrs(module_id("fiscal"))["name"] == "fiscal"
    fiscal_edges = [
        e
        for e in builder.iter_edges()
        if e.type == EdgeType.BELONGS_TO and e.dst == module_id("fiscal")
    ]
    assert len(fiscal_edges) == 1
    assert fiscal_edges[0].source == "ZEND1_RESOLVER"
    assert fiscal_edges[0].confidence == 1.0
    # O arquivo de application/entidades/ não deve ganhar BELONGS_TO nenhum.
    assert not any(
        e.type == EdgeType.BELONGS_TO
        and e.src == file_id("application/entidades/Financeira/ArquivoAlerta.php")
        for e in builder.iter_edges()
    )


def test_route_and_renders_with_existing_view() -> None:
    builder = GraphBuilder()
    view = "application/modules/fiscal/views/scripts/cancelamento-pedido/consultar.phtml"
    builder.add_node(Node(file_id(view), NodeLabel.FILE, {"path": view, "name": "consultar.phtml"}))
    records = [
        _record(
            "application/modules/fiscal/controllers/CancelamentoPedidoController.php",
            [_method("fiscal_cancelamentopedidocontroller", "consultaraction")],
        ),
        _record(view),
    ]
    stats = apply_zend1_structure(records, builder, None)

    assert stats.routes == 1
    assert stats.renders == 1
    assert stats.orphan_views == 0
    route = route_id("fiscal/cancelamento-pedido/consultar")
    assert route in builder
    assert builder.node_attrs(route) == {
        "module": "fiscal",
        "controller": "cancelamento-pedido",
        "action": "consultar",
        "resolved": True,
    }
    dst = method_id("fiscal_cancelamentopedidocontroller", "consultaraction")
    routes_to = [e for e in builder.iter_edges() if e.type == EdgeType.ROUTES_TO]
    assert len(routes_to) == 1
    got = (routes_to[0].src, routes_to[0].dst, routes_to[0].source)
    assert got == (route, dst, "ZEND1_RESOLVER")
    renders = [e for e in builder.iter_edges() if e.type == EdgeType.RENDERS]
    assert len(renders) == 1
    assert (renders[0].src, renders[0].dst) == (dst, file_id(view))


def test_route_without_a_matching_view_has_no_renders() -> None:
    builder = GraphBuilder()
    records = [
        _record(
            "application/modules/fiscal/controllers/PedidoController.php",
            [_method("fiscal_pedidocontroller", "consultaaction")],
        )
    ]
    stats = apply_zend1_structure(records, builder, None)

    assert stats.routes == 1
    assert stats.renders == 0
    assert not any(e.type == EdgeType.RENDERS for e in builder.iter_edges())


def test_orphan_view_is_counted_when_no_action_renders_it() -> None:
    builder = GraphBuilder()
    orphan = "application/modules/fiscal/views/scripts/pagina-orfa/tela.phtml"
    builder.add_node(Node(file_id(orphan), NodeLabel.FILE, {"path": orphan, "name": "tela.phtml"}))
    stats = apply_zend1_structure([_record(orphan)], builder, None)

    assert stats.orphan_views == 1
    assert stats.renders == 0


def test_methods_outside_a_controller_class_are_not_routes() -> None:
    builder = GraphBuilder()
    records = [
        _record(
            "application/modules/fiscal/models/RelatorioPedidos.php",
            [_method("fiscal_model_relatoriopedidos", "buscaraction")],
        )
    ]
    stats = apply_zend1_structure(records, builder, None)
    assert stats.routes == 0


def test_original_method_name_is_recovered_from_the_source_line(tmp_path: Path) -> None:
    """Uma action de duas palavras (`buscaCepAction`) só vira `busca-cep` lendo a linha de
    verdade: `declares` já chega em minúsculas do extrator (`buscacepaction`), sem como separar
    as palavras. Sem o arquivo (`source=None`), o resultado degrada para `buscacepaction` sem
    dash — pior, mas a rota ainda é criada (ver o módulo `resolve/zend1.py`)."""
    path = "application/modules/fiscal/controllers/PedidoController.php"
    record = _record(path, [_method("fiscal_pedidocontroller", "buscacepaction")])

    without_source = GraphBuilder()
    apply_zend1_structure([record], without_source, None)
    assert route_id("fiscal/pedido/buscacep") in without_source

    controller = tmp_path / path
    controller.parent.mkdir(parents=True)
    controller.write_text(
        "<?php\nclass Fiscal_PedidoController\n{\n    public function buscaCepAction()\n"
        "    {\n    }\n}\n"
    )
    with_source = GraphBuilder()
    apply_zend1_structure(
        [_record(path, [_method("fiscal_pedidocontroller", "buscacepaction", line=4)])],
        with_source,
        tmp_path,
    )
    assert route_id("fiscal/pedido/busca-cep") in with_source
