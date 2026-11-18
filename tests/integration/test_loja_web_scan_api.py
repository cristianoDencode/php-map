"""Mutações do scan pela interface com o `TestClient`.

Cada teste usa um `out` próprio (o `ui-settings.json` fica ali), e o banco das fixtures é
só lido. O teste de ponta a ponta roda o `scan` de verdade num subprocesso, sobre as
fixtures com o cache de AST semeado (sem Docker).
"""

import json
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from php_map.config import Settings
from php_map.errors import IngestError
from php_map.ingest import ast_cache, php_extractor
from php_map.web.app import create_app
from php_map.web.settings import UiSettingsStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MINI = FIXTURES / "loja-mini"
DDL = FIXTURES / "schema/loja_mini.sql"
LOCAL = "http://127.0.0.1:8765"
JSON_TYPE = {"content-type": "application/json"}

SET_SOURCE = "mutation($path: String!) { setSource(path: $path) { source origin isEcidade } }"
START = """mutation($input: ScanInput!) { startScan(input: $input) { id state } }"""
JOB = """query { scanJob { id state stage stageLabel stages { stage label at } startedAt
  finishedAt elapsedSeconds logTail result error } }"""


def make_client(db: Path, out: Path, **kwargs: Any) -> TestClient:
    return TestClient(create_app(db, ui_dir=None, out_dir=out, **kwargs), base_url=LOCAL)


def post(
    client: TestClient, query: str, headers: dict[str, str] | None = None, **variables: Any
) -> dict[str, Any]:
    response = client.post(
        "/graphql",
        json={"query": query, "variables": variables},
        headers={"origin": LOCAL, **(headers or {})},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def error(body: dict[str, Any]) -> str:
    assert body.get("errors"), body
    message: str = body["errors"][0]["message"]
    return message


@pytest.fixture
def client(shop_db: Path, tmp_path: Path) -> TestClient:
    return make_client(shop_db, tmp_path / "out")


# --------------------------------------------------------------------------- origem


def test_mutation_without_origin_is_refused(client: TestClient) -> None:
    response = client.post("/graphql", json={"query": SET_SOURCE, "variables": {"path": "x"}})
    assert error(response.json()) == "operação recusada: origem não permitida"


def test_mutation_from_another_site_is_refused(client: TestClient) -> None:
    body = post(client, SET_SOURCE, {"origin": "https://exemplo.com"}, path=str(MINI))
    assert error(body) == "operação recusada: origem não permitida"


@pytest.mark.parametrize(
    "content_type", ["application/x-www-form-urlencoded", "text/plain", "multipart/form-data"]
)
def test_mutation_with_form_content_type_is_refused(client: TestClient, content_type: str) -> None:
    query = f'mutation {{ setSource(path: "{MINI.as_posix()}") {{ source }} }}'
    response = client.post(
        "/graphql",
        content=json.dumps({"query": query}),
        headers={"origin": LOCAL, "content-type": content_type},
    )
    # O Strawberry recusa o corpo (4xx) ou a guarda recusa a mutação: nunca executa.
    if response.status_code == 200:
        assert error(response.json()) == "operação recusada: origem não permitida"
    else:
        assert 400 <= response.status_code < 500
    assert not (client.app.state.ecmap.settings.path).exists()  # type: ignore[attr-defined]


@pytest.mark.parametrize("origin", [LOCAL, "http://localhost:8765", "http://localhost:5173"])
def test_local_origin_is_accepted(client: TestClient, origin: str) -> None:
    body = post(client, SET_SOURCE, {"origin": origin}, path=str(MINI))
    assert "errors" not in body, body
    assert body["data"]["setSource"] == {
        "source": str(MINI.resolve()),
        "origin": "configuração",
        "isEcidade": True,
    }


def test_read_only_refuses_mutations(shop_db: Path, tmp_path: Path) -> None:
    client = make_client(shop_db, tmp_path / "out", read_only=True)
    assert error(post(client, SET_SOURCE, path=str(MINI))) == "servidor em modo somente leitura"
    assert error(post(client, "mutation { cancelScan { id } }")) == (
        "servidor em modo somente leitura"
    )
    defaults = post(client, "query { scanDefaults { readOnly } }")["data"]["scanDefaults"]
    assert defaults == {"readOnly": True}


# --------------------------------------------------------------------------- setSource


def test_set_source_refuses_folder_without_marker(client: TestClient, tmp_path: Path) -> None:
    message = error(post(client, SET_SOURCE, path=str(tmp_path)))
    assert "perfil 'ecidade'" in message and "libs/db_stdlib.php" in message
    assert "inexistente" in error(post(client, SET_SOURCE, path=str(tmp_path / "nao")))


def test_set_source_overrides_the_source_argument(shop_db: Path, tmp_path: Path) -> None:
    other = tmp_path / "outro-ecidade"
    shutil.copytree(MINI, other)
    client = make_client(shop_db, tmp_path / "out", source=other)
    ws = post(client, "query { workspace { source origin } }")["data"]["workspace"]
    assert ws == {"source": str(other.resolve()), "origin": "argumento"}

    post(client, SET_SOURCE, path=str(MINI))
    ws = post(client, "query { workspace { source origin } }")["data"]["workspace"]
    assert ws == {"source": str(MINI.resolve()), "origin": "configuração"}
    code = post(client, 'query { sourceFile(path: "ped4_cancelarpedido001.php") { totalLines } }')
    assert code["data"]["sourceFile"]["totalLines"] > 0  # o painel "Código" segue a pasta nova
    saved = UiSettingsStore(tmp_path / "out").load()
    assert saved.source == str(MINI.resolve()) and saved.recent_sources == [saved.source]


# --------------------------------------------------------------------------- startScan


def test_start_scan_refuses_missing_schema_and_catalog_without_dsn(
    client: TestClient, tmp_path: Path
) -> None:
    missing = str(tmp_path / "nao-existe.sql")
    body = post(
        client, START, input={"source": str(MINI), "schema": [missing], "catalogFromEnv": False}
    )
    assert "schema inexistente" in error(body)
    body = post(client, START, input={"source": str(MINI), "catalogFromEnv": True})
    assert "PHP_MAP_CATALOG_DSN" in error(body)
    assert post(client, JOB)["data"]["scanJob"] is None
    assert "nenhum scan" in error(post(client, "mutation { cancelScan { id } }"))


def test_scan_defaults(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_docker(_settings: Settings) -> None:
        raise IngestError("Docker não encontrado no PATH")

    monkeypatch.setattr(php_extractor, "check_environment", no_docker)
    query = """query { scanDefaults { source schema catalogFromEnv catalogAvailable reuseAst
      dockerAvailable dockerMessage recentSources readOnly } }"""
    defaults = post(client, query)["data"]["scanDefaults"]
    assert defaults == {
        "source": str(MINI.resolve()),  # a pasta do grafo, antes de qualquer escolha
        "schema": [],
        "catalogFromEnv": False,  # sem .env nos testes
        "catalogAvailable": False,
        "reuseAst": True,
        "dockerAvailable": False,
        "dockerMessage": "Docker não encontrado no PATH",
        "recentSources": [],
        "readOnly": False,
    }


SCHEMA_DEFAULTS = "query { scanDefaults { schema schemaSource schemaDbAvailable } }"


def test_scan_defaults_schema_source(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sem DSN e sem caminhos → none; com caminhos salvos → files; com DSN → db."""
    defaults = post(client, SCHEMA_DEFAULTS)["data"]["scanDefaults"]
    assert defaults == {"schema": [], "schemaSource": "none", "schemaDbAvailable": False}

    store = UiSettingsStore(tmp_path / "out")  # arquivo antigo: sem `schema_source`
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"schema": [str(DDL)]}), encoding="utf-8")
    defaults = post(client, SCHEMA_DEFAULTS)["data"]["scanDefaults"]
    assert defaults["schemaSource"] == "files" and defaults["schema"] == [str(DDL)]

    monkeypatch.setenv("PHP_MAP_CATALOG_DSN", "host=db.exemplo password=segredo")
    defaults = post(client, SCHEMA_DEFAULTS)["data"]["scanDefaults"]
    assert defaults["schemaSource"] == "db" and defaults["schemaDbAvailable"] is True
    store.update(schema_source="files")  # a escolha salva vale por cima do padrão
    assert post(client, SCHEMA_DEFAULTS)["data"]["scanDefaults"]["schemaSource"] == "files"
    assert "segredo" not in json.dumps(post(client, SCHEMA_DEFAULTS))


def test_start_scan_schema_source_is_validated(client: TestClient) -> None:
    body = post(
        client, START, input={"source": str(MINI), "schemaSource": "db", "catalogFromEnv": False}
    )
    assert "schema do banco indisponível" in error(body)
    body = post(
        client, START, input={"source": str(MINI), "schemaSource": "x", "catalogFromEnv": False}
    )
    assert "schemaSource aceita db, files ou none" in error(body)
    assert post(client, JOB)["data"]["scanJob"] is None


def test_scan_end_to_end_switches_the_graph(shop_db: Path, tmp_path: Path) -> None:
    """O `scan` real num subprocesso: o grafo novo passa a ser servido sem reiniciar."""
    out = tmp_path / "out"
    ast_dir = ast_cache.cache_dir(out, MINI)
    ast_dir.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "ast/loja-mini.jsonl", ast_dir / ast_cache.CACHE_NAME)
    ast_cache.write_meta(ast_dir, ast_cache.current_meta(MINI, Settings()))
    client = make_client(shop_db, out, scan_command=[sys.executable, "-m", "php_map"])
    before = post(client, "query { stats }")["data"]["stats"]
    assert before["served"] == {"from_interface": False}

    body = post(
        client,
        START,
        input={"source": str(MINI), "schema": [str(DDL)], "catalogFromEnv": False},
    )
    assert "errors" not in body, body
    assert body["data"]["startScan"]["state"] == "running"

    deadline = time.monotonic() + 120
    while (job := post(client, JOB)["data"]["scanJob"])["state"] == "running":
        assert time.monotonic() < deadline, job
        time.sleep(0.2)
    assert job["state"] == "succeeded", job
    assert "extract" not in [s["stage"] for s in job["stages"]]  # tudo do cache
    assert job["stages"][0]["stage"] == "discovery" and job["stages"][-1]["stage"] == "analytics"
    result = job["result"]
    db = Path(result["db"])
    assert db.parent == (out / "grafos").resolve() and db.is_file()

    after = post(client, "query { stats }")["data"]["stats"]
    assert after["served"] == {"from_interface": True}
    assert after["meta"]["source"] == str(MINI.resolve())
    with closing(sqlite3.connect(db)) as conn:
        nodes = conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
    assert result["summary"]["nodes"] == nodes == sum(after["nodes"].values())
    assert UiSettingsStore(out).load().last_db == str(db)


# --------------------------------------------------------------------------- cache por pasta

AST_CACHE = """query($source: String!) { astCache(source: $source) {
  exists reusable files updatedAt reason message } }"""


def _seed(out: Path, source: Path) -> None:
    ast_dir = ast_cache.cache_dir(out, source)
    ast_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / "ast/loja-mini.jsonl", ast_dir / ast_cache.CACHE_NAME)
    meta = ast_cache.current_meta(source, Settings())
    ast_cache.write_meta(
        ast_dir, replace(meta, files=35181, updated_at="2026-09-25T12:41:02-03:00")
    )


def test_ast_cache_query(client: TestClient, tmp_path: Path) -> None:
    out = tmp_path / "out"
    none = post(client, AST_CACHE, source=str(MINI))["data"]["astCache"]
    assert none["exists"] is False and none["reason"] == "no_cache"
    assert none["message"].startswith("Sem extração desta pasta")

    _seed(out, MINI)
    found = post(client, AST_CACHE, source=str(MINI))["data"]["astCache"]
    assert found == {
        "exists": True,
        "reusable": True,
        "files": 35181,
        "updatedAt": "2026-09-25T12:41:02-03:00",
        "reason": "reusable",
        "message": "Há extração desta pasta: 35.181 arquivos (25/09/2026 12:41). "
        "Só os arquivos novos ou alterados voltam ao extrator.",
    }

    meta_path = ast_cache.cache_dir(out, MINI) / ast_cache.META_NAME
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta_path.write_text(json.dumps({**meta, "extractor_hash": "outro"}), encoding="utf-8")
    stale = post(client, AST_CACHE, source=str(MINI))["data"]["astCache"]
    assert (stale["exists"], stale["reusable"], stale["reason"]) == (True, False, "extractor_hash")
    assert "outra versão do extrator" in stale["message"]

    assert error(post(client, AST_CACHE, source="   ")) == "informe a pasta do e-cidade"


def test_scan_two_folders_in_the_same_out(shop_db: Path, tmp_path: Path) -> None:
    """A, B e A de novo pela API, no mesmo `out` (antes: "cache de outra origem")."""
    out, other = tmp_path / "out", tmp_path / "outra" / "loja-mini"
    shutil.copytree(MINI, other)
    _seed(out, MINI)
    _seed(out, other)
    client = make_client(shop_db, out, scan_command=[sys.executable, "-m", "php_map"])

    for folder in (MINI, other, MINI):
        body = post(client, START, input={"source": str(folder), "catalogFromEnv": False})
        assert "errors" not in body, body
        deadline = time.monotonic() + 120
        while (job := post(client, JOB)["data"]["scanJob"])["state"] == "running":
            assert time.monotonic() < deadline, job
            time.sleep(0.2)
        assert job["state"] == "succeeded", job
        assert "extract" not in [s["stage"] for s in job["stages"]]  # tudo do cache da pasta
        db = Path(job["result"]["db"])
        assert (db.parent / f"{db.stem}.scan_summary.json").is_file()
    assert not (out / "scan_summary.json").exists()
    assert sorted(p.name for p in (out / "ast").iterdir()) == sorted(
        [ast_cache.cache_key(MINI), ast_cache.cache_key(other)]
    )
