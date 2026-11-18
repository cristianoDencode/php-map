"""`REFERENCES` a partir de literais `*.php` em strings/HTML e `dispatch_actions`."""

from collections.abc import Collection

from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    STRING_REF_CONFIDENCE,
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    file_id,
)
from php_map.ingest.records import PhpFileRecord
from php_map.resolve.includes import resolve_file


def resolve_references(
    record: PhpFileRecord, files: Collection[str], builder: GraphBuilder
) -> None:
    """Literal que não aponta para arquivo existente é descartado (ruído alto)."""
    path = record.path
    for ref in record.string_refs:
        target = resolve_file(ref.target, path, files)
        if target is None or target == path:
            continue
        builder.add_edge(
            Edge(
                file_id(path),
                file_id(target),
                EdgeType.REFERENCES,
                EdgeSource.STRING_REF,
                STRING_REF_CONFIDENCE,
                {"path": path, "line": ref.line, "origin": ref.origin},
            )
        )
    actions = dispatch_actions(record)
    if actions:
        builder.add_node(Node(file_id(path), NodeLabel.FILE, {"dispatch_actions": actions}))


def dispatch_actions(record: PhpFileRecord) -> list[str]:
    """Literais dos `case` de `switch ($oParam->exec | $objJson->method)`, sem repetição."""
    return list(dict.fromkeys(case for item in record.dispatch for case in item.cases))
