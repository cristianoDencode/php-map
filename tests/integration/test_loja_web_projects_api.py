"""API de projetos e da tela Comunicação com o `TestClient`.

Dois grafos sintéticos persistidos (`tests/cross_graphs.py`: `ecidade-t` serve, `zend-t` chama)
e, no fim, um scan de projeto de verdade num subprocesso sobre as fixtures (sem Docker).
"""

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from cross_graphs import (
    GET_A,
    GET_B,
    META_CHAMA,
    META_SERVE,
    MISSING,
    PRESTACAO,
    PROC_A,
    RPC,
    build_chama,
    build_serve,
)
from cross_graphs import save as save_graph
from fastapi.testclient import TestClient

from php_map.config import Settings
from php_map.ingest import ast_cache
from php_map.web.app import create_app
from php_map.web.projects import Project, ProjectStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MINI = FIXTURES / "loja-mini"
DDL = FIXTURES / "schema/loja_mini.sql"
LOCAL = "http://127.0.0.1:8765"
SECRET = "postgresql://usuario:segredo-da-senha@192.0.2.10:5433/exemplo"

PROJECT_FIELDS = """slug name profile source sourceExists hasGraph lastScan schemaFromDb dsnEnv
  dsnAvailable active scanning"""
ADD = f"""mutation($input: ProjectInput!) {{ addProject(input: $input) {{ {PROJECT_FIELDS} }} }}"""
UPDATE = f"""mutation($slug: String!, $input: ProjectUpdate!) {{
  updateProject(slug: $slug, input: $input) {{ {PROJECT_FIELDS} }} }}"""
REMOVE = "mutation($slug: String!) { removeProject(slug: $slug) { slug } }"
ACTIVATE = f"mutation($slug: String!) {{ setActiveProject(slug: $slug) {{ {PROJECT_FIELDS} }} }}"
PROJECTS = f"query {{ projects {{ {PROJECT_FIELDS} }} activeProject {{ slug }} }}"
STATS = "query { stats }"
SUGGEST = """query($source: String!) { suggestProfile(source: $source) {
  exists profile markers { profile marker found } } }"""

ROW = """endpointId nodeLabel system protocol category kind name direction resolved
  params { name required type source } implementedBy { node path line } callers
  callerSample { project node path line module occurrences } module status
  reaches { tables modules nodes }"""
COMMUNICATION = f"""query($system: String, $protocol: String, $direction: String, $module: String,
  $search: String, $limit: Int, $offset: Int) {{
  communication(system: $system, protocol: $protocol, direction: $direction, module: $module,
    search: $search, limit: $limit, offset: $offset) {{
    total offset limit crossNote
    counters {{ serves calls withParams orphans crossAvailable }}
    systems {{ value count }} protocols {{ value count }} modules {{ value count }}
    items {{ {ROW} }} }} }}"""
DETAIL = f"""query($id: ID!) {{ endpointDetail(id: $id) {{
  row {{ {ROW} }}
  callers {{ project node path line module args occurrences }}
  attrs
  counterpart {{ available message status servedBy system
    servers {{ node path line }} params {{ name required type source }}
    reaches {{ tables modules nodes }}
    callers {{ project node path line module occurrences }} }} }} }}"""


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


def data(body: dict[str, Any]) -> Any:
    assert "errors" not in body, body
    return body["data"]


def error(body: dict[str, Any]) -> str:
    assert body.get("errors"), body
    message: str = body["errors"][0]["message"]
    return message


def folder(root: Path, name: str, marker: str) -> Path:
    path = root / name
    (path / marker).parent.mkdir(parents=True)
    (path / marker).write_text("x\n", encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    """Os dois projetos já cadastrados, cada um com o grafo (servem e chamam um ao outro)."""
    serve = save_graph(tmp_path, "g-serve", build_serve(), META_SERVE)
    chama = save_graph(tmp_path, "g-chama", build_chama(), META_CHAMA)
    eco = folder(tmp_path, "e-cidade", "libs/db_stdlib.php")
    zend = folder(tmp_path, "zend-app", "application/configs/application.ini")
    out = tmp_path / "out"
    ProjectStore(out).save(
        [
            Project("ecidade-t", "e-cidade", "ecidade", str(eco), db=str(serve)),
            Project("zend-t", "Zend App", "zend1", str(zend), db=str(chama)),
        ],
        "zend-t",
    )
    return {"out": out, "serve": serve, "chama": chama, "eco": eco, "zend": zend, "root": tmp_path}


def make(world: dict[str, Any], **kwargs: Any) -> TestClient:
    app = create_app(None, ui_dir=None, out_dir=world["out"], explicit_db=False, **kwargs)
    return TestClient(app, base_url=LOCAL)


@pytest.fixture
def client(world: dict[str, Any]) -> TestClient:
    return make(world)


# --------------------------------------------------------------------------- projetos


def test_projects_and_active_project_come_from_the_file(client: TestClient) -> None:
    body = data(post(client, PROJECTS))
    assert [(p["slug"], p["active"], p["hasGraph"]) for p in body["projects"]] == [
        ("ecidade-t", False, True),
        ("zend-t", True, True),
    ]
    assert body["activeProject"] == {"slug": "zend-t"}
    assert body["projects"][1]["profile"] == "zend1" and body["projects"][1]["sourceExists"]
    # o servidor serve o grafo do projeto ativo
    assert data(post(client, STATS))["stats"]["meta"]["project"] == "zend-t"


def test_implicit_project_for_a_plain_db_without_projects_file(
    shop_db: Path, tmp_path: Path
) -> None:
    """`php-map ui --db X` sem projeto cadastrado continua funcionando."""
    client = TestClient(create_app(shop_db, ui_dir=None, out_dir=tmp_path / "out"), base_url=LOCAL)
    body = data(post(client, PROJECTS))
    (only,) = body["projects"]
    assert only["profile"] == "ecidade" and only["active"] and only["hasGraph"]
    assert not (tmp_path / "out/projects.json").exists()
    assert data(post(client, "query { stats }"))["stats"]["nodes"]["File"] > 0


def test_suggest_profile_for_each_folder(client: TestClient, world: dict[str, Any]) -> None:
    eco = data(post(client, SUGGEST, source=str(world["eco"])))["suggestProfile"]
    zend = data(post(client, SUGGEST, source=str(world["zend"])))["suggestProfile"]
    assert (eco["exists"], eco["profile"]) == (True, "ecidade")
    assert (zend["exists"], zend["profile"]) == (True, "zend1")
    assert {"profile": "zend1", "marker": "application/configs/application.ini", "found": True} in (
        zend["markers"]
    )
    nothing = data(post(client, SUGGEST, source=str(world["root"] / "nada")))["suggestProfile"]
    assert nothing["exists"] is False and nothing["profile"] is None
    profiles = data(post(client, "query { projectProfiles { name marker } }"))["projectProfiles"]
    assert {"name": "zend1", "marker": "application/configs/application.ini"} in profiles


def test_add_update_and_remove_a_project(client: TestClient, world: dict[str, Any]) -> None:
    other = folder(world["root"], "outro", "application/configs/application.ini")
    added = data(
        post(
            client,
            ADD,
            input={
                "name": "Zend Teste",
                "profile": "zend1",
                "source": str(other),
                "schemaFromDb": True,
                "dsnEnv": "SEGUNDO_PROJETO_DSN",
            },
        )
    )["addProject"]
    assert (added["slug"], added["profile"], added["hasGraph"], added["active"]) == (
        "zend-teste",
        "zend1",
        False,
        False,
    )
    assert added["schemaFromDb"] is True and added["dsnEnv"] == "SEGUNDO_PROJETO_DSN"
    saved = json.loads((world["out"] / "projects.json").read_text(encoding="utf-8"))
    assert [p["slug"] for p in saved["projects"]] == ["ecidade-t", "zend-t", "zend-teste"]

    changed = data(post(client, UPDATE, slug="zend-teste", input={"name": "Outro nome"}))
    assert changed["updateProject"]["name"] == "Outro nome"
    cleared = data(post(client, UPDATE, slug="zend-teste", input={"clearDsnEnv": True}))
    assert cleared["updateProject"]["dsnEnv"] is None

    remaining = data(post(client, REMOVE, slug="zend-teste"))["removeProject"]
    assert [p["slug"] for p in remaining] == ["ecidade-t", "zend-t"]
    assert "troque de projeto" in error(post(client, REMOVE, slug="zend-t"))  # o ativo
    assert "não encontrado" in error(post(client, REMOVE, slug="nada"))


def test_invalid_folder_names_the_missing_marker(client: TestClient, world: dict[str, Any]) -> None:
    empty = world["root"] / "vazia"
    empty.mkdir()
    message = error(
        post(client, ADD, input={"name": "X", "profile": "zend1", "source": str(empty)})
    )
    assert "perfil 'zend1'" in message and "falta application/configs/application.ini" in message
    # a pasta do e-cidade não vale para o perfil zend1, e vice-versa
    message = error(
        post(client, ADD, input={"name": "X", "profile": "zend1", "source": str(world["eco"])})
    )
    assert "falta application/configs/application.ini" in message
    message = error(
        post(client, ADD, input={"name": "X", "profile": "ecidade", "source": str(world["zend"])})
    )
    assert "falta libs/db_stdlib.php" in message
    assert "inexistente" in error(
        post(client, ADD, input={"name": "X", "profile": "ecidade", "source": "/nao/existe"})
    )
    assert "perfil desconhecido" in error(
        post(client, ADD, input={"name": "X", "profile": "laravel", "source": str(world["eco"])})
    )
    # nada foi cadastrado
    assert len(data(post(client, PROJECTS))["projects"]) == 2


def test_set_active_project_switches_the_served_graph(client: TestClient) -> None:
    assert data(post(client, STATS))["stats"]["meta"]["project"] == "zend-t"
    active = data(post(client, ACTIVATE, slug="ecidade-t"))["setActiveProject"]
    assert active["active"] is True and active["slug"] == "ecidade-t"
    assert data(post(client, STATS))["stats"]["meta"]["project"] == "ecidade-t"
    assert data(post(client, PROJECTS))["activeProject"] == {"slug": "ecidade-t"}
    assert "não encontrado" in error(post(client, ACTIVATE, slug="nada"))
    # a escolha sobrevive à reinicialização do servidor
    again = make({"out": client.app.state.ecmap.out_dir})  # type: ignore[attr-defined]
    assert data(post(again, STATS))["stats"]["meta"]["project"] == "ecidade-t"


def test_active_project_without_graph_says_to_run_the_scan(
    client: TestClient, world: dict[str, Any]
) -> None:
    other = folder(world["root"], "novo", "application/configs/application.ini")
    data(post(client, ADD, input={"name": "Novo", "profile": "zend1", "source": str(other)}))
    active = data(post(client, ACTIVATE, slug="novo"))["setActiveProject"]
    assert active["hasGraph"] is False and active["lastScan"] is None
    assert "ainda não tem grafo: rode o scan" in error(post(client, STATS))
    assert "ainda não tem grafo" in error(post(client, COMMUNICATION))
    # o cadastro e o scan continuam acessíveis sem grafo
    assert data(post(client, PROJECTS))["activeProject"] == {"slug": "novo"}
    defaults = data(
        post(client, "query { scanDefaults { project profile marker source catalogAvailable } }")
    )["scanDefaults"]
    assert defaults == {
        "project": "novo",
        "profile": "zend1",
        "marker": "application/configs/application.ini",
        "source": str(other.resolve()),
        "catalogAvailable": False,
    }


# --------------------------------------------------------------------------- segurança


MUTATIONS: list[tuple[str, dict[str, Any]]] = [
    (ADD, {"input": {"name": "X", "profile": "ecidade", "source": "."}}),
    (UPDATE, {"slug": "zend-t", "input": {"name": "Y"}}),
    (REMOVE, {"slug": "ecidade-t"}),
    (ACTIVATE, {"slug": "ecidade-t"}),
]


@pytest.mark.parametrize(("query", "variables"), MUTATIONS)
def test_project_mutations_need_a_local_origin(
    client: TestClient, world: dict[str, Any], query: str, variables: dict[str, Any]
) -> None:
    before = (world["out"] / "projects.json").read_text(encoding="utf-8")
    sem_origem = client.post("/graphql", json={"query": query, "variables": variables})
    assert error(sem_origem.json()) == "operação recusada: origem não permitida"
    outro_site = post(client, query, {"origin": "https://exemplo.com"}, **variables)
    assert error(outro_site) == "operação recusada: origem não permitida"
    formulario = client.post(
        "/graphql",
        content=json.dumps({"query": query, "variables": variables}),
        headers={"origin": LOCAL, "content-type": "text/plain"},
    )
    if formulario.status_code == 200:
        assert error(formulario.json()) == "operação recusada: origem não permitida"
    assert (world["out"] / "projects.json").read_text(encoding="utf-8") == before


@pytest.mark.parametrize(("query", "variables"), MUTATIONS)
def test_read_only_turns_project_mutations_off(
    world: dict[str, Any], query: str, variables: dict[str, Any]
) -> None:
    client = make(world, read_only=True)
    assert error(post(client, query, **variables)) == "servidor em modo somente leitura"
    assert len(data(post(client, PROJECTS))["projects"]) == 2  # as consultas seguem


def test_the_dsn_never_appears_in_the_api_or_in_projects_json(
    client: TestClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falha de aceite: o valor do DSN em qualquer resposta ou no arquivo do cadastro."""
    monkeypatch.setenv("SEGUNDO_PROJETO_DSN", SECRET)
    responses: list[dict[str, Any]] = []

    other = folder(world["root"], "dsn", "application/configs/application.ini")
    responses.append(
        post(
            client,
            ADD,
            input={
                "name": "Com DSN",
                "profile": "zend1",
                "source": str(other),
                "schemaFromDb": True,
                "dsnEnv": "SEGUNDO_PROJETO_DSN",
            },
        )
    )
    # um DSN colado no campo é recusado, e a mensagem não repete o valor
    refused = post(
        client,
        ADD,
        input={"name": "Mau", "profile": "zend1", "source": str(other), "dsnEnv": SECRET},
    )
    assert "NOME da variável" in error(refused)
    responses.append(refused)
    responses.append(post(client, UPDATE, slug="com-dsn", input={"dsnEnv": SECRET}))
    responses += [
        post(client, ACTIVATE, slug="com-dsn"),
        post(client, PROJECTS),
        post(client, "query { activeProject { slug dsnEnv dsnAvailable schemaFromDb } }"),
        post(client, "query { workspace { source origin marker } }"),
        post(client, "query { scanDefaults { schemaDbAvailable catalogAvailable dsnEnv } }"),
        post(client, "query { scanJob { id } }"),
        post(client, REMOVE, slug="ecidade-t"),
    ]
    listed = data(responses[4])["projects"]
    com_dsn = next(p for p in listed if p["slug"] == "com-dsn")
    assert com_dsn["dsnEnv"] == "SEGUNDO_PROJETO_DSN" and com_dsn["dsnAvailable"] is True
    defaults = data(responses[7])["scanDefaults"]
    assert defaults["schemaDbAvailable"] is True and defaults["dsnEnv"] == "SEGUNDO_PROJETO_DSN"
    everything = json.dumps(responses) + (world["out"] / "projects.json").read_text(
        encoding="utf-8"
    )
    for leaked in (SECRET, "segredo-da-senha", "192.0.2.10", "exemplo"):
        assert leaked not in everything
    settings = world["out"] / "ui-settings.json"
    if settings.exists():
        assert "segredo-da-senha" not in settings.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- comunicação


def rows(page: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["endpointId"]: row for row in page["items"]}


def test_communication_from_the_zend_side(client: TestClient) -> None:
    page = data(post(client, COMMUNICATION, limit=200))["communication"]  # ativo: zend-t
    assert page["counters"] == {
        "serves": 2,  # Prestacao e a rota Zend
        "calls": 4,  # getA, procA, Missing e o `?` (o `externo` é do e-cidade)
        "withParams": 0,
        "orphans": 1,  # Missing: chamado e sem servidor
        "crossAvailable": True,
    }
    by_id = rows(page)
    get_a = by_id[GET_A]
    assert (get_a["direction"], get_a["status"], get_a["callers"]) == ("sai", "casado", 3)
    assert get_a["module"] == "Module:contribuinte"
    assert get_a["callerSample"][0]["module"] == "Module:contribuinte"
    missing = by_id[MISSING]
    assert (missing["direction"], missing["status"]) == ("sai", "chamado_sem_servidor")
    assert by_id[PRESTACAO]["direction"] == "entra" and by_id[PRESTACAO]["status"] == "casado"
    assert by_id[PRESTACAO]["reaches"]["tables"] == ["Table:public.zend_t"]
    zend = by_id["Route:fiscal/pedido/index"]
    assert (zend["protocol"], zend["category"], zend["direction"], zend["kind"]) == (
        "http",
        "http",
        "entra",
        "route",
    )
    assert zend["module"] == "Module:fiscal" and zend["implementedBy"][0]["line"] == 12


def test_communication_from_the_ecidade_side(client: TestClient) -> None:
    data(post(client, ACTIVATE, slug="ecidade-t"))
    page = data(post(client, COMMUNICATION, limit=200))["communication"]
    by_id = rows(page)
    get_a = by_id[GET_A]
    assert (get_a["direction"], get_a["protocol"], get_a["kind"]) == (
        "entra",
        "soap",
        "soap_consulta",
    )
    assert get_a["params"] == [{"name": "cnpj", "required": True, "type": None, "source": "ws_xml"}]
    assert get_a["reaches"]["tables"] == ["Table:public.t1", "Table:public.t3"]
    assert get_a["implementedBy"][0]["path"] == "ws/getA.xml"
    assert by_id[GET_B]["status"] == "servido_sem_cliente" and by_id[GET_B]["callers"] == 0
    assert by_id[RPC]["category"] == "rpc"
    route = by_id["Route:GET /v4/api/x/{id}"]
    assert route["params"] == [{"name": "id", "required": True, "type": None, "source": None}]
    assert route["module"] == "Module:tributario" and route["protocol"] == "http"
    # o que o e-cidade chama (Prestacao, `externo`) sai pela mesma lista
    assert by_id[PRESTACAO]["direction"] == "sai" and by_id[PRESTACAO]["status"] == "casado"
    externo = next(r for r in page["items"] if r["system"] == "externo")
    assert (externo["direction"], externo["category"], externo["callers"]) == ("sai", "externo", 3)
    counters = page["counters"]
    assert (counters["serves"], counters["calls"]) == (5, 2) and counters["crossAvailable"]
    assert counters["withParams"] == 2  # getA (cnpj) e a rota ({id})


def test_communication_filters_and_pagination(client: TestClient) -> None:
    data(post(client, ACTIVATE, slug="ecidade-t"))

    def ids(**variables: Any) -> list[str]:
        page = data(post(client, COMMUNICATION, limit=200, **variables))["communication"]
        return [r["endpointId"] for r in page["items"]]

    assert set(ids(protocol="soap", direction="entra")) == {GET_A, GET_B, PROC_A}
    assert ids(protocol="rpc") == [RPC]
    assert ids(protocol="http") == ["Route:GET /v4/api/x/{id}"]
    assert [i.split(":")[1] for i in ids(protocol="externo")] == ["externo"]
    assert ids(direction="sai", system="zend1app") == [PRESTACAO]
    assert ids(system="ecidade", protocol="soap", direction="entra", search="cnpj") == [GET_A]
    assert ids(search="GETA") == [GET_A]  # sem diferença de caixa
    assert ids(search="salvar") == [RPC]
    assert ids(search="api x id") == ["Route:GET /v4/api/x/{id}"]
    assert set(ids(module="Module:tributario")) == {
        "Route:GET /v4/api/x/{id}",
        PRESTACAO,
        "Endpoint:externo:http:?",  # quem chama (o helper) está no módulo
    }
    assert ids(search="nao-existe") == []

    first = data(post(client, COMMUNICATION, limit=2, offset=0))["communication"]
    second = data(post(client, COMMUNICATION, limit=2, offset=2))["communication"]
    assert first["total"] == second["total"] == 7 and len(first["items"]) == 2
    assert not {r["endpointId"] for r in first["items"]} & {
        r["endpointId"] for r in second["items"]
    }
    assert (first["limit"], second["offset"]) == (2, 2)
    huge = data(post(client, COMMUNICATION, limit=100000))["communication"]
    assert huge["limit"] == 200  # a API corta o limite: o navegador nunca recebe tudo
    assert "direction aceita entra ou sai" in error(post(client, COMMUNICATION, direction="x"))
    # as facetas vêm do conjunto inteiro, não do filtro
    filtered = data(post(client, COMMUNICATION, protocol="rpc"))["communication"]
    assert {f["value"] for f in filtered["protocols"]} == {"soap", "rpc", "http", "externo"}


def test_detail_of_a_called_endpoint_shows_the_other_side(client: TestClient) -> None:
    detail = data(post(client, DETAIL, id=GET_A))["endpointDetail"]  # ativo: zend-t
    assert detail["row"]["direction"] == "sai" and detail["row"]["status"] == "casado"
    assert [(c["path"], c["line"], c["module"]) for c in detail["callers"]] == [
        (
            "application/modules/contribuinte/models/B.php",
            20,
            "Module:contribuinte",
        ),
        ("application/modules/fiscal/models/A.php", 10, "Module:fiscal"),
    ]
    other = detail["counterpart"]
    assert other["available"] is True and other["status"] == "casado"
    assert other["servedBy"] == "ecidade-t"
    assert other["params"] == [{"name": "cnpj", "required": True, "type": None, "source": "ws_xml"}]
    assert other["reaches"]["tables"] == ["Table:public.t1", "Table:public.t3"]
    assert {s["path"] for s in other["servers"]} == {"ws/getA.xml", "ws/server.php"}


def test_detail_of_an_endpoint_nobody_serves(client: TestClient) -> None:
    other = data(post(client, DETAIL, id=MISSING))["endpointDetail"]["counterpart"]
    assert other["available"] is True and other["status"] == "chamado_sem_servidor"
    assert other["servedBy"] is None and "Ninguém serve" in other["message"]


def test_detail_of_a_served_endpoint_shows_who_calls_it_from_the_other_project(
    client: TestClient,
) -> None:
    data(post(client, ACTIVATE, slug="ecidade-t"))
    detail = data(post(client, DETAIL, id=GET_A))["endpointDetail"]
    assert detail["row"]["direction"] == "entra" and detail["callers"] == []
    other = detail["counterpart"]
    assert other["status"] == "casado" and other["servedBy"] == "ecidade-t"
    assert {(c["project"], c["module"]) for c in other["callers"]} == {
        ("zend-t", "Module:contribuinte"),
        ("zend-t", "Module:fiscal"),
    }
    reverse = data(post(client, DETAIL, id=PRESTACAO))["endpointDetail"]  # o e-cidade chama
    assert reverse["row"]["direction"] == "sai"
    assert reverse["counterpart"]["servedBy"] == "zend-t"
    assert reverse["counterpart"]["reaches"]["tables"] == ["Table:public.zend_t"]


def test_detail_of_a_route_and_of_an_unknown_id(client: TestClient) -> None:
    route = data(post(client, DETAIL, id="Route:fiscal/pedido/index"))["endpointDetail"]
    assert route["row"]["nodeLabel"] == "Route" and route["counterpart"] is None
    assert route["attrs"]["controller"] == "pedido"
    assert "não encontrado" in error(post(client, DETAIL, id="Endpoint:x:soap:nao"))


def test_without_another_graph_the_other_side_is_not_mapped(
    world: dict[str, Any], client: TestClient
) -> None:
    # o outro projeto sai da lista: só sobra o grafo do projeto zend1
    data(post(client, REMOVE, slug="ecidade-t"))
    page = data(post(client, COMMUNICATION, limit=200))["communication"]
    assert page["counters"]["crossAvailable"] is False and page["counters"]["orphans"] is None
    assert "nenhum outro projeto" in page["crossNote"]
    assert all(r["status"] is None for r in page["items"])
    detail = data(post(client, DETAIL, id=GET_A))["endpointDetail"]
    other = detail["counterpart"]
    assert other["available"] is False and other["system"] == "ecidade"
    assert "não está mapeado" in other["message"]
    assert [c["path"] for c in detail["callers"]]  # quem chama, neste grafo, continua


def test_two_projects_with_the_same_meta_project_have_no_cross(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """Duas épocas do mesmo sistema: o `cross` recusa, e a tela explica em vez de quebrar."""
    twin = save_graph(tmp_path, "twin", build_serve(), META_SERVE)
    store = ProjectStore(world["out"])
    projects, active = store.load()
    store.save(
        [
            *projects,
            Project("ecidade-t2", "e-cidade (antes)", "ecidade", str(world["eco"]), db=str(twin)),
        ],
        "ecidade-t",
    )
    client = make(world)
    page = data(post(client, COMMUNICATION, limit=5))["communication"]
    assert page["counters"]["crossAvailable"] is False
    assert "mesmo projeto" in page["crossNote"]


# --------------------------------------------------------------------------- scan por projeto


@pytest.fixture
def scan_world(tmp_path: Path, shop_db: Path) -> dict[str, Any]:
    out = tmp_path / "out"
    ast_dir = ast_cache.cache_dir(out, MINI)
    ast_dir.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "ast/loja-mini.jsonl", ast_dir / ast_cache.CACHE_NAME)
    ast_cache.write_meta(ast_dir, ast_cache.current_meta(MINI, Settings()))
    return {"out": out, "db": shop_db}


def test_project_scan_end_to_end(scan_world: dict[str, Any]) -> None:
    """O `scan` real de um projeto: grafo em `grafos/<slug>/`, `meta.project` = slug, projeto
    atualizado, e o servidor passa a servi-lo (o projeto é o ativo)."""
    out = scan_world["out"]
    client = TestClient(
        create_app(
            scan_world["db"],
            ui_dir=None,
            out_dir=out,
            scan_command=[sys.executable, "-m", "php_map"],
        ),
        base_url=LOCAL,
    )
    added = data(
        post(client, ADD, input={"name": "Mini UI", "profile": "ecidade", "source": str(MINI)})
    )["addProject"]
    assert added["slug"] == "mini-ui"
    data(post(client, ACTIVATE, slug="mini-ui"))
    assert "ainda não tem grafo" in error(post(client, STATS))

    start = """mutation($input: ScanInput!) { startScan(input: $input) { id state } }"""
    body = post(
        client,
        start,
        input={
            "project": "mini-ui",
            "schema": [str(DDL)],
            "schemaSource": "files",
            "catalogFromEnv": False,
        },
    )
    assert data(body)["startScan"]["state"] == "running"
    scanning = data(post(client, PROJECTS))["projects"]
    assert next(p for p in scanning if p["slug"] == "mini-ui")["scanning"] is True

    job_query = "query { scanJob { state error result } }"
    deadline = time.monotonic() + 120
    while (job := data(post(client, job_query))["scanJob"])["state"] == "running":
        assert time.monotonic() < deadline, job
        time.sleep(0.2)
    assert job["state"] == "succeeded", job
    db = Path(job["result"]["db"])
    assert db.parent == (out / "grafos/mini-ui").resolve() and db.is_file()

    stats = data(post(client, STATS))["stats"]
    assert stats["meta"]["project"] == "mini-ui" and stats["meta"]["profile"] == "ecidade"
    assert stats["served"] == {"from_interface": True}
    project = next(p for p in data(post(client, PROJECTS))["projects"] if p["slug"] == "mini-ui")
    assert project["hasGraph"] and project["lastScan"] and project["scanning"] is False
    saved = json.loads((out / "projects.json").read_text(encoding="utf-8"))
    assert {p["slug"]: p["db"] for p in saved["projects"]}["mini-ui"] == str(db)
    # a tela Comunicação lê o grafo novo (os endpoints RPC do recorte)
    page = data(post(client, COMMUNICATION, protocol="rpc", limit=50))["communication"]
    assert "Endpoint:ecidade:rpc:ped4_liberarpedidos.RPC.php#pesquisaPedidos" in rows(page)


def test_scan_of_a_project_needs_the_marker_of_its_profile(
    client: TestClient, world: dict[str, Any]
) -> None:
    start = "mutation($input: ScanInput!) { startScan(input: $input) { id state } }"
    wrong = world["root"] / "pasta-errada"
    wrong.mkdir()
    message = error(post(client, start, input={"project": "zend-t", "source": str(wrong)}))
    assert "perfil 'zend1'" in message and "application/configs/application.ini" in message
    assert "não encontrado" in error(post(client, start, input={"project": "nada"}))
    assert "informe a pasta" in error(post(client, start, input={}))
    assert data(post(client, "query { scanJob { id } }"))["scanJob"] is None


@pytest.mark.docker
def test_zend1_project_scan_through_the_api(tmp_path: Path, shop_db: Path) -> None:
    """O mini projeto Zend 1 (recorte `zend1-mini`, extrator real): perfil zend1, sem catálogo,
    grafo em `grafos/<slug>/` e a tela Comunicação lendo os endpoints que ele chama e serve."""
    from php_map.errors import IngestError
    from php_map.ingest import php_extractor

    try:
        php_extractor.check_environment(Settings())
    except IngestError as exc:
        pytest.skip(str(exc))
    zend1_mini = FIXTURES / "zend1-mini"
    out = tmp_path / "out"
    client = TestClient(
        create_app(
            shop_db,
            ui_dir=None,
            out_dir=out,
            scan_command=[sys.executable, "-m", "php_map"],
        ),
        base_url=LOCAL,
    )
    suggested = data(post(client, SUGGEST, source=str(zend1_mini)))["suggestProfile"]
    assert suggested["profile"] == "zend1"  # o perfil sugerido pela pasta
    added = data(
        post(client, ADD, input={"name": "Zend App", "profile": "zend1", "source": str(zend1_mini)})
    )["addProject"]
    data(post(client, ACTIVATE, slug=added["slug"]))
    start = "mutation($input: ScanInput!) { startScan(input: $input) { state } }"
    schema = str(FIXTURES / "schema/zend1_mini.sql")
    body = post(
        client,
        start,
        input={
            "project": "zend-app",
            "schema": [schema],
            "schemaSource": "files",
            "catalogFromEnv": True,  # o perfil zend1 não lê catálogo: a opção é ignorada
        },
    )
    assert data(body)["startScan"]["state"] == "running"
    deadline = time.monotonic() + 240
    while (job := data(post(client, "query { scanJob { state error result } }"))["scanJob"])[
        "state"
    ] == "running":
        assert time.monotonic() < deadline, job
        time.sleep(0.5)
    assert job["state"] == "succeeded", job
    stats = data(post(client, STATS))["stats"]
    assert (stats["meta"]["project"], stats["meta"]["profile"]) == ("zend-app", "zend1")
    assert Path(job["result"]["db"]).parent == (out / "grafos/zend-app").resolve()
    page = data(post(client, COMMUNICATION, limit=50))["communication"]
    by_id = rows(page)
    assert by_id["Endpoint:ecidade:soap:getDadosEmpresa"]["direction"] == "sai"
    assert by_id["Endpoint:zend1app:soap:PrestacaoContas"]["direction"] == "entra"
    assert any(r["nodeLabel"] == "Route" for r in page["items"])  # as rotas do Zend 1
