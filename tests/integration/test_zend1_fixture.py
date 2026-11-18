"""Scan de ponta a ponta de `tests/fixtures/zend1-mini` com `--profile zend1`.

Com Docker (extrator de verdade): confere módulo por diretório, `Route`/`RENDERS` e a resolução
de classe "de graça" (o índice de classes é em minúsculas) sobre o fixture criado por
`scripts/build_fixtures_zend1_mini.py`. `tests/unit/test_resolve_zend1.py` já cobre
`apply_zend1_structure` sem Docker, com registros sintéticos; este teste é a prova de que a
extração real (`EXTENDS` pela pasta `library/` do próprio módulo, que o `EXCLUDE_DIRS`
não pode apagar) chega ao mesmo resultado.
"""

import json
from pathlib import Path

import pytest

from php_map.config import Settings
from php_map.errors import IngestError
from php_map.graph import store
from php_map.graph.model import method_id
from php_map.ingest import php_extractor
from php_map.pipeline import ScanOptions, run_scan

ZEND1_MINI = Path(__file__).resolve().parents[1] / "fixtures/zend1-mini"
ZEND1_MINI_SCHEMA = Path(__file__).resolve().parents[1] / "fixtures/schema/zend1_mini.sql"
EXPECTED = Path(__file__).resolve().parents[1] / "fixtures/expected/zend1_mini_edges.json"


@pytest.mark.docker
def test_real_scan_of_the_zend1_mini_fixture(tmp_path: Path) -> None:
    settings = Settings()
    try:
        php_extractor.check_environment(settings)
    except IngestError as exc:
        pytest.skip(str(exc))

    out = tmp_path / "out"
    options = ScanOptions(
        source=ZEND1_MINI,
        schema=(ZEND1_MINI_SCHEMA,),
        out_dir=out,
        profile="zend1",
        analytics=False,
    )
    summary = run_scan(options)

    assert summary.project == {"slug": "zend1-mini", "name": "zend1-mini", "profile": "zend1"}
    # application.ini não é .php/.phtml: os outros 18 arquivos do fixture entram no scan.
    assert summary.files["total"] == 18
    assert summary.files["by_status"]["ok"] == 18

    assert summary.zend1 is not None
    assert summary.zend1["modules"] == 2  # fiscal, webservice
    assert summary.zend1["files_in_module"] == 14
    assert summary.zend1["files_outside_modules"] == 4  # Bootstrap.php e 3 em entidades/
    # consultaAction (Pedido), consultarAction (Cancelamento) e integracaoAction (webservice/index)
    assert summary.zend1["routes"] == 3
    assert summary.zend1["renders"] == 1
    assert summary.zend1["orphan_views"] == 1

    # 3 entidades (2 com @Table, 1 por convenção) e a DQL do recorte (3 verbos).
    assert summary.doctrine is not None
    assert summary.doctrine["entities"] == 3
    assert (summary.doctrine["mapped_explicit"], summary.doctrine["mapped_convention"]) == (2, 1)
    assert summary.doctrine["table_missing"] == 0
    assert summary.sql["dql_strings"] == 3 and summary.sql["dql_edges"] == 3

    # 3 chamadas a Ecidade::consultar|processar (2 literais, 1 em variável) e a classe
    # servida por SoapServer (3 métodos públicos; o protected não entra).
    assert summary.endpoints is not None
    assert summary.endpoints["remote_calls"] == 3 and summary.endpoints["remote_calls_dynamic"] == 1
    assert summary.endpoints["called_endpoints"] == 2
    assert summary.endpoints["served_classes"] == 1 and summary.endpoints["served_by_class"] == 3
    assert summary.unresolved == {"dynamic_endpoint": 1}

    graph = store.load(out / "graph.db")
    edges = {(u, d["type"], v, d["source"]) for u, v, d in graph.edges(data=True)}
    confidences = {
        (u, d["type"], v, d["source"]): d["confidence"] for u, v, d in graph.edges(data=True)
    }
    assert (
        "Class:fiscal_pedidocontroller",
        "EXTENDS",
        "Class:fiscal_lib_controller_abstractcontroller",
        "PHP_AST",
    ) in edges
    consultar = method_id("Fiscal_CancelamentoPedidoController", "consultarAction")
    route = "Route:fiscal/cancelamento-pedido/consultar"
    assert (route, "ROUTES_TO", consultar, "ZEND1_RESOLVER") in edges
    view = "File:application/modules/fiscal/views/scripts/cancelamento-pedido/consultar.phtml"
    assert (consultar, "RENDERS", view, "ZEND1_RESOLVER") in edges
    zend_controller = "File:application/modules/fiscal/controllers/PedidoController.php"
    assert (zend_controller, "BELONGS_TO", "Module:fiscal", "ZEND1_RESOLVER") in edges
    assert not any(
        src == "File:application/Bootstrap.php" and type_ == "BELONGS_TO"
        for src, type_, _dst, _source in edges
    )

    # Gabarito exato (fora `DECLARES`): nada a mais, nada a menos. Inclui
    # `Zend_*`/`Twitter_*`, que não viram `class_not_found` (pasta de terceiros, ver `calls.py`).
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    assert {(e["src"], e["type"], e["dst"], e["source"]) for e in expected} == {
        edge for edge in edges if edge[1] != "DECLARES"
    }
    for row in expected:  # a confiança também é exata: 1.0, e 0.8 só na entidade sem @Table
        key = (row["src"], row["type"], row["dst"], row["source"])
        assert confidences[key] == row["min_confidence"], key
    # Nenhuma `Table` com nome de classe (a DQL não vira tabela falsa) e nenhuma pendência.
    tables = {n for n, d in graph.nodes(data=True) if d.get("label") == "Table"}
    assert tables == {
        "Table:public.arquivo_alerta",
        "Table:public.contas_contabeis",
        "Table:public.cliente",
        "Table:public.pedido",
        "Table:public.pedido_itens",
    }
    pending = [
        json.loads(line)
        for line in (out / "unresolved.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(p["kind"], p["hint"]) for p in pending] == [
        ("dynamic_endpoint", "method:fiscal_model_consultaempresa::pornome")
    ]
