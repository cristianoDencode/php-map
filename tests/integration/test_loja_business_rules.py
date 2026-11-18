"""Ficha de regras de negócio sobre o banco das fixtures, pela API GraphQL."""

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from php_map.web.app import create_app

LOCAL = "http://127.0.0.1:8765"
ENTRY = "ped4_cancelarpedido001.php"

RULES = """query($entry: String!, $depth: Int = 4) {
  businessRules(entry: $entry, depth: $depth) {
    entry depth minConfidence clamped
    modules { module name source confidence }
    menus { id path paths help desctec active params }
    actions { file cases }
    participants { node label name depth via parent confidence evidence }
    writes { table name operations catalog occurrences
             columnsLabeled { column label description } evidence { path line from } }
    reads { table operations evidence { path line from } }
    rpc { function name resolved evidence { path line from } }
    triggers { table function evidence { path line from } }
    messages { text kind category file line scope }
    notes { text file line scope }
    stats { files methods tables messages notes truncated }
  }
}"""


@pytest.fixture(scope="module")
def client(shop_db: Path) -> TestClient:
    return TestClient(create_app(shop_db, ui_dir=None), base_url=LOCAL)


def run(client: TestClient, entry: str = ENTRY, **variables: Any) -> dict[str, Any]:
    response = client.post(
        "/graphql", json={"query": RULES, "variables": {"entry": entry, **variables}}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def rules(client: TestClient, entry: str = ENTRY, **variables: Any) -> dict[str, Any]:
    body = run(client, entry, **variables)
    assert "errors" not in body, body.get("errors")
    result: dict[str, Any] = body["data"]["businessRules"]
    return result


@pytest.fixture(scope="module")
def sheet(client: TestClient) -> dict[str, Any]:
    return rules(client)


def _message(sheet: dict[str, Any], text: str) -> dict[str, Any]:
    found = [m for m in sheet["messages"] if m["text"] == text]
    assert found, f"mensagem ausente: {text}"
    return found[0]


# --------------------------------------------------------------------------- exemplos da tarefa


def test_menu_of_the_entry(sheet: dict[str, Any]) -> None:
    assert sheet["entry"] == f"File:{ENTRY}"
    assert (sheet["depth"], sheet["minConfidence"]) == (4, 0.5)
    [menu] = sheet["menus"]
    assert menu["id"] == "BusinessAction:menu:7001"
    assert menu["path"] == "Pedidos > Procedimentos > Cancelar Pedido"
    assert menu["desctec"] == "Cancela um pedido e devolve o estoque" and menu["active"] is True
    assert sheet["modules"][0]["module"] == "Module:pedidos"


def test_validation_message_of_the_form(sheet: dict[str, Any]) -> None:
    message = _message(sheet, "Não há pedido selecionado para cancelar.")
    assert message == {
        "text": "Não há pedido selecionado para cancelar.",
        "kind": "js_string",
        "category": "validação",
        "file": "forms/db_frmcancelarpedido.php",
        "line": 31,
        "scope": "",
    }


def test_dispatch_action_and_error_message_of_the_backend(sheet: dict[str, Any]) -> None:
    [action] = [a for a in sheet["actions"] if a["file"] == "ped4_baixaestoque004.php"]
    assert action["cases"] == ["cancelar", "getItens", "getEntregas"]
    message = _message(sheet, "Erro ao cancelar o pedido.")
    assert (message["category"], message["file"], message["line"]) == (
        "erro",
        "ped4_baixaestoque004.php",
        30,
    )


def test_rpc_of_the_business_class(sheet: dict[str, Any]) -> None:
    [rpc] = [r for r in sheet["rpc"] if r["name"] == "fc_saldo_estoque"]
    assert rpc["evidence"] == {
        "path": "classes/pedido.php",
        "line": 24,
        "from": "Method:pedido::cancelar",
    }


def test_insert_into_the_cancellation_table_with_dictionary_labels(sheet: dict[str, Any]) -> None:
    [table] = [t for t in sheet["writes"] if t["table"] == "Table:public.pedidocancelado"]
    assert table["operations"] == ["INSERT"]
    assert table["catalog"]["description"] == "Cancelamentos de pedido"
    assert {
        "column": "can01_codigo",
        "label": "Cancelamento",
        "description": "Código do cancelamento",
    } in (table["columnsLabeled"])
    assert table["evidence"][0] == {
        "path": "classes/db_pedidocancelado_classe.php",
        "line": 14,
        "from": "Method:cl_pedidocancelado::incluir",
    }
    assert all(len(t["evidence"]) <= 5 for t in sheet["writes"] + sheet["reads"])


def test_trigger_of_the_written_table(sheet: dict[str, Any]) -> None:
    assert {
        "table": "Table:public.pedido",
        "function": "PgFunction:public.tg_pedido_auditoria()",
        "evidence": {"path": "loja_mini.sql", "line": 113, "from": "Table:public.pedido"},
    } in sheet["triggers"]


def test_reads_and_infra_tables(sheet: dict[str, Any]) -> None:
    reads = {t["table"]: t for t in sheet["reads"]}
    assert reads["Table:public.pedido"]["operations"] == ["SELECT"]
    written = {t["table"] for t in sheet["writes"]}
    assert "Table:public.auditoria_loja" not in written | set(reads)  # só o trigger a grava


# --------------------------------------------------------------------------- regras gerais


def test_no_text_from_infrastructure_files(shop_db: Path, sheet: dict[str, Any]) -> None:
    with sqlite3.connect(shop_db) as conn:
        infra = {
            node_id.partition(":")[2]
            for node_id, attrs in conn.execute("SELECT id, attrs FROM nodes WHERE label = 'File'")
            if json.loads(attrs).get("is_infra")
        }
        infra_texts = conn.execute(
            f"SELECT count(*) FROM search_texts WHERE file_id IN ({','.join('?' * len(infra))})",
            tuple(f"File:{p}" for p in infra),
        ).fetchone()[0]
    assert infra and infra_texts > 0  # há textos de infraestrutura no índice…
    assert not {m["file"] for m in sheet["messages"] + sheet["notes"]} & infra  # …e não na ficha
    assert not any(p["node"].removeprefix("File:") in infra for p in sheet["participants"])


def test_texts_come_only_from_visited_scopes(sheet: dict[str, Any]) -> None:
    texts = {m["text"] for m in sheet["messages"]}
    assert {"Pedido sem itens.", "Erro ao cancelar o pedido."} <= texts
    files = {p["node"].removeprefix("File:") for p in sheet["participants"] if p["label"] == "File"}
    assert {m["file"] for m in sheet["messages"]} <= files  # nada de arquivo fora do fluxo
    assert all(m["kind"] in ("literal", "js_string") for m in sheet["messages"])


def test_every_item_has_file_and_line(sheet: dict[str, Any]) -> None:
    for item in sheet["messages"] + sheet["notes"]:
        assert item["file"] and item["line"] > 0
    for table in sheet["writes"] + sheet["reads"]:
        assert table["evidence"] and all(e["path"] and e["line"] for e in table["evidence"])
    assert all(r["evidence"]["path"] and r["evidence"]["line"] for r in sheet["rpc"])


def test_notes_have_at_least_four_words(sheet: dict[str, Any]) -> None:
    notes = {n["text"] for n in sheet["notes"]}
    assert "Regras de negocio do pedido (inventado)." in notes
    assert "revisado hoje" not in notes  # 2 palavras, em `pedido::cancelar`
    assert all(len(n.split()) >= 4 for n in notes)


def test_participants_and_stats(sheet: dict[str, Any]) -> None:
    by_node = {p["node"]: p for p in sheet["participants"]}
    assert by_node[f"File:{ENTRY}"]["depth"] == 0
    form = by_node["File:forms/db_frmcancelarpedido.php"]
    assert (form["depth"], form["via"], form["parent"]) == (1, "REQUIRES", f"File:{ENTRY}")
    assert form["evidence"] == {"path": ENTRY, "line": 43, "expr_kind": "modification"}
    assert by_node["File:ped4_baixaestoque004.php"]["via"] == "REFERENCES"
    assert {p["label"] for p in sheet["participants"]} <= {"File", "Class", "Method", "Function"}
    stats = sheet["stats"]
    assert stats["messages"] == len(sheet["messages"]) and stats["notes"] == len(sheet["notes"])
    assert stats["files"] == sum(1 for p in sheet["participants"] if p["label"] == "File")
    assert stats["tables"] == len({t["table"] for t in sheet["writes"] + sheet["reads"]})
    assert stats["truncated"] is False


def test_depth_controls_the_flow(client: TestClient, sheet: dict[str, Any]) -> None:
    shallow = rules(client, depth=1)
    texts = {m["text"] for m in shallow["messages"]}
    assert "Não há pedido selecionado para cancelar." in texts  # o form é nível 1
    assert "Erro ao cancelar o pedido." not in texts  # o backend AJAX é nível 2
    assert shallow["stats"]["files"] < sheet["stats"]["files"]
    deep = rules(client, depth=99)
    assert deep["depth"] == 10 and deep["clamped"] == ["depth: 99 → 10"]


def test_entry_as_node_id(client: TestClient, sheet: dict[str, Any]) -> None:
    assert rules(client, f"File:{ENTRY}")["stats"] == sheet["stats"]


def test_unknown_entry_has_suggestions(client: TestClient) -> None:
    body = run(client, "ped4_cancelarpedido.php")
    assert "Parecidos: ped4_cancelarpedido001.php" in body["errors"][0]["message"]


def test_old_database_without_texts(shop_db: Path, tmp_path: Path) -> None:
    old = tmp_path / "antigo.db"
    shutil.copy(shop_db, old)
    with sqlite3.connect(old) as conn:
        conn.execute("DROP TABLE search_texts")
    client = TestClient(create_app(old, ui_dir=None), base_url=LOCAL)
    message = run(client)["errors"][0]["message"]
    assert "rode o scan de novo para gerar os textos da ficha" in message
