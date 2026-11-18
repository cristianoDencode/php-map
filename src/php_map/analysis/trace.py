"""`trace`: da tela (ou de qualquer `File`) até as tabelas e funções do banco."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from php_map.analysis import (
    LEAF_LABELS,
    TABLE_OPS,
    TRAVERSED,
    TRAVERSED_WITH_REMOTE,
    Walk,
    node_label,
    require_node,
    walk,
)
from php_map.graph.model import file_id

if TYPE_CHECKING:
    from php_map.graph.builder import KnowledgeGraph


@dataclass(frozen=True, slots=True)
class TraceOptions:
    entry: str
    depth: int = 8
    min_confidence: float = 0.0
    include_infra: bool = False
    expand_pgfunctions: bool = False
    follow_remote: bool = False  # também `CALLS_REMOTE` (chega ao `Endpoint`)


@dataclass(frozen=True, slots=True)
class TableTouch:
    """Aresta de um nó visitado para uma tabela alcançada."""

    operation: str  # INSERT | UPDATE | DELETE | SELECT
    table: str
    origin: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TraceResult:
    options: TraceOptions
    walk: Walk
    labels: dict[str, str]
    tables: list[TableTouch]
    dispatch: dict[str, list[str]]  # File → ações despachadas (`dispatch_actions`)
    file_paths: list[str]

    def count(self, label: str) -> int:
        return sum(1 for value in self.labels.values() if value == label)

    def tables_by_operation(self) -> dict[str, int]:
        per_op: dict[str, set[str]] = {}
        for touch in self.tables:
            per_op.setdefault(touch.operation, set()).add(touch.table)
        return {
            op: len(per_op[op]) for op in ("INSERT", "UPDATE", "DELETE", "SELECT") if op in per_op
        }

    def rpc_count(self) -> int:
        return self.count("PgFunction") + self.count("RpcCall")


def trace(graph: "KnowledgeGraph", options: TraceOptions) -> TraceResult:
    root = file_id(options.entry)
    require_node(
        graph,
        root,
        [n for n, a in graph.nodes(data=True) if a.get("label") == "File"],
        "arquivo de entrada",
    )

    def expand(node: str) -> bool:
        label = node_label(graph, node)
        if label == "PgFunction":
            return options.expand_pgfunctions
        return label not in LEAF_LABELS

    result = walk(
        graph,
        root,
        depth=options.depth,
        min_confidence=options.min_confidence,
        include_infra=options.include_infra,
        expand=expand,
        traversed=TRAVERSED_WITH_REMOTE if options.follow_remote else TRAVERSED,
    )
    labels = {node: node_label(graph, node) for node in result.steps}
    tables: list[TableTouch] = []
    for origin in result.steps:
        if origin != root and not expand(origin):
            continue  # folha: não percorre as arestas dela
        for _u, target, data in graph.out_edges(origin, data=True):
            operation = TABLE_OPS.get(data["type"])
            if operation is None or target not in result.steps:
                continue
            if data["confidence"] < options.min_confidence:
                continue
            tables.append(
                TableTouch(operation, target, origin, data["confidence"], dict(data["evidence"]))
            )
    tables.sort(key=lambda t: (t.operation, t.table, t.origin))
    dispatch = {
        node: list(graph.nodes[node]["dispatch_actions"])
        for node in sorted(result.steps)
        if labels[node] == "File" and graph.nodes[node].get("dispatch_actions")
    }
    paths = sorted(
        str(graph.nodes[node].get("path") or node.partition(":")[2])
        for node in result.steps
        if labels[node] == "File"
    )
    return TraceResult(options, result, labels, tables, dispatch, paths)
