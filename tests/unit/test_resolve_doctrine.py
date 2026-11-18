"""`@Entity` → tabela (`MAPS_TO`) e DQL → tabelas, sem Docker.

Registros à mão, no formato do extrator (schema_version 5): `declares[].doc_comment` e
`declares[].properties` trazem os docblocks crus e o nome da classe vem em minúsculas, como o
extrator de verdade sempre devolve (PHP é case-insensitive).
"""

from typing import Any

from conftest import PhpRecords

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import EdgeSource, EdgeType, class_id, key_of
from php_map.ingest.records import PhpFileRecord
from php_map.ingest.table_directory import TableDirectory
from php_map.resolve.doctrine import (
    EntityTable,
    apply_doctrine,
    dql_references,
    parse_annotations,
)
from php_map.resolve.sql_strings import resolve_sql
from php_map.resolve.unresolved import UnresolvedWriter

ENTITY_DOC = '/**\n * @Entity\n * @Table(name="desif_arquivo_alerta")\n */'
BS = chr(92)  # barra invertida: evita escapes em strings PHP/DQL dentro dos testes


def _entity(
    php: PhpRecords,
    name: str,
    doc: str | None,
    properties: list[tuple[str, str]] | None = None,
    path: str = "application/entidades/Financeira/ArquivoAlerta.php",
    line: int = 11,
) -> PhpFileRecord:
    declare: dict[str, Any] = {
        **php.decl("class", name, line=line),
        "doc_comment": doc,
        "properties": [
            {"name": prop, "line": line + 5, "doc_comment": text} for prop, text in properties or []
        ],
    }
    return php.record(path, declares=[declare])


def _directory(*tables: tuple[str, str]) -> TableDirectory:
    directory = TableDirectory(("public",))
    for schema, name in tables:
        directory.add(schema, name)
    return directory


def _run(
    records: list[PhpFileRecord], directory: TableDirectory | None = None
) -> tuple[GraphBuilder, UnresolvedWriter, Any, dict[str, EntityTable]]:
    builder, unresolved = GraphBuilder(), UnresolvedWriter()
    stats, entities = apply_doctrine(records, builder, unresolved, Settings(), directory)
    unresolved.close()
    return builder, unresolved, stats, entities


def _edges(builder: GraphBuilder, type_: EdgeType) -> dict[tuple[str, str], Any]:
    return {(e.src, e.dst): e for e in builder.iter_edges() if e.type == type_}


# ------------------------------------------------------------------ anotações


def test_parse_annotations_reads_simple_and_multiline_arguments() -> None:
    doc = (
        "/**\n * @var int\n * @Id\n"
        '   * @Column(\n   *   type="text",\n   *   nullable=false\n   * )\n'
        '   * @ORM\\Table(name="x")\n   */'
    )
    notes = {a.name: a.args for a in parse_annotations(doc)}
    assert notes["Id"] == ""
    assert 'type="text"' in notes["Column"] and "nullable=false" in notes["Column"]
    assert notes["Table"] == 'name="x"'  # `@ORM\Table` vale como `@Table`


def test_parse_annotations_respects_parentheses_inside_quotes() -> None:
    notes = parse_annotations('/** @Column(type="string", options={"comment": "a)b"}) */')
    assert notes[0].args == 'type="string", options={"comment": "a)b"}'


def test_parse_annotations_reads_several_annotations_on_one_line() -> None:
    """`/** @Id @Column(type="integer") */` é comum em projetos Zend 1 com Doctrine."""
    notes = {a.name: a.args for a in parse_annotations('/** @Id @Column(type="integer") */')}
    assert notes == {"Id": "", "Column": 'type="integer"'}


def test_parse_annotations_ignores_email_addresses() -> None:
    assert parse_annotations("/** Contato: suporte@exemplo.com.br */") == []


def test_one_line_docblock_with_id_and_column_makes_a_column(php: PhpRecords) -> None:
    properties = [("id_dms", '/** @Id @Column(type="integer") */')]
    record = _entity(php, "contribuinte" + BS + "guiadms", ENTITY_DOC, properties)

    builder, _unresolved, _stats, _entities = _run([record])

    columns = builder.node_attrs(class_id("contribuinte" + BS + "guiadms"))["doctrine_columns"]
    assert columns == {"id_dms": {"type": "integer", "id": True, "generated": False}}


# ------------------------------------------------------------------ MAPS_TO


def test_entity_with_explicit_table_maps_with_confidence_one(php: PhpRecords) -> None:
    record = _entity(php, "financeira" + BS + "arquivoalerta", ENTITY_DOC)
    directory = _directory(("public", "desif_arquivo_alerta"))

    builder, unresolved, stats, entities = _run([record], directory)

    edge = _edges(builder, EdgeType.MAPS_TO)[
        (class_id("financeira" + BS + "arquivoalerta"), "Table:public.desif_arquivo_alerta")
    ]
    assert edge.confidence == 1.0
    assert edge.source == EdgeSource.DOCTRINE_ANNOTATION
    assert dict(edge.evidence) == {
        "path": "application/entidades/Financeira/ArquivoAlerta.php",
        "line": 11,
    }
    assert (stats.entities, stats.mapped_explicit, stats.mapped_convention) == (1, 1, 0)
    assert entities["financeira" + BS + "arquivoalerta"] == EntityTable(
        "Table:public.desif_arquivo_alerta", 1.0
    )
    assert "doctrine_table_missing" not in unresolved.counts


def test_entity_without_table_uses_the_class_name_with_confidence_point_eight(
    php: PhpRecords,
) -> None:
    record = _entity(php, "geral" + BS + "estado", "/**\n * @Entity\n */")

    builder, _unresolved, stats, entities = _run([record], _directory(("public", "estado")))

    edge = _edges(builder, EdgeType.MAPS_TO)[
        (class_id("geral" + BS + "estado"), "Table:public.estado")
    ]
    assert edge.confidence == 0.8
    assert edge.evidence["rule"] == "doctrine:classname"
    assert (stats.mapped_explicit, stats.mapped_convention) == (0, 1)
    assert entities["geral" + BS + "estado"].confidence == 0.8


def test_table_name_with_embedded_schema_is_split_at_the_dot(php: PhpRecords) -> None:
    doc = '/**\n * @Entity\n * @Table(name="migracao_nota.pessoa")\n */'
    record = _entity(php, "migracao" + BS + "pessoa", doc)

    builder, _unresolved, _stats, _entities = _run(
        [record], _directory(("migracao_nota", "pessoa"))
    )

    assert (class_id("migracao" + BS + "pessoa"), "Table:migracao_nota.pessoa") in _edges(
        builder, EdgeType.MAPS_TO
    )


def test_class_with_docblock_but_without_entity_is_ignored(php: PhpRecords) -> None:
    record = _entity(php, "x" + BS + "servico", "/**\n * Serviço comum, sem mapeamento.\n */")

    builder, _unresolved, stats, entities = _run([record])

    assert not _edges(builder, EdgeType.MAPS_TO)
    assert stats.entities == 0 and entities == {}


def test_entity_with_a_table_missing_from_the_schema_is_reported(php: PhpRecords) -> None:
    record = _entity(php, "financeira" + BS + "arquivoalerta", ENTITY_DOC)

    builder, unresolved, stats, _entities = _run([record], _directory(("public", "outra")))

    # A aresta existe do mesmo jeito, apontando para o placeholder (evidência de entidade obsoleta).
    edge = _edges(builder, EdgeType.MAPS_TO)[
        (class_id("financeira" + BS + "arquivoalerta"), "Table:public.desif_arquivo_alerta")
    ]
    assert edge.confidence == 1.0
    assert stats.table_missing == 1
    [pending] = [r for r in unresolved.records if r.kind == "doctrine_table_missing"]
    assert pending.candidates == ["Table:public.desif_arquivo_alerta"]


def test_no_pending_for_missing_tables_when_no_schema_was_read(php: PhpRecords) -> None:
    record = _entity(php, "financeira" + BS + "arquivoalerta", ENTITY_DOC)

    _builder, unresolved, stats, _entities = _run([record], None)

    assert stats.table_missing == 0
    assert "doctrine_table_missing" not in unresolved.counts


def test_doctrine_columns_come_from_the_property_docblocks(php: PhpRecords) -> None:
    properties = [
        ("id", '/**\n * @var int\n * @Id\n * @Column(type="integer")\n * @GeneratedValue\n */'),
        ("id_arquivo", '/**\n * @var int\n * @Column(type="integer", name="arquivo_id")\n */'),
        ("descricao", '/**\n * @Column(\n *   type="text",\n *   nullable=false\n * )\n */'),
        ("sem_coluna", "/**\n * @var string\n */"),  # só `@var`: não inventa coluna
        ("estado", '/**\n * @ManyToOne(targetEntity="Estado")\n */'),  # associação
    ]
    record = _entity(php, "financeira" + BS + "arquivoalerta", ENTITY_DOC, properties)

    builder, _unresolved, stats, _entities = _run(
        [record], _directory(("public", "desif_arquivo_alerta"))
    )

    columns = builder.node_attrs(class_id("financeira" + BS + "arquivoalerta"))["doctrine_columns"]
    assert columns == {
        "id": {"type": "integer", "id": True, "generated": True},
        "id_arquivo": {"type": "integer", "id": False, "generated": False, "column": "arquivo_id"},
        "descricao": {"type": "text", "id": False, "generated": False},
    }
    assert stats.columns == 3


# ------------------------------------------------------------------ DQL


def _dql(*lines: str) -> list[dict[str, Any]]:
    return [
        {"text": text, "complete": True, "via": "assignment", "line": number, "scope": ""}
        for number, text in enumerate(lines, start=10)
    ]


def _resolve(
    sql_strings: list[dict[str, Any]],
    entities: dict[str, EntityTable] | None,
    directory: TableDirectory | None = None,
) -> tuple[GraphBuilder, UnresolvedWriter, Any]:
    builder, unresolved = GraphBuilder(), UnresolvedWriter()
    php = PhpRecords()
    record = php.record("application/modules/fiscal/models/CadastroDeContasCosif.php")
    record = record.model_copy(update={"sql_strings": [_sql(s) for s in sql_strings]})
    stats = resolve_sql([record], builder, unresolved, Settings(), directory, entities)
    unresolved.close()
    return builder, unresolved, stats


def _sql(raw: dict[str, Any]) -> Any:
    from php_map.ingest.records import SqlString

    return SqlString.model_validate(raw)


ENTITIES = {
    "contribuinte" + BS + "planocontaabrasf": EntityTable("Table:public.plano_conta_abrasf", 1.0),
    "contribuinte" + BS + "dmsnota": EntityTable("Table:public.dms_nota", 1.0),
    "geral" + BS + "estado": EntityTable("Table:public.estado", 0.8),
}
FILE = "File:application/modules/fiscal/models/CadastroDeContasCosif.php"


def test_dql_references_by_verb() -> None:
    cp, dn = "Contribuinte" + BS + "PlanoContaAbrasf", "Contribuinte" + BS + "DmsNota"
    select = dql_references(f"SELECT c FROM {cp} c WHERE c.conta = :n")
    assert [(r.entity, r.op) for r in select] == [
        ("contribuinte" + BS + "planocontaabrasf", EdgeType.QUERIES)
    ]
    update = dql_references(f"UPDATE {cp} c SET c.x = 1 WHERE c.id IN (SELECT d.id FROM {dn} d)")
    assert [(r.entity.split(BS)[1], r.op) for r in update] == [
        ("planocontaabrasf", EdgeType.UPDATES),
        ("dmsnota", EdgeType.QUERIES),
    ]
    delete = dql_references(f"DELETE FROM {dn} d WHERE d.id = 1")
    assert [(r.entity.split(BS)[1], r.op) for r in delete] == [("dmsnota", EdgeType.DELETES)]
    assert [r.op for r in dql_references(f"DELETE {dn} d")] == [EdgeType.DELETES]
    comma = dql_references(f"SELECT a FROM {cp} a, {dn} b INNER JOIN Geral{BS}Estado e WITH 1 = 1")
    assert [(r.entity.split(BS)[1], r.op) for r in comma] == [
        ("planocontaabrasf", EdgeType.QUERIES),
        ("dmsnota", EdgeType.QUERIES),
        ("estado", EdgeType.QUERIES),
    ]


def test_real_sql_is_never_taken_for_dql() -> None:
    assert dql_references("select x.id from dms_nota x where x.id = 1") == []
    assert dql_references("select * from notas n inner join dms_nota d on d.id = n.id") == []
    assert dql_references("update notas set a = 1 where id = 2") == []
    # barra invertida em literal, longe de FROM/JOIN/UPDATE: não é entidade
    assert dql_references(f"select 'C:{BS}Temp{BS}x' as caminho from notas") == []


def test_dql_select_with_namespace_alias_and_named_parameter_makes_queries(
    php: PhpRecords,
) -> None:
    text = f"SELECT c FROM Contribuinte{BS}PlanoContaAbrasf c WHERE c.conta_abrasf = :numero_conta"

    builder, unresolved, stats = _resolve(_dql(text), ENTITIES)

    edge = _edges(builder, EdgeType.QUERIES)[(FILE, "Table:public.plano_conta_abrasf")]
    assert (edge.source, edge.confidence) == (EdgeSource.DOCTRINE_ANNOTATION, 1.0)
    assert dict(edge.evidence)["via"] == "dql"
    assert (stats.dql_strings, stats.dql_edges, stats.dql_unknown) == (1, 1, 0)
    assert not unresolved.counts


def test_dql_update_and_delete_make_updates_and_deletes() -> None:
    update = f"UPDATE Contribuinte{BS}DmsNota d SET d.situacao = 2 WHERE d.id = :id"
    delete = f"DELETE FROM Contribuinte{BS}DmsNota d WHERE d.id = :id"

    builder, _unresolved, stats = _resolve(_dql(update, delete), ENTITIES)

    assert (FILE, "Table:public.dms_nota") in _edges(builder, EdgeType.UPDATES)
    assert (FILE, "Table:public.dms_nota") in _edges(builder, EdgeType.DELETES)
    assert stats.dql_edges == 2


def test_dql_edge_inherits_the_convention_confidence_of_the_entity() -> None:
    builder, _unresolved, _stats = _resolve(_dql(f"SELECT e FROM Geral{BS}Estado e"), ENTITIES)

    assert _edges(builder, EdgeType.QUERIES)[(FILE, "Table:public.estado")].confidence == 0.8


def test_dql_entity_outside_the_map_makes_a_pending_and_no_edge() -> None:
    text = f"SELECT x FROM Contribuinte{BS}Fantasma x"

    builder, unresolved, stats = _resolve(_dql(text), ENTITIES)

    assert not _edges(builder, EdgeType.QUERIES)
    assert unresolved.counts["dql_entity_unknown"] == 1
    [pending] = [r for r in unresolved.records if r.kind == "dql_entity_unknown"]
    assert pending.hint == "contribuinte" + BS + "fantasma"
    assert stats.dql_unknown == 1


def test_dql_never_creates_a_table_named_after_the_namespace() -> None:
    """Conserto do spike da 33: `FROM Contribuinte\\X` virava uma `Table` falsa."""
    texts = _dql(
        f"SELECT c FROM Contribuinte{BS}PlanoContaAbrasf c",
        f"SELECT x FROM Contribuinte{BS}Fantasma x",
    )

    builder, _unresolved, _stats = _resolve(texts, ENTITIES)

    tables = {n.id for n in builder.iter_nodes() if n.label == "Table"}
    assert not any("contribuinte" in key_of(t) and "plano" not in key_of(t) for t in tables)
    assert tables == {"Table:public.plano_conta_abrasf"}


def test_real_sql_still_goes_through_pglast_when_doctrine_is_on() -> None:
    sql = _dql("select x.id from dms_nota x where x.id = 1")
    directory = _directory(("public", "dms_nota"))

    builder, _unresolved, stats = _resolve(sql, ENTITIES, directory)

    edge = _edges(builder, EdgeType.QUERIES)[(FILE, "Table:public.dms_nota")]
    assert edge.source == EdgeSource.SQL_AST
    assert stats.dql_strings == 0


def test_without_an_entity_map_a_profile_does_not_translate_dql() -> None:
    """e-cidade: `entities=None`, nada muda (a DQL só existe nos perfis que leem Doctrine)."""
    text = _dql(f"select 1 from Contribuinte{BS}X c")

    _builder, _unresolved, stats = _resolve(text, None)

    assert stats.dql_strings == 0
