"""Subgrafos por SQL: a BFS do `analysis.walk` sem carregar o grafo.

Mesma semântica de `analysis.walk`:
- só as arestas de `TRAVERSED`, com `confidence >= min_confidence`;
- cada nó é visitado uma vez, pela menor profundidade;
- os vizinhos em ordem de ID e, entre as arestas do mesmo par, a de maior confiança primeiro
  (depois o tipo e a ordem de inserção, que é o `id` da aresta);
- `is_infra` pelo atributo do nó; `expand(label)` decide se a busca continua a partir de um nó
  (as raízes sempre continuam).

A busca anda por nível: as arestas de todos os nós do nível vêm numa consulta (em lotes de
`BATCH` IDs, por causa do limite de variáveis do SQLite) e são processadas na ordem da fila,
exatamente como a BFS com `deque`. Corta em `max_nodes`, na ordem da BFS, e marca `truncated`.
"""

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from php_map.analysis import TRAVERSED, Step, Walk

BATCH = 500
_TYPES = tuple(sorted(TRAVERSED))

ExpandFn = Callable[[str], bool]  # label → continua a busca a partir do nó?


@dataclass(frozen=True, slots=True)
class NodeInfo:
    label: str
    is_infra: bool


@dataclass(slots=True)
class SqlWalk:
    walk: Walk
    labels: dict[str, str]
    edge_ids: dict[str, int] = field(default_factory=dict)  # nó → aresta que o descobriu
    truncated: bool = False


def batches(items: Sequence[str], size: int = BATCH) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def placeholders(count: int) -> str:
    return ",".join("?" * count)


def node_info(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, NodeInfo]:
    """Label e `is_infra` de cada nó que existe (os ausentes ficam de fora)."""
    wanted = list(dict.fromkeys(ids))
    found: dict[str, NodeInfo] = {}
    for chunk in batches(wanted):
        rows = conn.execute(
            "SELECT id, label, json_extract(attrs, '$.is_infra') FROM nodes"
            f" WHERE id IN ({placeholders(len(chunk))})",
            tuple(chunk),
        )
        for node_id, label, infra in rows:
            found[node_id] = NodeInfo(label, bool(infra))
    return found


def edges_of(
    conn: sqlite3.Connection, nodes: Sequence[str], reverse: bool, min_confidence: float
) -> dict[str, list[tuple[str, str, float, dict[str, Any], int]]]:
    """Arestas de `TRAVERSED` saindo (ou chegando, com `reverse`) de cada nó, já na ordem do
    `analysis._neighbors`: vizinho, confiança decrescente, tipo, id."""
    key, other = ("dst", "src") if reverse else ("src", "dst")
    result: dict[str, list[tuple[str, str, float, dict[str, Any], int]]] = {}
    for chunk in batches(nodes):
        rows = conn.execute(
            f"SELECT {key}, {other}, type, confidence, evidence, id FROM edges"
            f" WHERE {key} IN ({placeholders(len(chunk))})"
            f" AND type IN ({placeholders(len(_TYPES))}) AND confidence >= ?"
            f" ORDER BY {key}, {other}, confidence DESC, type, id",
            (*chunk, *_TYPES, min_confidence),
        )
        for node, neighbor, type_, confidence, evidence, edge_id in rows:
            result.setdefault(node, []).append(
                (neighbor, type_, confidence, json.loads(evidence), edge_id)
            )
    return result


def walk_sql(
    conn: sqlite3.Connection,
    root: str,
    *,
    depth: int,
    min_confidence: float,
    include_infra: bool,
    reverse: bool = False,
    expand: ExpandFn | None = None,
    max_nodes: int = 2000,
    roots: Sequence[str] = (),
) -> SqlWalk:
    """BFS a partir de `root` (que precisa existir: quem chama confere com `require_node`).

    `roots`: outras partidas, também na profundidade 0 e sem pai, expandidas no
    1º nível junto com `root` (o arquivo e o que ele declara). `Walk.root` continua `root`.
    """
    starts = list(dict.fromkeys([root, *roots]))
    info = node_info(conn, starts)
    steps: dict[str, Step] = {node: Step(node, None, None, None, 0) for node in starts}
    labels = {node: info[node].label if node in info else node.partition(":")[0] for node in starts}
    result = SqlWalk(Walk(root, steps), labels)
    frontier = starts
    level = 0
    while frontier and level < depth:
        expandable = [  # as raízes (nível 0) sempre continuam
            node for node in frontier if level == 0 or expand is None or expand(labels[node])
        ]
        edges = edges_of(conn, expandable, reverse, min_confidence)
        candidates = {n for rows in edges.values() for n, *_ in rows if n not in steps}
        info.update(node_info(conn, candidates))
        next_frontier: list[str] = []
        for node in expandable:
            for neighbor, type_, confidence, evidence, edge_id in edges.get(node, ()):
                if neighbor in steps:
                    continue
                neighbor_info = info.get(neighbor)
                if not include_infra and neighbor_info is not None and neighbor_info.is_infra:
                    continue
                if len(steps) >= max_nodes:
                    result.truncated = True
                    return result
                steps[neighbor] = Step(neighbor, node, type_, confidence, level + 1, evidence)
                labels[neighbor] = (
                    neighbor_info.label if neighbor_info else neighbor.partition(":")[0]
                )
                result.edge_ids[neighbor] = edge_id
                next_frontier.append(neighbor)
        frontier = next_frontier
        level += 1
    return result
