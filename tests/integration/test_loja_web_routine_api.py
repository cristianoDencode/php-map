"""API do texto das funções da base: `routineSource` e `hasRoutineTexts`."""

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from php_map.analysis.aggregate import SCHEMA
from php_map.graph import routines
from php_map.web.app import create_app

LOCAL = "http://127.0.0.1:8765"
FN = "PgFunction:public.fc_calcula(int4)"

SOURCE = """query($id: ID!, $offset: Int, $limit: Int) {
  routineSource(id: $id, offset: $offset, limit: $limit) {
    id schema name language generated readAt sha1 totalLines startLine endLine lines truncated
  }
}"""
HAS = "query { hasRoutineTexts }"
ORIGIN_FIELDS = """status reason module moduleConfidence generated callersPhp callersRoutines
  triggers
  entries { kind target label distance via confidence }
  mentions { file line confidence hypothesis }"""
ORIGIN = "query($id: ID!) { hasRoutineOrigins routineOrigin(id: $id) { " + ORIGIN_FIELDS + " } }"
SOURCE_ORIGIN = (
    "query($id: ID!) { routineSource(id: $id, offset: 1, limit: 1) { name origin { "
    + ORIGIN_FIELDS
    + " } } }"
)


def _client(db: Path, tmp_path: Path) -> TestClient:
    app = create_app(db, ui_dir=None, out_dir=tmp_path / "out")
    return TestClient(app, base_url=LOCAL)


def _post(client: TestClient, query: str, **variables: Any) -> dict[str, Any]:
    response = client.post(
        "/graphql", json={"query": query, "variables": variables}, headers={"origin": LOCAL}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.fixture
def with_text(shop_db: Path, tmp_path: Path) -> TestClient:
    db = tmp_path / "com-texto.db"
    shutil.copy(shop_db, db)
    body = "\n".join(f"  perform {i};" for i in range(300))
    ddl = (
        "CREATE OR REPLACE FUNCTION public.fc_calcula(integer)\n RETURNS integer\n"
        f" LANGUAGE plpgsql\nAS $function$\n{body}\n$function$\n"
    )
    routines.write_routines(
        db,
        [routines.make_source(FN, "public", "fc_calcula", "plpgsql", ddl, "2026-11-03T10:00:00")],
    )
    return _client(db, tmp_path)


@pytest.fixture
def with_origin(with_text: TestClient, tmp_path: Path) -> TestClient:
    db = tmp_path / "com-texto.db"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO analytics_routines VALUES (?, 'com_origem', 'chega a 1 menu', "
            "'Module:cadastro', 0.75, 0, 2, 1, 0, 1)",
            (FN,),
        )
        conn.execute(
            "INSERT INTO analytics_routines VALUES (?, 'sem_chamador', 'ninguém a chama', "
            "NULL, NULL, 0, 0, 0, 0, 0)",
            ("PgFunction:public.orfa()",),
        )
        conn.execute(
            "INSERT INTO analytics_routine_entries VALUES (?, 'menu', 'BusinessAction:menu:1', "
            "'Cadastro > Calcula', 2, ?, 0.9)",
            (FN, '["File:a.php", "PgFunction:public.fc_calcula(int4)"]'),
        )
        conn.execute(
            "INSERT INTO analytics_routine_mentions VALUES (?, 'File:z.php', 7, "
            "'LITERAL_MENTION', 0.5)",
            ("PgFunction:public.orfa()",),
        )
    return with_text


def test_origin_comes_with_the_function_and_by_itself(with_origin: TestClient) -> None:
    alone = _post(with_origin, ORIGIN, id=FN)["data"]
    assert alone["hasRoutineOrigins"] is True
    origin = alone["routineOrigin"]
    assert (origin["status"], origin["module"], origin["moduleConfidence"]) == (
        "com_origem",
        "Module:cadastro",
        0.75,
    )
    assert (origin["callersPhp"], origin["callersRoutines"], origin["triggers"]) == (2, 1, 0)
    assert origin["entries"] == [
        {
            "kind": "menu",
            "target": "BusinessAction:menu:1",
            "label": "Cadastro > Calcula",
            "distance": 2,
            "via": ["File:a.php", FN],
            "confidence": 0.9,
        }
    ]
    assert origin["mentions"] == []
    inside = _post(with_origin, SOURCE_ORIGIN, id=FN)["data"]["routineSource"]
    assert inside["origin"] == origin


def test_orphan_origin_carries_the_hypothesis_mention(with_origin: TestClient) -> None:
    origin = _post(with_origin, ORIGIN, id="PgFunction:public.orfa()")["data"]["routineOrigin"]
    assert origin["status"] == "sem_chamador" and origin["module"] is None
    assert origin["mentions"] == [
        {"file": "File:z.php", "line": 7, "confidence": 0.5, "hypothesis": True}
    ]


def test_origin_is_null_without_the_tables(
    with_text: TestClient, shop_db: Path, tmp_path: Path
) -> None:
    old = tmp_path / "antigo.db"
    shutil.copy(shop_db, old)
    with closing(sqlite3.connect(old)) as conn, conn:
        for table in (
            "analytics_routines",
            "analytics_routine_entries",
            "analytics_routine_mentions",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    body = _post(_client(old, tmp_path), ORIGIN, id=FN)["data"]
    assert body == {"hasRoutineOrigins": False, "routineOrigin": None}


def test_routine_source_pages_the_text(with_text: TestClient) -> None:
    first = _post(with_text, SOURCE, id=FN)["data"]["routineSource"]
    assert first["id"] == FN and first["name"] == "fc_calcula" and first["language"] == "plpgsql"
    assert first["generated"] is False and first["readAt"] == "2026-11-03T10:00:00"
    assert first["totalLines"] == 305 and first["startLine"] == 1 and first["endLine"] == 200
    assert first["lines"][0].startswith("CREATE OR REPLACE FUNCTION")  # sem número de linha
    assert first["truncated"] is True and len(first["sha1"]) == 40

    rest = _post(with_text, SOURCE, id=FN, offset=201, limit=200)["data"]["routineSource"]
    assert rest["startLine"] == 201 and rest["endLine"] == 305 and len(rest["lines"]) == 105
    assert rest["lines"][-1] == "$function$" and rest["truncated"] is True

    capped = _post(with_text, SOURCE, id=FN, limit=99999)["data"]["routineSource"]
    assert len(capped["lines"]) == 305 and capped["truncated"] is False  # cabe em 500


def test_function_without_text_is_null_and_the_flag_tells_why(
    with_text: TestClient, shop_db: Path, tmp_path: Path
) -> None:
    assert _post(with_text, SOURCE, id="PgFunction:public.outra()")["data"] == {
        "routineSource": None
    }
    assert _post(with_text, HAS)["data"] == {"hasRoutineTexts": True}

    old = _client(shop_db, tmp_path)  # grafo antigo: sem a tabela
    assert _post(old, HAS)["data"] == {"hasRoutineTexts": False}
    assert _post(old, SOURCE, id=FN)["data"] == {"routineSource": None}
    with closing(sqlite3.connect(shop_db)) as conn:
        assert routines.has_routines(conn) is False
