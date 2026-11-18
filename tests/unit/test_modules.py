"""Módulo por regra de nome: `resolve/modules.py`."""

from collections import Counter

import pytest

from php_map.errors import GraphContractError
from php_map.graph.builder import GraphBuilder, StoredEdge
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    file_id,
    module_id,
    table_id,
)
from php_map.resolve.modules import ModuleStats, apply_naming_modules

MODULES = ("empenho", "compras", "esocial", "patrimonio")


def _builder() -> GraphBuilder:
    builder = GraphBuilder()
    for name in MODULES:
        builder.add_node(Node(module_id(name), NodeLabel.MODULE, {"name": name}))
    return builder


def _file(builder: GraphBuilder, path: str, *modules: str, **attrs: object) -> str:
    """`File` com `module_hint` do nome e `BELONGS_TO` do catálogo para `modules`."""
    name = path.rsplit("/", 1)[-1]
    hint = name[:3] if len(name) > 4 and name[3].isdigit() and name[4] == "_" else None
    node = file_id(path)
    builder.add_node(
        Node(node, NodeLabel.FILE, {"path": path, "module_hint": hint, "is_infra": False, **attrs})
    )
    for module in modules:
        _catalog_edge(builder, node, module)
    return node


def _table(builder: GraphBuilder, name: str, *modules: str, schema: str = "public") -> str:
    node = table_id(name, schema)
    builder.add_node(Node(node, NodeLabel.TABLE, {"name": name}))
    for module in modules:
        _catalog_edge(builder, node, module)
    return node


def _catalog_edge(builder: GraphBuilder, src: str, module: str) -> None:
    evidence = {"path": "db:db_sysarqmod", "key": "codmod=1,codarq=1"}
    builder.add_edge(
        Edge(src, module_id(module), EdgeType.BELONGS_TO, EdgeSource.ECIDADE_CATALOG, 1.0, evidence)
    )


def _naming(builder: GraphBuilder) -> dict[tuple[str, str], StoredEdge]:
    return {(e.src, e.dst): e for e in builder.iter_edges() if e.source == EdgeSource.NAMING_RULE}


def _screens(builder: GraphBuilder, prefix: str, *modules: str) -> None:
    for i, module in enumerate(modules):
        _file(builder, f"{prefix}1_tela{i:03d}.php", module)


# --------------------------------------------------------------------------- voto do prefixo


def test_majority_classifies_the_screens_without_module() -> None:
    builder = _builder()
    _screens(builder, "emp", *(["empenho"] * 4), "compras")
    orphan = _file(builder, "emp4_semmenu001.php")
    other = _file(builder, "pes4_outra001.php")  # prefixo sem votos

    stats = apply_naming_modules(builder)

    edges = _naming(builder)
    edge = edges[(orphan, module_id("empenho"))]
    assert edge.type == EdgeType.BELONGS_TO and edge.confidence == pytest.approx(0.8)
    assert edge.evidence == {"path": "emp4_semmenu001.php", "rule": "prefix:emp", "votes": "4/5"}
    assert len(edges) == 1 and all(src != other for src, _dst in edges)
    assert (stats.prefixes_voted, stats.screens_classified, stats.edges) == (1, 1, 1)
    assert stats.files_with_catalog == 5 and stats.files_without_module == 1  # pes4_outra001


def test_confidence_is_capped_at_0_9() -> None:
    builder = _builder()
    _screens(builder, "emp", *(["empenho"] * 10))
    orphan = _file(builder, "emp2_relatorio001.php")

    apply_naming_modules(builder)

    assert _naming(builder)[(orphan, module_id("empenho"))].confidence == 0.9


def test_fraction_exactly_0_6_is_accepted() -> None:
    builder = _builder()
    _screens(builder, "emp", "empenho", "empenho", "empenho", "compras", "esocial")
    orphan = _file(builder, "emp4_x001.php")

    apply_naming_modules(builder)

    assert _naming(builder)[(orphan, module_id("empenho"))].confidence == pytest.approx(0.6)


def test_tie_is_not_a_majority() -> None:
    builder = _builder()
    _screens(builder, "emp", *(["empenho"] * 3), *(["compras"] * 3))
    _file(builder, "emp4_x001.php")

    stats = apply_naming_modules(builder)

    assert _naming(builder) == {}
    assert (stats.prefixes_voted, stats.prefixes_no_majority) == (0, 1)


def test_less_than_five_screens_do_not_vote() -> None:
    builder = _builder()
    _screens(builder, "emp", *(["empenho"] * 4))
    _file(builder, "emp4_x001.php")

    stats = apply_naming_modules(builder)

    assert _naming(builder) == {}
    assert stats.prefixes_few_screens == 1 and stats.files_without_module == 1


def test_fraction_below_0_6_is_not_a_majority() -> None:
    builder = _builder()
    _screens(builder, "emp", "empenho", "empenho", "compras", "esocial", "patrimonio")
    _file(builder, "emp4_x001.php")

    stats = apply_naming_modules(builder)

    assert _naming(builder) == {}
    assert stats.prefixes_no_majority == 1


def test_screen_in_two_modules_votes_for_both() -> None:
    """Cada tela vota uma vez em cada módulo dela; a fração é votos / telas."""
    builder = _builder()
    for i in range(5):
        _file(builder, f"emp1_tela{i}.php", "empenho", *(["compras"] if i < 2 else []))
    orphan = _file(builder, "emp4_x001.php")

    apply_naming_modules(builder)

    edge = _naming(builder)[(orphan, module_id("empenho"))]
    assert edge.evidence["votes"] == "5/5" and edge.confidence == 0.9
    assert (orphan, module_id("compras")) not in _naming(builder)


def test_files_already_classified_infra_and_placeholders_are_skipped() -> None:
    builder = _builder()
    _screens(builder, "emp", *(["empenho"] * 5))
    in_menu = _file(builder, "emp4_menu001.php", "compras")
    infra = _file(builder, "emp4_infra001.php", is_infra=True)
    builder.ensure_node(file_id("emp4_ausente001.php"))  # include de arquivo que não existe

    stats = apply_naming_modules(builder)

    assert _naming(builder) == {}
    assert all(src not in (in_menu, infra) for src, _dst in _naming(builder))
    assert stats.files_with_catalog == 6 and stats.files_without_module == 0


# --------------------------------------------------------------------------- DAO → tabela


def test_dao_gets_the_module_of_its_table() -> None:
    builder = _builder()
    _table(builder, "empempenho", "empenho")
    dao = _file(builder, "classes/db_empempenho_classe.php")

    stats = apply_naming_modules(builder)

    edge = _naming(builder)[(dao, module_id("empenho"))]
    assert edge.confidence == 0.9
    assert edge.evidence == {"path": "classes/db_empempenho_classe.php", "rule": "dao:empempenho"}
    assert stats.daos_classified == 1 and stats.files_without_module == 0


def test_dao_with_two_modules_splits_the_confidence() -> None:
    builder = _builder()
    _table(builder, "db_menu", "empenho")
    _table(builder, "db_menu", "compras", schema="configuracoes")  # outro schema, outro módulo
    _table(builder, "db_menu", "empenho", schema="outro")  # mesmo módulo: uma aresta só
    dao = _file(builder, "classes/db_db_menu_classe.php")

    apply_naming_modules(builder)

    edges = _naming(builder)
    assert {dst: e.confidence for (_src, dst), e in edges.items()} == {
        module_id("compras"): pytest.approx(0.45),
        module_id("empenho"): pytest.approx(0.45),
    }
    assert all(src == dao for src, _dst in edges)


def test_dao_without_table_module_and_dao_in_the_menu() -> None:
    builder = _builder()
    _table(builder, "orcdotacao")  # tabela sem módulo no dicionário
    _table(builder, "empanulado", "empenho")
    _file(builder, "classes/db_orcdotacao_classe.php")
    _file(builder, "classes/db_empanulado_classe.php", "compras")  # já tem módulo do catálogo
    _file(builder, "outros/db_empanulado_classe.php")  # fora de classes/: não é DAO

    stats = apply_naming_modules(builder)

    assert _naming(builder) == {}
    assert stats.daos_without_module == 1 and stats.files_without_module == 2


# --------------------------------------------------------------------------- contrato


def test_no_new_module_and_naming_rule_below_1() -> None:
    builder = _builder()
    _screens(builder, "emp", *(["empenho"] * 5))
    _file(builder, "emp4_x001.php")
    _table(builder, "empempenho", "empenho")
    _file(builder, "classes/db_empempenho_classe.php")
    before = Counter(node.label for node in builder.iter_nodes())

    stats = apply_naming_modules(builder)

    assert Counter(node.label for node in builder.iter_nodes()) == before
    assert stats.edges == 2 and all(e.confidence < 1.0 for e in _naming(builder).values())
    with pytest.raises(GraphContractError):
        builder.add_edge(
            Edge(
                file_id("emp4_x001.php"),
                module_id("empenho"),
                EdgeType.BELONGS_TO,
                EdgeSource.NAMING_RULE,
                1.0,
                {"path": "emp4_x001.php"},
            )
        )


def test_without_catalog_nothing_happens() -> None:
    builder = GraphBuilder()
    _file(builder, "emp4_x001.php")

    stats = apply_naming_modules(builder)

    assert stats == ModuleStats(files_without_module=1, seconds=stats.seconds)
    assert _naming(builder) == {}
