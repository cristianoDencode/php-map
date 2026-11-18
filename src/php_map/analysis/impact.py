"""`impact`: quem é afetado por uma tabela, pela busca reversa."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from php_map.analysis import TABLE_OPS, Walk, is_infra, node_label, require_node, walk
from php_map.errors import QueryError
from php_map.graph.model import file_id, key_of, table_id

if TYPE_CHECKING:
    from php_map.graph.builder import KnowledgeGraph

_RPC_FILE = re.compile(r"\.RPC\.php$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ImpactOptions:
    table: str
    schema: str | None = None  # sem schema: o nó `Table` único com esse nome
    depth: int = 10
    min_confidence: float = 0.0
    include_infra: bool = False


@dataclass(frozen=True, slots=True)
class DirectTouch:
    """Aresta que chega na tabela: quem a lê ou altera diretamente, e como."""

    origin: str
    label: str
    operation: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AffectedFile:
    node: str
    path: str
    depth: int
    module_hint: str | None
    is_screen: bool
    is_rpc: bool
    via: str | None = None  # None = alcançado pela busca; "método" = arquivo do método alcançado


@dataclass(frozen=True, slots=True)
class DaoTouch:
    """Quem usa a tabela **pelo DAO**: chama um método `cl_x::m` que toca a tabela.

    `confidence` é a menor entre a chamada e a do DAO com a tabela; `evidence` é a da chamada."""

    origin: str
    label: str
    operation: str
    dao_method: str  # `incluir`; vários da mesma operação: `sql_query, sql_record`
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ImpactResult:
    options: ImpactOptions
    target: str
    walk: Walk
    labels: dict[str, str]
    direct: list[DirectTouch]
    files: list[AffectedFile]
    code: list[str]  # Method/Function intermediários
    pgfunctions: list[DirectTouch]
    dao_users: list[DaoTouch] = field(default_factory=list)


METHOD_VIA = "método"
DAO_PREFIX = "cl_"


def is_dao_method(node: str) -> bool:
    """`Method:cl_cgmtipoempresa::incluir`: método de uma classe `cl_*` (o DAO do e-cidade)."""
    return node.startswith("Method:") and key_of(node).startswith(DAO_PREFIX)


def table_target(table_ids: Iterable[str], table: str, schema: str | None) -> str:
    """ID da tabela. Sem `schema`, o nó único com esse nome (a tabela mora no schema do
    módulo); em mais de um schema → `QueryError` listando os candidatos.
    Nenhum → `public.<tabela>`, para o erro com sugestões de `require_node`."""
    if schema is not None:
        return table_id(table, schema)
    name = table.lower()
    found = sorted(i for i in table_ids if key_of(i).rpartition(".")[2] == name)
    if len(found) > 1:
        options = ", ".join(key_of(i) for i in found)
        example = key_of(found[0])
        raise QueryError(
            f"a tabela {table} existe em mais de um schema ({options}): "
            f"informe o schema (ex.: {example})"
        )
    return found[0] if found else table_id(table, "public")


def impact(graph: "KnowledgeGraph", options: ImpactOptions) -> ImpactResult:
    tables = [n for n, a in graph.nodes(data=True) if a.get("label") == "Table"]
    target = table_target(tables, options.table, options.schema)
    require_node(graph, target, tables, "tabela")
    result = walk(
        graph,
        target,
        depth=options.depth,
        min_confidence=options.min_confidence,
        include_infra=options.include_infra,
        reverse=True,
    )
    labels = {node: node_label(graph, node) for node in result.steps}
    direct = []
    for origin, _v, data in graph.in_edges(target, data=True):
        operation = TABLE_OPS.get(data["type"])
        if operation is None or origin not in result.steps:
            continue
        if data["confidence"] < options.min_confidence:
            continue
        direct.append(
            DirectTouch(
                origin, labels[origin], operation, data["confidence"], dict(data["evidence"])
            )
        )
    direct.sort(key=lambda d: (d.label, d.origin, d.operation))
    files = []
    for node, step in result.steps.items():
        if labels[node] != "File":
            continue
        attrs = graph.nodes[node]
        path = str(attrs.get("path") or node.partition(":")[2])
        hint = attrs.get("module_hint")
        files.append(
            AffectedFile(
                node, path, step.depth, hint, hint is not None, bool(_RPC_FILE.search(path))
            )
        )
    seen = {f.node for f in files}
    for node, step in result.steps.items():
        # o `walk` não percorre `DECLARES`; o arquivo de um método alcançado entra
        # aqui, na mesma profundidade. O `path` do nó, nunca derivado do nome (a caixa difere).
        if labels[node] not in ("Method", "Function"):
            continue
        declared = graph.nodes[node].get("path")
        owner = file_id(str(declared)) if declared else None
        if owner is None or owner in seen or owner not in graph:
            continue
        if not options.include_infra and is_infra(graph, owner):
            continue
        seen.add(owner)
        hint = graph.nodes[owner].get("module_hint")
        files.append(
            AffectedFile(
                owner,
                str(declared),
                step.depth,
                hint,
                hint is not None,
                bool(_RPC_FILE.search(str(declared))),
                METHOD_VIA,
            )
        )
    files.sort(key=lambda f: f.path)
    code = sorted(n for n in result.steps if labels[n] in ("Method", "Function"))
    pgfunctions = [d for d in direct if d.label == "PgFunction"]
    dao_users = _dao_users(graph, result, direct, labels, options)
    return ImpactResult(
        options, target, result, labels, direct, files, code, pgfunctions, dao_users
    )


def _dao_users(
    graph: "KnowledgeGraph",
    result: Walk,
    direct: list[DirectTouch],
    labels: dict[str, str],
    options: ImpactOptions,
) -> list[DaoTouch]:
    """Chamadores dos métodos `cl_*` que tocam a tabela (ver `group_dao_users`)."""
    direct_origins = {d.origin for d in direct}
    calls: list[tuple[str, str, str, str, float, dict[str, Any]]] = []
    for touch in direct:
        if not is_dao_method(touch.origin):
            continue
        for caller, _v, data in graph.in_edges(touch.origin, data=True):
            if data["type"] != "CALLS" or data["confidence"] < options.min_confidence:
                continue
            if caller in direct_origins or caller not in result.steps:
                continue
            calls.append(
                (
                    caller,
                    labels[caller],
                    touch.operation,
                    touch.origin,
                    min(data["confidence"], touch.confidence),
                    dict(data["evidence"]),
                )
            )
    return group_dao_users(calls)


def group_dao_users(
    calls: Iterable[tuple[str, str, str, str, float, dict[str, Any]]],
) -> list[DaoTouch]:
    """Uma linha por (chamador, operação). Cada item: `(chamador, rótulo, operação, método do
    DAO, confiança, evidência da chamada)`. É a regra única da CLI e da interface (que montam as
    chamadas por caminhos diferentes: grafo em memória × SQL)."""
    grouped: dict[tuple[str, str, str], list[tuple[str, float, dict[str, Any]]]] = {}
    for caller, label, operation, dao_node, confidence, evidence in calls:
        dao_class, _sep, method = key_of(dao_node).partition("::")
        if is_dao_method(caller) and key_of(caller).startswith(f"{dao_class}::"):
            continue  # o DAO chamando a si mesmo
        grouped.setdefault((caller, label, operation), []).append((method, confidence, evidence))
    users = []
    for (caller, label, operation), items in grouped.items():
        items.sort(key=lambda item: (-item[1], item[0]))
        methods = ", ".join(sorted({method for method, _c, _e in items}))
        users.append(DaoTouch(caller, label, operation, methods, items[0][1], items[0][2]))
    users.sort(key=lambda d: (d.label, d.origin, d.operation))
    return users
