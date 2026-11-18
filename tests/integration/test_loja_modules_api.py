"""A API dos módulos devolve os números de `analytics_*` do banco das fixtures.

Os campos novos (`ownedTables`, `ownedShared`, `coverage`, `writesForeign`, `tablesTotal`) são
recalculados aqui em Python, direto das tabelas, e comparados com a API.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from php_map.analysis.aggregate import NO_MODULE
from php_map.web.app import create_app

LOCAL = "http://127.0.0.1:8765"
MODULES = """query { modules { module name files screens tables ca ce instability weightIn
  weightOut sharedTables ownedTables ownedShared coverage } }"""


@pytest.fixture(scope="module")
def client(shop_db: Path) -> TestClient:
    return TestClient(create_app(shop_db, ui_dir=None), base_url=LOCAL)


def data(client: TestClient, query: str) -> dict[str, Any]:
    body = client.post("/graphql", json={"query": query}).json()
    assert "errors" not in body, body.get("errors")
    return dict(body["data"])


@pytest.fixture(scope="module")
def db(shop_db: Path) -> dict[str, list[tuple[Any, ...]]]:
    with sqlite3.connect(shop_db) as conn:
        return {
            "modules": conn.execute("SELECT * FROM analytics_modules").fetchall(),
            "edges": conn.execute("SELECT src, dst, weight FROM analytics_module_edges").fetchall(),
            "tables": conn.execute(
                "SELECT table_id, owner_modules, writer_modules, shared FROM analytics_tables"
            ).fetchall(),
        }


def test_modules_are_the_analytics_rows(
    client: TestClient, db: dict[str, list[tuple[Any, ...]]]
) -> None:
    api = {m["module"]: m for m in data(client, MODULES)["modules"]}
    assert set(api) == {row[0] for row in db["modules"]}
    for module, name, files, screens, tables, ca, ce, inst, w_in, w_out, shared in db["modules"]:
        row = api[module]
        assert (row["name"], row["files"], row["screens"], row["tables"]) == (
            name,
            files,
            screens,
            tables,
        )
        assert (row["ca"], row["ce"], row["instability"]) == (ca, ce, inst)
        assert (row["weightIn"], row["weightOut"], row["sharedTables"]) == (w_in, w_out, shared)


def test_owned_tables_and_coverage(
    client: TestClient, db: dict[str, list[tuple[Any, ...]]]
) -> None:
    api = {m["module"]: m for m in data(client, MODULES)["modules"]}
    for module in api:
        owned = [t for t in db["tables"] if module in json.loads(t[1])]
        assert api[module]["ownedTables"] == len(owned)
        assert api[module]["ownedShared"] == sum(t[3] for t in owned)
        out = [(dst, w) for src, dst, w in db["edges"] if src == module]
        total = sum(w for _dst, w in out)
        known = sum(w for dst, w in out if dst != NO_MODULE)
        expected = round(known / total, 4) if total else None
        assert api[module]["coverage"] == expected
    assert api["Module:pedidos"]["ownedTables"] >= 2  # pedido, pedidocancelado…


def test_module_detail_writes_foreign_and_hotspots(
    client: TestClient, db: dict[str, list[tuple[Any, ...]]]
) -> None:
    result = data(
        client,
        """query { module(id: "Module:pedidos") { module { ownedTables }
          writesForeign { tableId ownerModules writerModules } hotspots { nodeId module }
          incoming { src weight samples } outgoing { dst kind weight samples } }
          tablesTotal(shared: false) all: tablesTotal }""",
    )
    detail = result["module"]
    expected = sorted(
        t[0]
        for t in db["tables"]
        if "Module:pedidos" in json.loads(t[2])
        and json.loads(t[1])
        and "Module:pedidos" not in json.loads(t[1])
    )
    assert sorted(t["tableId"] for t in detail["writesForeign"]) == expected
    assert all("Module:pedidos" in h["module"].split(",") for h in detail["hotspots"])
    assert result["all"] == len(db["tables"])
    assert result["tablesTotal"] == sum(1 for t in db["tables"] if not t[3])
