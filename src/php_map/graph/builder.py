"""Acumula nós e arestas sem duplicar e entrega o `networkx.MultiDiGraph`."""

from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeAlias

import networkx as nx

from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    key_of,
    label_of,
    validate_edge,
    validate_node,
)

if TYPE_CHECKING:
    KnowledgeGraph: TypeAlias = nx.MultiDiGraph[str]

MAX_SAMPLE_LINES = 5

EdgeKey = tuple[str, str, EdgeType, EdgeSource]


@dataclass(slots=True)
class _NodeState:
    label: NodeLabel
    attrs: dict[str, Any]


@dataclass(slots=True)
class _EdgeState:
    first: Edge
    occurrences: int = 0
    sample_lines: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GraphStats:
    nodes: int
    edges: int
    placeholders: int
    nodes_by_label: dict[str, int]
    edges_by_type: dict[str, int]
    edges_by_source: dict[str, int]


@dataclass(frozen=True, slots=True)
class StoredEdge:
    """Aresta agregada, pronta para persistir: `attrs` já inclui `occurrences` e `sample_lines`."""

    src: str
    dst: str
    type: EdgeType
    source: EdgeSource
    confidence: float
    evidence: Mapping[str, Any]
    attrs: Mapping[str, Any]


def _sample(evidence: Mapping[str, Any]) -> dict[str, Any] | None:
    if "line" not in evidence:
        return None
    return {"path": evidence.get("path"), "line": evidence["line"]}


class GraphBuilder:
    """Nós por ID; arestas deduplicadas por `(src, dst, type, source)`.

    Na deduplicação ficam a **primeira** aresta (confiança, evidência, attrs), o total de
    `occurrences` e até `MAX_SAMPLE_LINES` locais (`{path, line}`) na ordem de chegada.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, _NodeState] = {}
        self._edges: dict[EdgeKey, _EdgeState] = {}
        # Índices para remoção (CREATE OR REPLACE / DROP nos incrementais).
        self._by_src: dict[str, set[EdgeKey]] = {}
        self._by_dst: dict[str, set[EdgeKey]] = {}

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._nodes

    def add_node(self, node: Node, replace: bool = False) -> None:
        """Adiciona um nó real. Sobre um placeholder, os attrs do nó real prevalecem.

        `replace=True` troca todos os attrs de um nó existente (`CREATE OR REPLACE`).
        """
        validate_node(node)
        current = self._nodes.get(node.id)
        attrs = dict(node.attrs)
        attrs.setdefault("resolved", True)
        if current is None or replace:
            self._nodes[node.id] = _NodeState(node.label, attrs)
        elif current.attrs.get("resolved") is False:
            current.attrs = {**current.attrs, **attrs}
        else:
            current.attrs.update(node.attrs)

    def ensure_node(self, node_id: str, label: NodeLabel | None = None, **attrs: Any) -> None:
        """Cria um placeholder (`resolved=False`) se o nó não existir.

        Se já existir, só preenche attrs ausentes: nunca sobrescreve nem muda `resolved`.
        """
        actual = label_of(node_id)
        if label is not None and label != actual:
            validate_node(Node(node_id, label))
        current = self._nodes.get(node_id)
        if current is None:
            placeholder = {"name": key_of(node_id), **attrs, "resolved": False}
            self._nodes[node_id] = _NodeState(actual, placeholder)
            return
        for name, value in attrs.items():
            if name != "resolved":
                current.attrs.setdefault(name, value)

    def add_edge(self, edge: Edge) -> None:
        """Valida, cria placeholders para as pontas ausentes e agrega arestas repetidas."""
        validate_edge(edge)
        self.ensure_node(edge.src)
        self.ensure_node(edge.dst)
        key: EdgeKey = (edge.src, edge.dst, EdgeType(edge.type), EdgeSource(edge.source))
        state = self._edges.get(key)
        if state is None:
            state = self._edges[key] = _EdgeState(edge)
            self._by_src.setdefault(edge.src, set()).add(key)
            self._by_dst.setdefault(edge.dst, set()).add(key)
        state.occurrences += 1
        if len(state.sample_lines) < MAX_SAMPLE_LINES:
            sample = _sample(edge.evidence)
            if sample is not None:
                state.sample_lines.append(sample)

    def remove_edges_from(self, node_id: str) -> int:
        """Remove as arestas que **saem** de `node_id`; devolve quantas eram."""
        keys = self._by_src.pop(node_id, set())
        for key in keys:
            del self._edges[key]
            self._by_dst[key[1]].discard(key)
        return len(keys)

    def drop_node(self, node_id: str) -> None:
        """`DROP`: remove o nó e as arestas que saem dele.

        Se ainda houver arestas chegando (ex.: uma função que lê a tabela removida e que pode
        ser recriada depois), o nó volta a ser placeholder em vez de sumir.
        """
        state = self._nodes.get(node_id)
        if state is None:
            return
        self.remove_edges_from(node_id)
        if self._by_dst.get(node_id):
            state.attrs = {"name": key_of(node_id), "resolved": False}
        else:
            del self._nodes[node_id]
            self._by_dst.pop(node_id, None)

    def node_attrs(self, node_id: str) -> Mapping[str, Any]:
        return self._nodes[node_id].attrs

    def iter_nodes(self) -> Iterator[Node]:
        for node_id, state in self._nodes.items():
            yield Node(node_id, state.label, state.attrs)

    def iter_edges(self) -> Iterator[StoredEdge]:
        for (src, dst, type_, source), state in self._edges.items():
            first = state.first
            attrs = {
                **first.attrs,
                "occurrences": state.occurrences,
                "sample_lines": list(state.sample_lines),
            }
            yield StoredEdge(src, dst, type_, source, first.confidence, first.evidence, attrs)

    def stats(self) -> GraphStats:
        by_label = Counter(state.label.value for state in self._nodes.values())
        by_type: Counter[str] = Counter()
        by_source: Counter[str] = Counter()
        for _src, _dst, type_, source in self._edges:
            by_type[type_.value] += 1
            by_source[source.value] += 1
        placeholders = sum(1 for s in self._nodes.values() if s.attrs.get("resolved") is False)
        return GraphStats(
            nodes=len(self._nodes),
            edges=len(self._edges),
            placeholders=placeholders,
            nodes_by_label=dict(sorted(by_label.items())),
            edges_by_type=dict(sorted(by_type.items())),
            edges_by_source=dict(sorted(by_source.items())),
        )

    def to_networkx(self) -> "KnowledgeGraph":
        graph = new_graph()
        for node in self.iter_nodes():
            add_nx_node(graph, node.id, node.label, node.attrs)
        for edge in self.iter_edges():
            add_nx_edge(graph, edge)
        return graph


def new_graph() -> "KnowledgeGraph":
    return nx.MultiDiGraph()


def add_nx_node(
    graph: "KnowledgeGraph",
    node_id: str,
    label: str,
    attrs: Mapping[str, Any],
) -> None:
    graph.add_node(node_id, **{**attrs, "label": str(label)})


def add_nx_edge(graph: "KnowledgeGraph", edge: StoredEdge) -> None:
    """Chave da aresta = `type`. Se o par já tiver esse tipo por outra fonte
    (ex.: `QUERIES` por `SQL_AST` e por `SQL_REGEX`), a chave vira `type:source`."""
    key = str(edge.type)
    if graph.has_edge(edge.src, edge.dst, key=key):
        key = f"{edge.type}:{edge.source}"
    data = {
        **edge.attrs,
        "type": str(edge.type),
        "source": str(edge.source),
        "confidence": edge.confidence,
        "evidence": dict(edge.evidence),
    }
    graph.add_edge(edge.src, edge.dst, key=key, **data)
