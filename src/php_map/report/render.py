"""Relatórios Markdown + Mermaid do `trace` e do `impact` ."""

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from php_map.analysis import Step, Walk
from php_map.analysis.cross import CrossTraceResult
from php_map.analysis.impact import ImpactResult
from php_map.analysis.trace import TraceResult

TEMPLATES = Path(__file__).parent / "templates"
MAX_PENDING = 50
# Formas do Mermaid por label (o rótulo vai entre aspas: `n1["File: x.php"]`).
_SHAPES = {
    "Table": ('[("', '")]'),
    "PgFunction": ('[["', '"]]'),
    "RpcCall": ('[["', '"]]'),
    "Method": ('("', '")'),
    "Function": ('("', '")'),
}

_env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=False,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=StrictUndefined,
)


@dataclass(frozen=True, slots=True)
class Pending:
    kind: str
    path: str
    line: int
    hint: str | None
    count: int


def read_pending(unresolved: Path, paths: Iterable[str]) -> list[Pending]:
    """Pendências de `unresolved.jsonl` cujo `path` está entre os arquivos alcançados."""
    wanted = set(paths)
    if not unresolved.is_file():
        return []
    found = []
    with unresolved.open(encoding="utf-8") as handle:
        for line in handle:
            data = json.loads(line)
            if data.get("path") in wanted:
                found.append(
                    Pending(
                        data["kind"],
                        data["path"],
                        int(data.get("line") or 0),
                        data.get("hint"),
                        int(data.get("count") or 1),
                    )
                )
    return sort_pending(found)


def sort_pending(items: Iterable[Pending]) -> list[Pending]:
    return sorted(items, key=lambda p: (p.kind, p.path, p.line, p.hint or ""))


# --------------------------------------------------------------------------- Mermaid


def _mermaid_label(node: str, label: str) -> str:
    key = node.partition(":")[2]
    return f"{label}: {key}".replace('"', "#quot;")


def mermaid(
    walk: Walk,
    labels: Mapping[str, str],
    max_nodes: int,
    *,
    reverse: bool = False,
    priority: Sequence[str] = (),
) -> tuple[str, int]:
    """`flowchart LR` da árvore da busca, cortado em `max_nodes`.

    IDs sintéticos (`n1`, `n2`…), porque `:`, `/` e `.` quebram IDs do Mermaid. Os caminhos
    até os nós de `priority` (as folhas de banco) entram primeiro; o resto, na ordem da busca.
    Aresta tracejada (`-.->`) quando `confidence < 1`. Na busca reversa (`impact`), a seta
    continua no sentido do código (quem chama → quem é chamado). Devolve o texto e quantos
    nós ficaram de fora.
    """
    order = {node: index for index, node in enumerate(walk.steps)}
    chosen: list[str] = []
    seen: set[str] = set()
    # Caminho a caminho (folhas mais rasas primeiro): uma folha só entra com o caminho inteiro.
    for leaf in sorted(priority, key=lambda n: (walk.steps[n].depth, order[n])):
        missing = [n for n in walk.path_to(leaf) if n not in seen]
        if len(chosen) + len(missing) > max_nodes:
            continue
        chosen.extend(missing)
        seen.update(missing)
    for node in walk.steps:
        if len(chosen) >= max_nodes:
            break
        parent = walk.steps[node].parent
        if node not in seen and (parent is None or parent in seen):
            chosen.append(node)
            seen.add(node)
    ids = {node: f"n{index}" for index, node in enumerate(sorted(chosen, key=order.__getitem__), 1)}
    lines = ["flowchart LR"]
    for node in sorted(chosen, key=order.__getitem__):
        start, end = _SHAPES.get(labels[node], ('["', '"]'))
        lines.append(f"    {ids[node]}{start}{_mermaid_label(node, labels[node])}{end}")
    for node in sorted(chosen, key=order.__getitem__):
        step = walk.steps[node]
        if step.parent is None or step.parent not in ids:
            continue
        confidence = step.confidence if step.confidence is not None else 1.0
        arrow = "-->" if confidence >= 1.0 else "-.->"
        text = step.edge_type if confidence >= 1.0 else f"{step.edge_type} {confidence:.2f}"
        src, dst = (node, step.parent) if reverse else (step.parent, node)
        lines.append(f"    {ids[src]} {arrow}|{text}| {ids[dst]}")
    return "\n".join(lines), len(walk.steps) - len(chosen)


# --------------------------------------------------------------------------- relatórios


def render_trace(
    result: TraceResult,
    meta: Mapping[str, Any],
    pending: Sequence[Pending],
    max_mermaid_nodes: int = 60,
    cross: CrossTraceResult | None = None,
) -> str:
    leaves = [
        n for n, label in result.labels.items() if label in ("Table", "PgFunction", "RpcCall")
    ]
    diagram, cut = mermaid(result.walk, result.labels, max_mermaid_nodes, priority=leaves)
    tree = _cross_tree(result, cross) if cross else _tree(result.walk)
    return _env.get_template("trace.md.j2").render(
        cross=cross,
        r=result,
        options=result.options,
        meta=meta,
        tree=tree,
        mermaid=diagram,
        cut=cut,
        max_nodes=max_mermaid_nodes,
        pending=list(pending)[:MAX_PENDING],
        pending_total=len(pending),
        pending_kinds=dict(sorted(Counter(p.kind for p in pending).items())),
    )


def _item(step: Step, indent: int, project: str | None, seam: bool = False) -> dict[str, Any]:
    return {
        "indent": "  " * indent,
        "node": step.node,
        "edge": None if seam else step.edge_type,
        "confidence": step.confidence,
        "project": project,
        "seam": seam,
    }


def _tree(walk: Walk, project: str | None = None) -> list[dict[str, Any]]:
    return [_item(step, step.depth, project) for step in walk.dfs_order()]


def _cross_tree(result: TraceResult, cross: CrossTraceResult) -> list[dict[str, Any]]:
    """Árvore do projeto de origem com, abaixo de cada `Endpoint` atravessado, a do outro lado.

    A primeira linha do outro lado leva `seam` (`~>` no relatório): não é chamada local.
    """
    crossings = {c.endpoint: c for c in cross.crossings}
    items: list[dict[str, Any]] = []
    for step in result.walk.dfs_order():
        items.append(_item(step, step.depth, cross.project))
        crossing = crossings.get(step.node)
        if crossing is None:
            continue
        for sub in crossing.walk.dfs_order():
            indent = step.depth + 1 + sub.depth
            items.append(_item(sub, indent, crossing.to_project, seam=sub.parent is None))
    return items


def render_impact(
    result: ImpactResult,
    meta: Mapping[str, Any],
    max_mermaid_nodes: int = 60,
    export: str = "md",
) -> str:
    diagram, cut = mermaid(
        result.walk,
        result.labels,
        max_mermaid_nodes,
        reverse=True,
        priority=[f.node for f in result.files if f.via is None and (f.is_screen or f.is_rpc)],
    )
    if export == "mermaid":
        return f"```mermaid\n{diagram}\n```\n"
    return _env.get_template("impact.md.j2").render(
        r=result,
        options=result.options,
        meta=meta,
        mermaid=diagram,
        cut=cut,
        max_nodes=max_mermaid_nodes,
    )


def output_name(prefix: str, key: str) -> str:
    """`trace_emp4_anularempenho001_php.md` (sem `/` nem `.` no nome)."""
    safe = "".join(ch if ch.isalnum() else "_" for ch in key).strip("_")
    return f"{prefix}_{safe}.md"
