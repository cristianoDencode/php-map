"""Ferramenta `routine` do MCP (texto das funções da base) e `node.has_source`."""

from pathlib import Path

import pytest
from cross_graphs import META_SERVE, build_serve
from cross_graphs import save as _save

from php_map.analysis.aggregate import build_analytics, write_analytics, write_mentions
from php_map.analysis.routine_origin import Mention
from php_map.config import Settings
from php_map.errors import QueryError
from php_map.graph import routines
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Edge, EdgeSource, EdgeType, Node, NodeLabel
from php_map.mcp import tools
from php_map.mcp.catalog import ProjectCatalog
from php_map.search.index import build_documents, write_index

PROJECT = "ecidade-t"
CALC = "PgFunction:public.fc_calcula(int4,int4)"
LPAD_A = "PgFunction:public.lpad(float8,int4)"
LPAD_B = "PgFunction:public.lpad(varchar,int4,int4)"
BIG = "PgFunction:public.fc_enorme()"
IPTUBASE = "Table:cadastro.iptubase"
# duas sobrecargas, a 1ª só repassa para a 2ª (`CALLS_RPC` interna à família)
DELEGA_A = "PgFunction:public.fc_delega(int4)"
DELEGA_B = "PgFunction:public.fc_delega(int4,int4)"


def _ddl(name: str, args: str, body: str) -> str:
    return (
        f"CREATE OR REPLACE FUNCTION public.{name}({args})\n RETURNS integer\n"
        f" LANGUAGE plpgsql\nAS $function$\n{body}\n$function$\n"
    )


def _edge(src: str, dst: str, kind: EdgeType) -> Edge:
    return Edge(src, dst, kind, EdgeSource.SQL_AST, 1.0, {"path": src.partition(":")[2], "line": 1})


def _save_indexed(tmp_path: Path, builder: GraphBuilder, name: str) -> Path:
    db = _save(tmp_path, name, builder, {**META_SERVE, "project": PROJECT})
    docs, stats = build_documents(lambda: [], builder, Settings())
    write_index(db, docs, stats)
    return db


@pytest.fixture
def with_text(tmp_path: Path) -> ProjectCatalog:
    b = GraphBuilder()
    for node_id, name, args in (
        (CALC, "fc_calcula", ["int4", "int4"]),
        (LPAD_A, "lpad", ["float8", "int4"]),
        (LPAD_B, "lpad", ["varchar", "int4", "int4"]),
        (BIG, "fc_enorme", []),
        (DELEGA_A, "fc_delega", ["int4"]),
        (DELEGA_B, "fc_delega", ["int4", "int4"]),
    ):
        attrs = {"name": name, "schema": "public", "args": args, "returns": "int4"}
        b.add_node(Node(node_id, NodeLabel.PG_FUNCTION, attrs))
    b.add_edge(_edge(DELEGA_A, DELEGA_B, EdgeType.CALLS_RPC))
    b.add_node(Node(IPTUBASE, NodeLabel.TABLE, {"name": "iptubase", "schema": "cadastro"}))
    b.add_edge(_edge(CALC, IPTUBASE, EdgeType.QUERIES))
    b.add_edge(_edge(CALC, IPTUBASE, EdgeType.UPDATES))
    b.add_edge(_edge("File:a.php", CALC, EdgeType.CALLS_RPC))
    b.add_edge(_edge("File:b.php", CALC, EdgeType.CALLS_RPC))
    b.add_edge(_edge(BIG, CALC, EdgeType.CALLS_RPC))
    b.add_edge(_edge(IPTUBASE, CALC, EdgeType.TRIGGERS))
    b.add_node(
        Node(
            "BusinessAction:menu:1", NodeLabel.BUSINESS_ACTION, {"menu_path": "Cadastro > Calcula"}
        )
    )
    b.add_edge(
        Edge(
            "File:a.php",
            "BusinessAction:menu:1",
            EdgeType.IMPLEMENTS_ACTION,
            EdgeSource.ECIDADE_CATALOG,
            1.0,
            {"path": "a.php"},
        )
    )
    db = _save_indexed(tmp_path, b, "texto")

    big_body = "\n".join(f"  perform {i};" for i in range(600))
    make = routines.make_source
    analytics = build_analytics(b, Settings())
    write_analytics(db, analytics)
    write_mentions(db, [Mention(LPAD_A, "File:z.php", 7)])
    routines.write_routines(
        db,
        [
            make(CALC, "public", "fc_calcula", "plpgsql", _ddl("fc_calcula", "int, int", "x"), "T"),
            make(LPAD_A, "public", "lpad", "sql", _ddl("lpad", "double precision, int", "a"), "T"),
            make(LPAD_B, "public", "lpad", "sql", _ddl("lpad", "varchar, int, int", "b"), "T"),
            make(BIG, "public", "fc_enorme", "plpgsql", _ddl("fc_enorme", "", big_body), "T"),
            make(DELEGA_A, "public", "fc_delega", "plpgsql", _ddl("fc_delega", "int", "d1"), "T"),
            make(
                DELEGA_B, "public", "fc_delega", "plpgsql", _ddl("fc_delega", "int, int", "d2"), "T"
            ),
        ],
    )
    return ProjectCatalog.from_dbs([db])


@pytest.fixture
def without_text(tmp_path: Path) -> ProjectCatalog:
    return ProjectCatalog.from_dbs([_save_indexed(tmp_path, build_serve(), "sem")])


def test_routine_by_id_schema_name_and_name(with_text: ProjectCatalog) -> None:
    for name in (CALC, "public.fc_calcula", "fc_calcula", "FC_CALCULA"):
        out = tools.routine(with_text, PROJECT, name)
        assert out["id"] == CALC and out["language"] == "plpgsql" and out["read_at"] == "T"
        assert out["args"] == ["int4", "int4"] and out["returns"] == "int4"
        assert out["lines"][0].startswith("1: CREATE OR REPLACE FUNCTION public.fc_calcula")
        assert out["total_lines"] == 6 and out["truncated"] is False and len(out["sha1"]) == 40


def test_routine_users_and_tables(with_text: ProjectCatalog) -> None:
    out = tools.routine(with_text, PROJECT, "fc_calcula")
    assert out["used_by"] == {"php": 2, "functions": 1, "triggers": 1}
    tables = {(t["table"], t["type"]): t["confidence"] for t in out["tables"]["items"]}
    assert tables == {(IPTUBASE, "QUERIES"): 1.0, (IPTUBASE, "UPDATES"): 1.0}
    assert out["tables"]["truncated"] is False


def test_ambiguous_name_lists_candidates_without_text(with_text: ProjectCatalog) -> None:
    out = tools.routine(with_text, PROJECT, "lpad")
    assert out["ambiguous"] is True and "lines" not in out
    assert [c["id"] for c in out["candidates"]] == [LPAD_A, LPAD_B]
    assert out["candidates"][1]["args"] == ["varchar", "int4", "int4"]
    # sem aresta interna entre as sobrecargas: nenhum candidato ganha `delegates_to`
    assert all("delegates_to" not in c for c in out["candidates"])
    # com o id de uma delas, o texto certo de cada sobrecarga
    first = "\n".join(tools.routine(with_text, PROJECT, LPAD_A)["lines"])
    second = "\n".join(tools.routine(with_text, PROJECT, LPAD_B)["lines"])
    assert "double precision" in first and "varchar" not in first
    assert "varchar" in second and "double precision" not in second


def test_delegated_name_resolves_with_the_evidence_in_resolved(
    with_text: ProjectCatalog,
) -> None:
    """`fc_delega` (1 arg) só repassa para a de 2 (`CALLS_RPC` interna à família)."""
    out = tools.routine(with_text, PROJECT, "fc_delega")
    assert "ambiguous" not in out
    assert out["id"] == DELEGA_B
    assert out["resolved"] == {"by": "repasse", "chain": [DELEGA_A, DELEGA_B], "others": [DELEGA_A]}
    # a assinatura exata que só repassa continua indo nela, sem `resolved`
    direct = tools.routine(with_text, PROJECT, DELEGA_A)
    assert direct["id"] == DELEGA_A and "resolved" not in direct


def test_routine_card_delegated_name_resolves_with_the_warning(
    with_text: ProjectCatalog,
) -> None:
    out = tools.routine_card(with_text, PROJECT, "fc_delega")
    assert out["id"] == DELEGA_B
    assert out["resolved"] == {"by": "repasse", "chain": [DELEGA_A, DELEGA_B], "others": [DELEGA_A]}
    assert "só repassa para esta" in out["markdown"]


def test_missing_routine_suggests_by_prefix(with_text: ProjectCatalog) -> None:
    with pytest.raises(QueryError, match=r"inexistente.*Parecidas: public\.fc_calcula"):
        tools.routine(with_text, PROJECT, "fc_calc")
    with pytest.raises(QueryError, match="inexistente"):
        tools.routine(with_text, PROJECT, "nada_parecido")


def test_routine_without_rule_tables_has_null_rules_and_profile(with_text: ProjectCatalog) -> None:
    out = tools.routine(with_text, PROJECT, CALC)
    assert out["rules"] is None and out["profile"] is None


@pytest.fixture
def with_rules(with_text: ProjectCatalog) -> ProjectCatalog:
    from php_map.ingest.plpgsql_rules import scan_routine

    db = with_text.projects[0].db
    ddl = (
        "CREATE OR REPLACE FUNCTION public.fc_calcula(a integer, b integer) RETURNS integer\n"
        " LANGUAGE plpgsql\nAS $function$\nBEGIN\n"
        "  IF a < 0 THEN\n    RAISE EXCEPTION 'saldo invalido';\n  END IF;\n"
        "  RAISE NOTICE 'traco';\nEND;\n$function$\n"
    )
    rules, profile = scan_routine(ddl)
    routines.write_rules(db, [(CALC, rules, profile)])
    return with_text


def test_routine_rules_and_profile_fields(with_rules: ProjectCatalog) -> None:
    out = tools.routine(with_rules, PROJECT, CALC)
    assert out["profile"] is not None and out["profile"]["language"] == "plpgsql"
    assert out["rules"] is not None
    kinds = {r["kind"] for r in out["rules"]["items"]}
    assert kinds == {"validation", "decision"}  # diagnostic fica de fora do padrão
    everything = tools.routine(with_rules, PROJECT, CALC, rules_kind="diagnostic")
    assert everything["rules"] is not None
    assert [r["kind"] for r in everything["rules"]["items"]] == ["diagnostic"]


def test_routine_rules_tool_searches_across_functions(with_rules: ProjectCatalog) -> None:
    found = tools.routine_rules(with_rules, PROJECT, query="saldo")
    assert found["total"] == 1
    assert found["items"][0]["routine_id"] == CALC
    assert found["items"][0]["message"] == "saldo invalido"
    none_found = tools.routine_rules(with_rules, PROJECT, query="nada disso existe")
    assert none_found["total"] == 0


@pytest.fixture
def with_fields(with_text: ProjectCatalog) -> ProjectCatalog:
    from php_map.ingest.sql_fields import analyze_statement
    from php_map.ingest.table_directory import TableDirectory

    db = with_text.projects[0].db
    directory = TableDirectory()
    directory.add("cadastro", "iptubase", columns=("id_imovel", "valor"))
    facts = analyze_statement(
        "update iptubase set valor = valor + 1 where id_imovel = 1", directory=directory
    )
    routines.write_fields(db, [routines.QueryRow(CALC, 0, 5, 5, facts)])
    return with_text


def test_routine_has_queries_with_fields(with_fields: ProjectCatalog) -> None:
    out = tools.routine(with_fields, PROJECT, CALC)
    assert out["queries"] is not None
    [query] = out["queries"]["items"]
    assert query["kind"] == "update"
    roles = {(f["column"], f["role"]) for f in query["fields"]}
    assert roles == {("valor", "write"), ("valor", "read"), ("id_imovel", "filter")}


def test_column_usage_finds_who_writes_and_reads(with_fields: ProjectCatalog) -> None:
    writers = tools.column_usage(with_fields, PROJECT, IPTUBASE, "valor", role="write")
    assert writers["total"] == 1 and writers["items"][0]["routine_id"] == CALC
    readers = tools.column_usage(with_fields, PROJECT, "iptubase", "valor", role="read")
    assert readers["total"] == 1
    nothing = tools.column_usage(with_fields, PROJECT, IPTUBASE, "valor", role="sort")
    assert nothing["total"] == 0


def test_column_usage_unknown_table_raises(with_fields: ProjectCatalog) -> None:
    with pytest.raises(QueryError, match="tabela inexistente"):
        tools.column_usage(with_fields, PROJECT, "nao_existe", "x")


def test_column_usage_without_tables_raises(with_text: ProjectCatalog) -> None:
    with pytest.raises(QueryError, match="rode o scan novo"):
        tools.column_usage(with_text, PROJECT, IPTUBASE, "valor")


def test_routine_card_returns_markdown_by_default(with_rules: ProjectCatalog) -> None:
    out = tools.routine_card(with_rules, PROJECT, CALC)
    assert out["id"] == CALC
    assert "saldo invalido" in out["markdown"]
    assert "# public.fc_calcula" in out["markdown"]


def test_routine_card_json_format(with_rules: ProjectCatalog) -> None:
    out = tools.routine_card(with_rules, PROJECT, "fc_calcula", format="json")
    assert out["card"]["id"] == CALC
    assert out["card"]["kind"] == "function"


def test_routine_card_ambiguous_name_lists_candidates(with_rules: ProjectCatalog) -> None:
    out = tools.routine_card(with_rules, PROJECT, "lpad")
    assert out["ambiguous"] is True
    assert len(out["candidates"]) == 2


def test_routine_card_bad_format_raises(with_rules: ProjectCatalog) -> None:
    with pytest.raises(QueryError, match="format"):
        tools.routine_card(with_rules, PROJECT, CALC, format="html")


@pytest.fixture
def with_screen(tmp_path: Path) -> ProjectCatalog:
    entry = "File:emp4_x.php"
    b = GraphBuilder()
    b.add_node(Node(entry, NodeLabel.FILE, {"path": "emp4_x.php"}))
    b.add_node(Node(CALC, NodeLabel.PG_FUNCTION, {"name": "fc_calcula", "schema": "public"}))
    b.add_edge(
        Edge(
            entry, CALC, EdgeType.CALLS_RPC, EdgeSource.SQL_AST, 1.0,
            {"path": "emp4_x.php", "line": 10},
        )
    )  # fmt: skip
    db = _save_indexed(tmp_path, b, "tela")
    return ProjectCatalog.from_dbs([db])


def test_screen_card_returns_markdown(with_screen: ProjectCatalog) -> None:
    out = tools.screen_card(with_screen, PROJECT, "emp4_x.php")
    assert out["entry"] == "File:emp4_x.php"
    assert "Tela: emp4_x.php" in out["markdown"]


def test_screen_card_json_format(with_screen: ProjectCatalog) -> None:
    out = tools.screen_card(with_screen, PROJECT, "emp4_x.php", format="json")
    assert out["card"]["entry"] == "File:emp4_x.php"


def test_routine_rules_tool_without_tables_raises(with_text: ProjectCatalog) -> None:
    with pytest.raises(QueryError, match="rode o scan novo"):
        tools.routine_rules(with_text, PROJECT, query="x")


def test_routine_pagination(with_text: ProjectCatalog) -> None:
    first = tools.routine(with_text, PROJECT, BIG)
    assert first["total_lines"] == 605 and first["start_line"] == 1 and first["end_line"] == 200
    assert first["truncated"] is True and len(first["lines"]) == 200
    middle = tools.routine(with_text, PROJECT, BIG, offset=201, limit=200)
    assert middle["lines"][0] == "201: " + "  perform 196;"  # linhas 1 a 4 são o cabeçalho
    last = tools.routine(with_text, PROJECT, BIG, offset=601, limit=500)
    assert last["end_line"] == 605 and last["lines"][-1] == "605: $function$"
    assert last["truncated"] is True  # a janela não começa na linha 1
    huge = tools.routine(with_text, PROJECT, BIG, limit=99999)  # o limite é contido
    assert len(huge["lines"]) == tools.MAX_ROUTINE_LINES


def test_node_reports_whether_the_function_has_text(with_text: ProjectCatalog) -> None:
    out = tools.node(with_text, CALC, PROJECT)
    assert out["has_source"] is True and out["read_at"] == "T"
    assert "has_source" not in tools.node(with_text, IPTUBASE, PROJECT)  # só PgFunction


def test_routine_on_a_graph_without_the_table(without_text: ProjectCatalog) -> None:
    with pytest.raises(QueryError, match="rode o scan novo"):
        tools.routine(without_text, PROJECT, "fc_calcula")


# --------------------------------------------------------------------------- origem


def test_routine_brings_its_origin(with_text: ProjectCatalog) -> None:
    origin = tools.routine(with_text, PROJECT, CALC)["origin"]
    assert origin["status"] == "com_origem" and origin["generated"] is False
    assert origin["callers"] == {"php": 2, "functions": 1, "triggers": 1}
    menu = [e for e in origin["entries"] if e["kind"] == "menu"]
    assert [(e["target"], e["label"], e["distance"]) for e in menu] == [
        ("BusinessAction:menu:1", "Cadastro > Calcula", 1)
    ]
    assert menu[0]["via"] == ["File:a.php", CALC] and menu[0]["confidence"] == 1.0
    assert origin["mentions"] == []


def test_origin_of_a_function_without_callers_has_status_reason_and_hypothesis(
    with_text: ProjectCatalog,
) -> None:
    origin = tools.routine(with_text, PROJECT, LPAD_A)["origin"]
    assert origin["status"] == "sem_chamador" and "SQL dinâmico" in origin["reason"]
    assert origin["entries"] == [] and origin["module"] is None
    assert origin["mentions"] == [
        {
            "file": "File:z.php",
            "line": 7,
            "source": "LITERAL_MENTION",
            "confidence": 0.5,
            "hypothesis": True,
        }
    ]


def test_routine_origins_counts_filters_and_paginates(with_text: ProjectCatalog) -> None:
    report = tools.routine_origins(with_text, PROJECT)
    assert report["by_status"] == {
        "com_origem": 1,
        "so_arquivo": 0,
        "gerada": 0,
        # as duas lpad, a fc_enorme (chama mas ninguém a chama) e fc_delega(1 arg), que só
        # repassa e também não tem chamador
        "sem_chamador": 4,
        "cadeia_sem_origem": 1,  # fc_delega(2 args): só quem a chama é a que não tem origem
    }
    assert report["total"] == 6 and sum(report["by_status"].values()) == 6
    orphans = tools.routine_origins(with_text, PROJECT, status="sem_chamador", limit=3)
    assert orphans["total"] == 4 and orphans["truncated"] is True
    assert [i["id"] for i in orphans["items"]] == [DELEGA_A, BIG, LPAD_A]  # por id
    rest = tools.routine_origins(with_text, PROJECT, status="sem_chamador", limit=3, offset=3)
    assert [i["id"] for i in rest["items"]] == [LPAD_B] and rest["truncated"] is False
    calc = tools.routine_origins(with_text, PROJECT, status="com_origem")["items"][0]
    assert calc["id"] == CALC and calc["nearest"][0]["label"] == "Cadastro > Calcula"
    assert tools.routine_origins(with_text, PROJECT, generated=True)["total"] == 0
    assert tools.routine_origins(with_text, PROJECT, module="Module:nenhum")["total"] == 0
    assert tools.routine_origins(with_text, PROJECT, limit=99999)["items"]  # o limite é contido


def test_routine_origins_rejects_an_unknown_status(with_text: ProjectCatalog) -> None:
    with pytest.raises(QueryError, match="status aceita"):
        tools.routine_origins(with_text, PROJECT, status="qualquer")


def test_origin_on_a_graph_without_the_tables(without_text: ProjectCatalog) -> None:
    with pytest.raises(QueryError, match="rode o scan novo"):
        tools.routine_origins(without_text, PROJECT)
