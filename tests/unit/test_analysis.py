"""Busca e diagrama sobre um grafo sintético."""

import networkx as nx
import pytest

from php_map.analysis import require_node, walk
from php_map.errors import QueryError
from php_map.report.render import mermaid, output_name


def _graph() -> "nx.MultiDiGraph[str]":
    graph: nx.MultiDiGraph[str] = nx.MultiDiGraph()
    for node, label, infra in [
        ("File:a.php", "File", False),
        ("File:b.php", "File", False),
        ('File:c"q.php', "File", False),
        ("Function:db_query", "Function", True),
        ("Table:public.t", "Table", False),
    ]:
        graph.add_node(node, label=label, is_infra=infra)

    def edge(src: str, dst: str, type_: str, confidence: float = 1.0) -> None:
        graph.add_edge(
            src,
            dst,
            key=type_,
            type=type_,
            confidence=confidence,
            evidence={"path": src, "line": 1},
        )

    edge("File:a.php", "File:b.php", "REFERENCES", 0.9)
    edge("File:a.php", "Function:db_query", "CALLS")
    edge("File:a.php", 'File:c"q.php', "DECLARES")
    edge("File:b.php", "Table:public.t", "QUERIES")
    return graph


def test_walk_filters_confidence_infra_and_untraversed_edges() -> None:
    graph = _graph()
    default = walk(graph, "File:a.php", depth=5, min_confidence=0.0, include_infra=False)
    assert list(default.steps) == ["File:a.php", "File:b.php", "Table:public.t"]
    assert default.path_to("Table:public.t") == ["File:a.php", "File:b.php", "Table:public.t"]
    strict = walk(graph, "File:a.php", depth=5, min_confidence=1.0, include_infra=True)
    assert list(strict.steps) == ["File:a.php", "Function:db_query"]  # DECLARES nunca
    shallow = walk(graph, "File:a.php", depth=1, min_confidence=0.0, include_infra=False)
    assert "Table:public.t" not in shallow.steps
    reverse = walk(
        graph, "Table:public.t", depth=5, min_confidence=0.0, include_infra=False, reverse=True
    )
    assert list(reverse.steps) == ["Table:public.t", "File:b.php", "File:a.php"]


def test_mermaid_escapes_quotes_and_dashes_low_confidence() -> None:
    graph = _graph()
    graph.add_edge("File:b.php", 'File:c"q.php', key="INCLUDES", type="INCLUDES", confidence=1.0)
    result = walk(graph, "File:a.php", depth=5, min_confidence=0.0, include_infra=False)
    labels = {n: graph.nodes[n]["label"] for n in result.steps}
    text, cut = mermaid(result, labels, 10)
    assert cut == 0
    assert 'n2["File: b.php"]' in text and "#quot;" in text and '"q' not in text
    assert "n1 -.->|REFERENCES 0.90| n2" in text
    assert 'n3[("Table: public.t")]' in text or 'n4[("Table: public.t")]' in text


def test_require_node_suggests_similar_or_partial_names() -> None:
    graph = _graph()
    candidates = [n for n in graph if n.startswith("File:")]
    require_node(graph, "File:a.php", candidates, "arquivo")
    with pytest.raises(QueryError, match="Parecidos: a.php"):
        require_node(graph, "File:aa.php", candidates, "arquivo")
    with pytest.raises(QueryError, match="não encontrado") as info:
        require_node(graph, "File:zzz.php", candidates, "arquivo")
    assert "Parecidos" not in str(info.value)


def test_output_name_has_no_path_separators() -> None:
    assert output_name("trace", "forms/db_frm.php") == "trace_forms_db_frm_php.md"
    assert output_name("impact", "public.empempenho") == "impact_public_empempenho.md"
