"""Consultas sobre o grafo persistido: `trace` e `impact`.

Regras comuns:
- só estas arestas são percorridas: `TRAVERSED` (nunca `DECLARES`, que traria todas as
  funções de uma lib, nem `TRIGGERS`);
- nós `is_infra` não são atravessados nem exibidos, salvo com `include_infra`;
- arestas com `confidence < min_confidence` são ignoradas;
- cada nó é visitado uma vez, pelo caminho mínimo (BFS limitada por profundidade); vizinhos
  em ordem de ID, para a saída ser determinística.
"""

import difflib
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from php_map.errors import QueryError

if TYPE_CHECKING:
    from php_map.graph.builder import KnowledgeGraph

TRAVERSED = frozenset(
    {
        "INCLUDES",
        "REQUIRES",
        "CALLS",
        "INSTANTIATES",
        "REFERENCES",
        "CALLS_RPC",
        "QUERIES",
        "INSERTS",
        "UPDATES",
        "DELETES",
        # tela/JS -> rota -> controller, e model -> tabela.
        "CALLS_HTTP",
        "ROUTES_TO",
        "MAPS_TO",
    }
)
# `trace --cross`: sem a chamada remota o trace não chega ao `Endpoint` do outro lado.
TRAVERSED_WITH_REMOTE = TRAVERSED | {"CALLS_REMOTE"}
TABLE_OPS = {"INSERTS": "INSERT", "UPDATES": "UPDATE", "DELETES": "DELETE", "QUERIES": "SELECT"}
LEAF_LABELS = frozenset({"Table", "PgFunction", "RpcCall"})
MAX_SUGGESTIONS = 5


@dataclass(frozen=True, slots=True)
class Step:
    """Como a busca chegou a `node`: pela aresta `edge_type` a partir de `parent`."""

    node: str
    parent: str | None
    edge_type: str | None
    confidence: float | None
    depth: int
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Walk:
    """Resultado de uma busca: `steps` na ordem de visita (a da BFS)."""

    root: str
    steps: dict[str, Step]

    def children(self) -> dict[str, list[str]]:
        tree: dict[str, list[str]] = {}
        for step in self.steps.values():
            if step.parent is not None:
                tree.setdefault(step.parent, []).append(step.node)
        for kids in tree.values():
            kids.sort()
        return tree

    def path_to(self, node: str) -> list[str]:
        path = [node]
        while (parent := self.steps[path[-1]].parent) is not None:
            path.append(parent)
        return path[::-1]

    def dfs_order(self) -> Iterator[Step]:
        """Árvore da busca em pré-ordem (para a lista indentada do relatório)."""
        tree = self.children()
        stack = [self.root]
        while stack:
            node = stack.pop()
            yield self.steps[node]
            stack.extend(reversed(tree.get(node, [])))


def node_label(graph: "KnowledgeGraph", node: str) -> str:
    return str(graph.nodes[node].get("label", node.partition(":")[0]))


def is_infra(graph: "KnowledgeGraph", node: str) -> bool:
    return bool(graph.nodes[node].get("is_infra"))


def walk(
    graph: "KnowledgeGraph",
    root: str,
    *,
    depth: int,
    min_confidence: float,
    include_infra: bool,
    reverse: bool = False,
    expand: Any = None,
    traversed: frozenset[str] = TRAVERSED,
) -> Walk:
    """BFS a partir de `root` pelas arestas de `traversed` (ao contrário, com `reverse`).

    `expand(node) -> bool` diz se a busca continua a partir do nó (folhas param).
    """
    steps = {root: Step(root, None, None, None, 0)}
    queue = deque([root])
    while queue:
        current = queue.popleft()
        step = steps[current]
        if step.depth >= depth or (expand is not None and current != root and not expand(current)):
            continue
        for neighbor, edge in _neighbors(graph, current, reverse):
            if edge["type"] not in traversed or edge["confidence"] < min_confidence:
                continue
            if neighbor in steps or (not include_infra and is_infra(graph, neighbor)):
                continue
            steps[neighbor] = Step(
                neighbor,
                current,
                edge["type"],
                edge["confidence"],
                step.depth + 1,
                dict(edge.get("evidence") or {}),
            )
            queue.append(neighbor)
    return Walk(root, steps)


def _neighbors(
    graph: "KnowledgeGraph", node: str, reverse: bool
) -> list[tuple[str, dict[str, Any]]]:
    """Vizinhos por ID; entre arestas do mesmo par, a de maior confiança primeiro."""
    if reverse:
        edges = [(u, data) for u, _v, data in graph.in_edges(node, data=True)]
    else:
        edges = [(v, data) for _u, v, data in graph.out_edges(node, data=True)]
    return sorted(edges, key=lambda item: (item[0], -item[1]["confidence"], item[1]["type"]))


def require_node(graph: "KnowledgeGraph", node: str, candidates: Sequence[str], what: str) -> None:
    """`QueryError` com até 5 sugestões parecidas (`difflib`) se `node` não existe."""
    if node in graph:
        return
    key = node.partition(":")[2]
    keys = [c.partition(":")[2] for c in candidates]
    # Sem a extensão: o `.php` comum a todos inflaria a semelhança (`zzz.php` ~ `a.php`).
    stems = {_stem(k): k for k in reversed(keys)}
    matches = difflib.get_close_matches(_stem(key), list(stems), n=MAX_SUGGESTIONS, cutoff=0.5)
    close = [stems[m] for m in matches]
    if not close:  # nome parcial (ex.: só o arquivo, sem o diretório)
        close = [k for k in keys if _stem(key).lower() in k.lower()][:MAX_SUGGESTIONS]
    hint = f" Parecidos: {', '.join(close)}" if close else ""
    raise QueryError(f"{what} não encontrado no grafo: {key}.{hint}")


def _stem(key: str) -> str:
    return key[:-4] if key.lower().endswith(".php") else key
