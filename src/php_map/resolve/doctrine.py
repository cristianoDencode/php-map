"""Doctrine: entidade → tabela (`MAPS_TO`) e DQL → tabelas.

O Doctrine anotado declara a tabela no docblock da classe (`@Entity`, `@Table(name="x")`) e
as colunas nos docblocks das propriedades (`@Column`, `@Id`, `@GeneratedValue`). O extrator
(schema_version 5) entrega esses docblocks crus; a leitura é daqui. A DQL, por sua vez, usa o
**nome da classe** (`FROM Contribuinte\\PlanoContaAbrasf c`), então só dá para ligá-la a uma
tabela depois de ter o mapa classe → tabela: `apply_doctrine` devolve esse mapa para
`resolve/sql_strings.py`, que traduz a string antes de mandá-la ao `pglast` (a DQL não é SQL
válido e, sem isto, o regex de fallback criava uma `Table` com o nome do namespace).
"""

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    DOCTRINE_CONVENTION_CONFIDENCE,
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    class_id,
    key_of,
    table_id,
)
from php_map.ingest.records import Declare, PhpFileRecord
from php_map.ingest.table_directory import TableDirectory
from php_map.resolve.unresolved import UnresolvedWriter

RecordSource = Iterable[PhpFileRecord] | Callable[[], Iterable[PhpFileRecord]]

# `@Nome` depois de espaço, `*` ou `/` (cabem várias numa linha: `/** @Id @Column(...) */`), com
# `ORM\` opcional na frente. `suporte@exemplo.com.br` não casa: a `@` vem colada em texto.
_ANNOTATION = re.compile(r"(?<![^\s*/])@(?:ORM\\)?(?P<name>[A-Za-z_]\w*)")
_QUOTED = r"""(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)')"""
_NAME_ARG = re.compile(rf"\bname\s*=\s*{_QUOTED}")
_TYPE_ARG = re.compile(rf"\btype\s*=\s*{_QUOTED}")
# Nome de entidade na DQL: com namespace (`Contribuinte\PlanoContaAbrasf`). SQL de verdade nunca
# tem barra invertida num identificador, então é o sinal que separa as duas linguagens.
_ENTITY = r"[A-Za-z_]\w*(?:\\\w+)+"
_ALIAS = r"(?:\s+(?:AS\s+)?\w+)?"
_DQL_ANCHOR = re.compile(
    rf"\b(?P<kw>DELETE\s+FROM|DELETE|UPDATE|FROM|JOIN)\s+(?P<first>{_ENTITY}){_ALIAS}"
    rf"(?P<rest>(?:\s*,\s*{_ENTITY}{_ALIAS})*)",
    re.IGNORECASE,
)
_ENTITY_RE = re.compile(_ENTITY)

_VERB_OPS = {
    "update": EdgeType.UPDATES,
    "delete": EdgeType.DELETES,
    "delete from": EdgeType.DELETES,
}


@dataclass(frozen=True, slots=True)
class EntityTable:
    """Tabela de uma entidade e o quanto se confia nela (1.0 do `@Table`, 0.8 pela convenção)."""

    table: str
    confidence: float


@dataclass(slots=True)
class DoctrineStats:
    entities: int = 0
    mapped_explicit: int = 0
    mapped_convention: int = 0
    table_missing: int = 0
    columns: int = 0
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class Annotation:
    name: str
    args: str


@dataclass(slots=True)
class DqlRef:
    entity: str  # chave normalizada: FQCN em minúsculas, sem barra inicial
    op: EdgeType
    raw: str = field(default="")


def parse_annotations(doc: str) -> list[Annotation]:
    """Anotações (`@Nome` ou `@Nome(args)`) do docblock; só as que abrem uma linha.

    Os argumentos podem atravessar linhas (`@Column(\\n type="text"\\n)`): vale o parêntese que
    fecha, respeitando aspas. Texto que não casa (`@var int`, prosa) vira anotação sem argumentos
    e é ignorado por quem consome.
    """
    found: list[Annotation] = []
    for match in _ANNOTATION.finditer(doc):
        end = match.end()
        args = ""
        if end < len(doc) and doc[end] == "(":
            args, _closed = _balanced(doc, end)
        found.append(Annotation(match.group("name"), args))
    return found


def _balanced(text: str, start: int) -> tuple[str, bool]:
    """Conteúdo entre o `(` em `start` e o `)` que o fecha; `(texto, False)` se não fechar."""
    depth = 0
    quote = ""
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], True
    return text[start + 1 :], False


def _arg(pattern: re.Pattern[str], args: str) -> str | None:
    match = pattern.search(args)
    if match is None:
        return None
    value = match.group("dq") if match.group("dq") is not None else match.group("sq")
    return str(value)


def _columns(declare: Declare) -> dict[str, dict[str, object]]:
    """`{propriedade: {type, id, generated[, column]}}` das propriedades com `@Column`.

    Propriedade sem `@Column` (associação, `@var` solto) fica de fora: nada de inventar coluna.
    """
    columns: dict[str, dict[str, object]] = {}
    for prop in declare.properties:
        notes = {a.name: a for a in parse_annotations(prop.doc_comment)}
        column = notes.get("Column")
        if column is None:
            continue
        info: dict[str, object] = {
            "type": _arg(_TYPE_ARG, column.args),
            "id": "Id" in notes,
            "generated": "GeneratedValue" in notes,
        }
        name = _arg(_NAME_ARG, column.args)
        if name:
            info["column"] = name
        columns[prop.name] = info
    return columns


def _entity_table(declare: Declare) -> tuple[str, str | None, float, str | None] | None:
    """`(tabela, schema explícito, confiança, regra da convenção)`; `None` se não é `@Entity`."""
    notes = {a.name: a for a in parse_annotations(declare.doc_comment or "")}
    if "Entity" not in notes:
        return None
    table = notes.get("Table")
    name = _arg(_NAME_ARG, table.args) if table is not None else None
    if name:
        schema, _dot, bare = name.rpartition(".")
        return bare, schema or None, 1.0, None
    short = declare.name.rsplit("\\", 1)[-1]
    return short, None, DOCTRINE_CONVENTION_CONFIDENCE, "doctrine:classname"


def apply_doctrine(
    records: RecordSource,
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    settings: Settings,
    directory: TableDirectory | None = None,
) -> tuple[DoctrineStats, dict[str, EntityTable]]:
    """`MAPS_TO` de cada `@Entity` e o mapa `FQCN em minúsculas → tabela` para a DQL.

    **Ordem:** depois do schema (o `directory` diz em que schema a tabela está) e da resolução
    PHP (os nós `Class` já existem); antes do SQL, que usa o mapa. Sem `directory` com tabelas
    (scan sem `--schema`/`--schema-db`), "a tabela não existe no banco" não se aplica e não vira
    pendência: não há como saber.
    """
    started = time.perf_counter()
    stats = DoctrineStats()
    entities: dict[str, EntityTable] = {}
    known = directory if directory is not None else TableDirectory(settings.search_path)
    has_schema = len(known) > 0
    for record in records() if callable(records) else records:
        for declare in record.declares:
            if declare.kind != "class" or not declare.doc_comment:
                continue
            found = _entity_table(declare)
            if found is None:
                continue
            name, schema, confidence, rule = found
            stats.entities += 1
            real_schema, _rule = known.resolve(name, schema)
            target = table_id(name, real_schema)
            evidence: dict[str, object] = {"path": record.path, "line": declare.line_start}
            if rule is not None:
                evidence["rule"] = rule
                stats.mapped_convention += 1
            else:
                stats.mapped_explicit += 1
            source = class_id(declare.name)
            builder.add_edge(
                Edge(
                    source,
                    target,
                    EdgeType.MAPS_TO,
                    EdgeSource.DOCTRINE_ANNOTATION,
                    confidence,
                    evidence,
                )
            )
            columns = _columns(declare)
            stats.columns += len(columns)
            builder.add_node(Node(source, NodeLabel.CLASS, {"doctrine_columns": columns}))
            entities[key_of(source)] = EntityTable(target, confidence)
            if has_schema and not known.has(name, real_schema):
                stats.table_missing += 1
                unresolved.add(
                    "doctrine_table_missing",
                    record.path,
                    declare.line_start,
                    declare.name,
                    candidates=[target],
                    hint=declare.name,
                )
    stats.seconds = round(time.perf_counter() - started, 2)
    return stats, entities


def dql_references(text: str) -> list[DqlRef]:
    """Entidades citadas numa string que é DQL; lista vazia se a string não é DQL.

    É DQL quando um `FROM`, `JOIN`, `UPDATE` ou `DELETE [FROM]` aponta para um nome com
    namespace (`Contribuinte\\PlanoContaAbrasf`). O critério é estrutural e não olha o schema:
    um identificador SQL nunca tem barra invertida, então SQL legítimo (`from dms_nota x`)
    nunca chega aqui. `SELECT` e `JOIN` dão `QUERIES`; `UPDATE`, `UPDATES`; `DELETE`, `DELETES`.
    """
    refs: list[DqlRef] = []
    for match in _DQL_ANCHOR.finditer(text):
        keyword = " ".join(match.group("kw").lower().split())
        op = _VERB_OPS.get(keyword, EdgeType.QUERIES)
        first = match.group("first")
        refs.append(DqlRef(first.lstrip("\\").lower(), op, first))
        for other in _ENTITY_RE.findall(match.group("rest")):
            refs.append(DqlRef(other.lstrip("\\").lower(), EdgeType.QUERIES, other))
    return refs
