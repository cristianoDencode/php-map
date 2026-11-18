"""Pendência `class_not_found` — só quando `module_source == "directory"`.

Mede quanto a convenção do perfil erra (`ClassResolver.expected_paths` previu um caminho que não
está entre os arquivos escaneados); nunca deve aparecer para o `ecidade` (regra existente,
`_SuffixClassResolver.expected_paths` prevê 3 sufixos para **qualquer** nome, o que encheria
`unresolved.jsonl` de pendências novas para toda classe nativa/de vendor).
"""

from pathlib import Path

from conftest import PhpRecords

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.profiles.ecidade import CLASS_RESOLVERS
from php_map.profiles.zend1 import class_resolvers_for
from php_map.resolve import resolve_php
from php_map.resolve.unresolved import UnresolvedWriter


def test_class_not_found_is_registered_for_the_directory_module_source(
    php: PhpRecords, tmp_path: Path
) -> None:
    # `cls` em minúsculas: é como o extrator de verdade sempre devolve.
    records = [
        php.record(
            "application/modules/fiscal/controllers/PedidoController.php",
            calls=[php.call("new", "__construct", cls="fiscal_model_inexistente")],
        )
    ]
    builder, unresolved = GraphBuilder(), UnresolvedWriter()
    settings = Settings(module_source="directory", class_resolvers=class_resolvers_for(tmp_path))
    resolve_php(records, builder, unresolved, settings)
    unresolved.close()

    assert unresolved.counts["class_not_found"] == 1
    [record] = [r for r in unresolved.records if r.kind == "class_not_found"]
    assert record.candidates == ["application/modules/fiscal/models/inexistente.php"]
    assert record.hint == "fiscal_model_inexistente"


def test_class_not_found_does_not_fire_when_the_predicted_file_exists(
    php: PhpRecords, tmp_path: Path
) -> None:
    """Achado real no scan de um projeto zend1: sem comparar sem diferenciar caixa, isto disparava
    `class_not_found` para `RelatorioPedidos.php`, que **existe** — ver `_match` e
    `CallResolver._register_class_not_found`."""
    records = [
        php.record("application/modules/fiscal/models/RelatorioPedidos.php"),
        php.record(
            "application/modules/fiscal/controllers/PedidoController.php",
            calls=[php.call("new", "__construct", cls="fiscal_model_relatoriopedidos")],
        ),
    ]
    builder, unresolved = GraphBuilder(), UnresolvedWriter()
    settings = Settings(module_source="directory", class_resolvers=class_resolvers_for(tmp_path))
    resolve_php(records, builder, unresolved, settings)
    unresolved.close()

    assert "class_not_found" not in unresolved.counts


def test_class_not_found_does_not_fire_for_a_third_party_folder_excluded_by_design(
    php: PhpRecords, tmp_path: Path
) -> None:
    """Achado no scan de um projeto zend1: `Zend_Registry` prevê `library/Zend/Registry.php`,
    que existe mas fica fora do scan (`third_party_dirs`) — a convenção acertou, não é erro."""
    records = [
        php.record(
            "application/modules/fiscal/controllers/PedidoController.php",
            calls=[php.call("new", "__construct", cls="zend_registry")],
        )
    ]
    builder, unresolved = GraphBuilder(), UnresolvedWriter()
    settings = Settings(
        module_source="directory",
        class_resolvers=class_resolvers_for(tmp_path),
        third_party_dirs=("library/Zend",),
    )
    resolve_php(records, builder, unresolved, settings)
    unresolved.close()

    assert "class_not_found" not in unresolved.counts


def test_class_not_found_never_fires_for_the_ecidade_profile(php: PhpRecords) -> None:
    records = [
        php.record(
            "func_x.php",
            calls=[php.call("new", "__construct", cls="Exception")],
        )
    ]
    builder, unresolved = GraphBuilder(), UnresolvedWriter()
    settings = Settings(module_source="catalog", class_resolvers=CLASS_RESOLVERS)
    resolve_php(records, builder, unresolved, settings)
    unresolved.close()

    assert "class_not_found" not in unresolved.counts
