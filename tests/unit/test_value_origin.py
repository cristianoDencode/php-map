""" "de onde vem este valor?" — pergunta guiada, sem IA (`analysis/value_origin.py`).

A fixture reproduz, em miniatura, um caso real de cálculo de IPTU (`fc_calculoiptu_mar_2011` →
`fc_iptu_calculavvt_mar_2011`): um `select ... into` com duas variáveis (cada uma de uma tabela
diferente), uma atribuição condicional que troca a variável usada, e a gravação final numa tabela
temporária. `tabela_a` tem DAO sem chamador conhecido (testa a hipótese pelo nome); `tabela_b` tem
DAO chamado por um arquivo com item de menu (testa o cadastro encontrado).
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from php_map.analysis import value_origin as vo
from php_map.graph import routines as routine_store
from php_map.graph import store as graph_store
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Edge, EdgeSource, EdgeType, Node, NodeLabel
from php_map.ingest.plpgsql_rules import scan_routine
from php_map.ingest.sql_fields import analyze_statement
from php_map.ingest.table_directory import TableDirectory

ROOT = "PgFunction:public.fc_root()"
HELPER = "PgFunction:public.fc_helper(int4)"
TABELA_A = "Table:cadastro.tabela_a"
TABELA_B = "Table:cadastro.tabela_b"
TMP_CALC = "Table:public.tmp_calc"
READ_AT = "2026-11-05T10:00:00-03:00"

HELPER_DDL = (
    "CREATE OR REPLACE FUNCTION public.fc_helper(iid integer) RETURNS numeric\n"
    " LANGUAGE plpgsql\nAS $f$\n"
    "DECLARE\n"
    "  nValor numeric;\n"
    "  nFator numeric;\n"
    "BEGIN\n"
    "  select valor, extra\n"
    "    into nValor, nFator\n"
    "    from tabela_a a\n"
    "    inner join tabela_b b on b.id = a.id\n"
    "   where a.id = iid;\n"
    "\n"
    "  if nFator < nValor then\n"
    "    nValor = nFator;\n"
    "  end if;\n"
    "\n"
    "  update tmp_calc set resultado = nValor;\n"
    "  return nValor;\n"
    "END;\n"
    "$f$;\n"
)
ROOT_DDL = (
    "CREATE OR REPLACE FUNCTION public.fc_root() RETURNS void\n"
    " LANGUAGE plpgsql\nAS $f$\nBEGIN\n  perform fc_helper(1);\nEND;\n$f$;\n"
)


def _db(tmp_path: Path, name: str = "g.db") -> Path:
    builder = GraphBuilder()
    builder.add_node(
        Node(
            TABELA_A,
            NodeLabel.TABLE,
            {
                "name": "tabela_a",
                "schema": "cadastro",
                "columns": ["id", "valor"],
                "catalog": {"label": "Tabela A"},
                "columns_catalog": {"valor": {"label": "Valor m2 do item"}},
            },
        )
    )
    builder.add_node(
        Node(
            TABELA_B,
            NodeLabel.TABLE,
            {
                "name": "tabela_b",
                "schema": "cadastro",
                "columns": ["id", "extra"],
                "catalog": {"label": "Tabela B"},
                "columns_catalog": {"extra": {"label": "Fator extra m2"}},
            },
        )
    )
    builder.add_node(
        Node(
            TMP_CALC,
            NodeLabel.TABLE,
            {"name": "tmp_calc", "schema": "public", "columns": ["resultado"]},
        )
    )
    builder.add_node(
        Node(ROOT, NodeLabel.PG_FUNCTION, {"name": "fc_root", "schema": "public", "args": []})
    )
    builder.add_node(
        Node(
            HELPER,
            NodeLabel.PG_FUNCTION,
            {"name": "fc_helper", "schema": "public", "args": ["int4"], "returns": "numeric"},
        )
    )
    builder.add_edge(
        Edge(
            ROOT, HELPER, EdgeType.CALLS_RPC, EdgeSource.SQL_AST, 1.0,
            {"path": "x", "line": 2},
        )
    )  # fmt: skip
    # tabela_a: DAO sem chamador conhecido (hipótese pelo nome)
    builder.add_node(Node("Method:cl_tabela_a::incluir", NodeLabel.METHOD, {"name": "incluir"}))
    builder.add_edge(
        Edge(
            "Method:cl_tabela_a::incluir", TABELA_A, EdgeType.INSERTS, EdgeSource.SQL_AST, 1.0,
            {"path": "classes/db_tabela_a_classe.php", "line": 10},
        )
    )  # fmt: skip
    builder.add_node(Node("File:func_tabela_a.php", NodeLabel.FILE, {"name": "func_tabela_a.php"}))
    # tabela_b: DAO chamado por um arquivo com item de menu (cadastro encontrado)
    builder.add_node(Node("Method:cl_tabela_b::incluir", NodeLabel.METHOD, {"name": "incluir"}))
    builder.add_edge(
        Edge(
            "Method:cl_tabela_b::incluir", TABELA_B, EdgeType.INSERTS, EdgeSource.SQL_AST, 1.0,
            {"path": "classes/db_tabela_b_classe.php", "line": 10},
        )
    )  # fmt: skip
    builder.add_node(
        Node("File:cad1_tabelab001.php", NodeLabel.FILE, {"name": "cad1_tabelab001.php"})
    )
    builder.add_edge(
        Edge(
            "File:cad1_tabelab001.php", "Method:cl_tabela_b::incluir", EdgeType.CALLS,
            EdgeSource.PHP_AST, 1.0, {"path": "cad1_tabelab001.php", "line": 5},
        )
    )  # fmt: skip
    builder.add_node(
        Node(
            "BusinessAction:menu:1",
            NodeLabel.BUSINESS_ACTION,
            {"menu_path": "Cadastro > Procedimentos > Tabela B > Inclusão"},
        )
    )
    builder.add_edge(
        Edge(
            "File:cad1_tabelab001.php", "BusinessAction:menu:1", EdgeType.IMPLEMENTS_ACTION,
            EdgeSource.ECIDADE_CATALOG, 1.0, {"path": "cad1_tabelab001.php", "line": 1},
        )
    )  # fmt: skip
    # uma tela que chama fc_helper diretamente (alcance por menu/fonte)
    builder.add_node(
        Node("File:cad1_helper001.php", NodeLabel.FILE, {"name": "cad1_helper001.php"})
    )
    builder.add_edge(
        Edge(
            "File:cad1_helper001.php", HELPER, EdgeType.CALLS_RPC, EdgeSource.SQL_AST, 1.0,
            {"path": "cad1_helper001.php", "line": 7},
        )
    )  # fmt: skip
    builder.add_node(
        Node("BusinessAction:menu:2", NodeLabel.BUSINESS_ACTION, {"menu_path": "Cadastro > Helper"})
    )
    builder.add_edge(
        Edge(
            "File:cad1_helper001.php", "BusinessAction:menu:2", EdgeType.IMPLEMENTS_ACTION,
            EdgeSource.ECIDADE_CATALOG, 1.0, {"path": "cad1_helper001.php", "line": 1},
        )
    )  # fmt: skip
    # item de menu sem nenhum arquivo que o implemente (erro de alcance por menu)
    builder.add_node(
        Node("BusinessAction:menu:3", NodeLabel.BUSINESS_ACTION, {"menu_path": "Órfão"})
    )
    builder.add_node(Node("Module:tabelab", NodeLabel.MODULE, {"name": "Tabela B"}))
    db = tmp_path / name
    graph_store.save(builder, db, {"source": "x", "scanned_at": READ_AT})

    rows = [
        routine_store.make_source(ROOT, "public", "fc_root", "plpgsql", ROOT_DDL, READ_AT),
        routine_store.make_source(HELPER, "public", "fc_helper", "plpgsql", HELPER_DDL, READ_AT),
    ]
    routine_store.write_routines(db, rows)
    rule_results = []
    for node_id, ddl in ((ROOT, ROOT_DDL), (HELPER, HELPER_DDL)):
        rules, profile = scan_routine(ddl)
        rule_results.append((node_id, rules, profile))
    routine_store.write_rules(db, rule_results)

    directory = TableDirectory()
    directory.add("cadastro", "tabela_a", columns=("id", "valor"))
    directory.add("cadastro", "tabela_b", columns=("id", "extra"))
    directory.add("public", "tmp_calc", columns=("resultado",))
    query_rows = []
    statements = (
        (
            8,
            12,
            "select valor, extra into nValor, nFator from tabela_a a"
            " inner join tabela_b b on b.id = a.id where a.id = iid",
        ),
        (18, 18, "update tmp_calc set resultado = nValor"),
    )
    for seq, (line, end_line, sql) in enumerate(statements):
        facts = analyze_statement(sql, directory=directory, variables=("iid", "nvalor", "nfator"))
        query_rows.append(routine_store.QueryRow(HELPER, seq, line, end_line, facts))
    routine_store.write_fields(db, query_rows)
    # classificação de módulo (analytics_routines), só para o alcance
    # por módulo — sem rodar o pipeline de agregados inteiro.
    with sqlite3.connect(db) as module_conn:
        module_conn.execute(
            "CREATE TABLE analytics_routines (routine_id TEXT PRIMARY KEY, status TEXT,"
            " reason TEXT, module TEXT, module_confidence REAL, generated INTEGER,"
            " callers_php INTEGER, callers_routines INTEGER, triggers INTEGER, entries INTEGER)"
        )
        # a origem sempre nasce junto; function_card espera as duas
        module_conn.execute(
            "CREATE TABLE analytics_routine_entries (routine_id TEXT, kind TEXT, target TEXT,"
            " label TEXT, distance INTEGER, via TEXT, confidence REAL)"
        )
        module_conn.execute(
            "CREATE TABLE analytics_routine_mentions (routine_id TEXT, file_id TEXT, line INTEGER,"
            " source TEXT, confidence REAL)"
        )
        module_conn.execute(
            "INSERT INTO analytics_routines"
            " VALUES (?, 'com_origem', '', 'Module:tabelab', 1.0, 0, 1, 0, 0, 1)",
            (HELPER,),
        )
        module_conn.commit()
    return db


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return _db(tmp_path)


@pytest.fixture
def conn(db: Path) -> Iterator[sqlite3.Connection]:
    with sqlite3.connect(db) as connection:
        yield connection


# --------------------------------------------------------------------------- interpretação


def test_interpret_finds_the_column_candidate_within_reach(conn: sqlite3.Connection) -> None:
    interp = vo.interpret(conn, "qual o valor m2 do item", "fc_root")
    assert interp.function == ROOT
    assert HELPER in interp.reach and len(interp.reach) == 2
    top = interp.candidates[0]
    assert top.table == TABELA_A and top.column == "valor"


def test_interpret_without_function_warns_and_suggests(conn: sqlite3.Connection) -> None:
    interp = vo.interpret(conn, "valor m2 do item")
    assert interp.function is None
    assert any("escolha a função" in w for w in interp.warnings)


def test_resolve_ambiguous_or_missing_function_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(vo.ValueOriginError, match="função não encontrada"):
        vo.interpret(conn, "x", "fc_fantasma")


# ------------------------------------------------------------------------- repasse

REP_A = "PgFunction:public.fc_rep(int4)"
REP_B = "PgFunction:public.fc_rep(int4,int4)"


def delegated_db(tmp_path: Path, name: str = "rep.db") -> Path:
    """Duas sobrecargas de `fc_rep`; a de 1 argumento só repassa (`CALLS_RPC` interna) para a
    de 2, que faz o trabalho de verdade. Reaproveitada por `test_mcp_value_origin.py`."""
    builder = GraphBuilder()
    builder.add_edge(
        Edge(REP_A, REP_B, EdgeType.CALLS_RPC, EdgeSource.SQL_AST, 1.0, {"path": "x", "line": 1})
    )
    db = tmp_path / name
    graph_store.save(builder, db, {"source": "x", "scanned_at": READ_AT})
    rows = [
        routine_store.make_source(
            REP_A, "public", "fc_rep", "plpgsql", "CREATE FUNCTION a\n", READ_AT
        ),
        routine_store.make_source(
            REP_B, "public", "fc_rep", "plpgsql", "CREATE FUNCTION b\n", READ_AT
        ),
    ]
    routine_store.write_routines(db, rows)
    routine_store.write_fields(db, [])  # `routine_fields`/`routine_queries` vazias (interpret lê)
    return db


def test_a_delegated_name_resolves_to_the_destination(tmp_path: Path) -> None:
    with sqlite3.connect(delegated_db(tmp_path)) as conn:
        interp = vo.interpret(conn, "x", "fc_rep")
        assert interp.function == REP_B and REP_B in interp.reach
        # a assinatura exata que só repassa continua indo nela, sem repasse
        direct = vo.interpret(conn, "x", REP_A)
        assert direct.function == REP_A


# --------------------------------------------------------------------------- alcance por tipo (57)


def test_interpret_by_table_reaches_the_routines_that_use_it(conn: sqlite3.Connection) -> None:
    interp = vo.interpret(conn, "valor m2 do item", TABELA_A, anchor_kind="table")
    assert interp.anchor_kind == "table"
    assert interp.function == TABELA_A
    assert interp.reach == (HELPER,)
    top = interp.candidates[0]
    assert top.table == TABELA_A and top.column == "valor"


def test_interpret_by_table_without_routine_fields_is_a_clear_error(tmp_path: Path) -> None:
    db = _db(tmp_path, "sem-campos.db")
    with sqlite3.connect(db) as conn:
        conn.execute(f"DROP TABLE {routine_store.FIELDS_TABLE}")
        conn.execute(f"DROP TABLE {routine_store.QUERIES_TABLE}")
        conn.commit()
    with (
        sqlite3.connect(db) as conn,
        pytest.raises(vo.ValueOriginError, match="campos e consultas"),
    ):
        vo.interpret(conn, "x", TABELA_A, anchor_kind="table")


def test_interpret_by_module_reaches_its_routines(conn: sqlite3.Connection) -> None:
    interp = vo.interpret(conn, "fator extra do item", "Module:tabelab", anchor_kind="module")
    assert interp.reach == (HELPER,)
    top = interp.candidates[0]
    assert top.table == TABELA_B and top.column == "extra"


def test_interpret_by_module_without_analytics_is_a_clear_error(tmp_path: Path) -> None:
    db = _db(tmp_path, "sem-modulo.db")
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE analytics_routines")
        conn.commit()
    with (
        sqlite3.connect(db) as conn,
        pytest.raises(vo.ValueOriginError, match="classificação de módulo"),
    ):
        vo.interpret(conn, "x", "Module:tabelab", anchor_kind="module")


def test_interpret_by_menu_reaches_the_pgfunction_the_screen_calls(
    conn: sqlite3.Connection,
) -> None:
    interp = vo.interpret(conn, "fator extra do item", "BusinessAction:menu:2", anchor_kind="menu")
    assert interp.reach == (HELPER,)


def test_interpret_by_menu_whose_file_calls_no_pgfunction_has_an_empty_reach(
    conn: sqlite3.Connection,
) -> None:
    # menu:1 (tabela_b) é implementado por um arquivo que só grava pelo DAO, sem CALLS_RPC
    interp = vo.interpret(conn, "x", "BusinessAction:menu:1", anchor_kind="menu")
    assert interp.reach == ()


def test_interpret_by_menu_without_an_implementing_file_is_a_clear_error(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(vo.ValueOriginError, match="nenhum arquivo implementa"):
        vo.interpret(conn, "x", "BusinessAction:menu:3", anchor_kind="menu")


def test_interpret_by_file_reaches_the_pgfunction_it_calls(conn: sqlite3.Connection) -> None:
    interp = vo.interpret(
        conn, "fator extra do item", "File:cad1_helper001.php", anchor_kind="file"
    )
    assert interp.reach == (HELPER,)


def test_interpret_with_an_unknown_anchor_kind_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(vo.ValueOriginError, match="tipo de alcance"):
        vo.interpret(conn, "x", TABELA_A, anchor_kind="endpoint")


def test_interpret_warns_when_the_reach_is_wide(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vo, "WIDE_REACH_WARNING", 1)
    interp = vo.interpret(conn, "valor m2 do item", "fc_root")
    assert any("alcance amplo" in w for w in interp.warnings)


# --------------------------------------------------------------------------- linhagem


def test_trace_value_follows_the_conditional_reassignment_to_the_other_table(
    conn: sqlite3.Connection,
) -> None:
    interp = vo.interpret(conn, "valor m2 do item", "fc_root")
    origin = vo.trace_value(conn, interp, [f"{TMP_CALC}#resultado"])
    [chain] = origin.chains
    kinds = [s.kind for s in chain.steps]
    assert kinds[0] == "write"
    assert "assign" in kinds and "select_into" in kinds
    select_step = next(s for s in chain.steps if s.kind == "select_into")
    assert select_step.table == TABELA_B and select_step.column == "extra"
    assert any(s.kind == "base_column" and s.table == TABELA_B for s in chain.steps)
    assert chain.stop == "cadastro encontrado"
    menu_step = next(s for s in chain.steps if s.kind == "menu")
    assert "Tabela B > Inclusão" in menu_step.text


def test_trace_value_direct_column_without_further_writes(conn: sqlite3.Connection) -> None:
    interp = vo.interpret(conn, "valor m2 do item", "fc_root")
    origin = vo.trace_value(conn, interp, [f"{TABELA_A}#valor"])
    [chain] = origin.chains
    assert chain.steps[0].kind == "base_column"
    assert "hipótese" in chain.stop
    hint = next(s for s in chain.steps if s.kind == "hypothesis")
    assert "func_tabela_a.php" in hint.text


def test_trace_value_invalid_target_is_a_warning_not_a_crash(conn: sqlite3.Connection) -> None:
    interp = vo.interpret(conn, "x", "fc_root")
    origin = vo.trace_value(conn, interp, ["isto nao e um alvo valido"])
    assert origin.chains == () and origin.warnings


def test_trace_value_respects_the_hop_limit(conn: sqlite3.Connection) -> None:
    interp = vo.interpret(conn, "valor m2 do item", "fc_root")
    origin = vo.trace_value(conn, interp, [f"{TMP_CALC}#resultado"], max_hops=1)
    [chain] = origin.chains
    assert chain.stop == "limite de saltos"


# --------------------------------------------------------------------------- dossiê


def test_render_dossier_is_deterministic_and_cites_lines(conn: sqlite3.Connection) -> None:
    interp = vo.interpret(conn, "valor m2 do item", "fc_root")
    origin = vo.trace_value(conn, interp, [f"{TMP_CALC}#resultado"])
    first = vo.render_dossier(origin)
    second = vo.render_dossier(origin)
    assert first == second
    assert "linha 16" in first or "linha" in first
    assert "SELECT SÓ" not in first  # instrução não grita (maiúsculas só do texto fixo)


def test_render_dossier_respects_the_word_cap() -> None:
    # `max_words` tem um piso de 500 (API do dossiê): monta conteúdo acima disso para o
    # corte acontecer de verdade.
    long_text = " ".join(f"palavra{i}" for i in range(60))
    step = vo.Step("write", long_text, None, None, None, None, None, (), 1.0, "lexical_slice")
    chain = vo.Chain("t.c", None, tuple([step] * 15), "parou")
    interp = vo.Interpretation("pergunta longa de teste", None, (), (), (), ())
    origin = vo.ValueOrigin(interp, (chain,), ())
    text = vo.render_dossier(origin, max_words=500)
    assert len(text.split()) <= 530
    assert "cortado" in text


def test_render_dossier_refuses_a_secret(conn: sqlite3.Connection) -> None:
    interp = vo.Interpretation("x", None, (), (), (), ())
    origin = vo.ValueOrigin(
        interp,
        (
            vo.Chain(
                "t.c",
                None,
                (
                    vo.Step(
                        "stop",
                        "a senha é password=Sup3rS3cretValor",
                        None,
                        None,
                        None,
                        None,
                        None,
                        (),
                        0.0,
                        "lexical_slice",
                    ),
                ),
                "parou",
            ),
        ),
        (),
    )
    with pytest.raises(vo.ValueOriginError, match="segredo aparente"):
        vo.render_dossier(origin)


def test_render_dossier_next_steps_suggests_sql_when_registration_found(
    conn: sqlite3.Connection,
) -> None:
    interp = vo.interpret(conn, "valor m2 do item", "fc_root")
    origin = vo.trace_value(conn, interp, [f"{TMP_CALC}#resultado"])
    assert origin.chains[0].stop == "cadastro encontrado"
    text = vo.render_dossier(origin)
    assert "# Próximos passos sugeridos" in text
    assert "montar a consulta SQL que leria este valor na tela encontrada" in text
    assert "hipótese" not in text.rsplit("# Próximos passos sugeridos", 1)[1]


def test_render_dossier_next_steps_suggests_confirming_the_hypothesis(
    conn: sqlite3.Connection,
) -> None:
    interp = vo.interpret(conn, "valor m2 do item", "fc_root")
    origin = vo.trace_value(conn, interp, [f"{TABELA_A}#valor"])
    assert "hipótese" in origin.chains[0].stop
    text = vo.render_dossier(origin)
    tail = text.rsplit("# Próximos passos sugeridos", 1)[1]
    assert "ajudar a confirmar olhando o trecho de código" in tail
    assert "consulta SQL" not in tail


def test_render_dossier_next_steps_suggests_a_manual_step_when_the_trail_stops(
    conn: sqlite3.Connection,
) -> None:
    interp = vo.interpret(conn, "valor m2 do item", "fc_root")
    origin = vo.trace_value(conn, interp, [f"{TMP_CALC}#resultado"], max_hops=1)
    assert origin.chains[0].stop == "limite de saltos"
    text = vo.render_dossier(origin)
    tail = text.rsplit("# Próximos passos sugeridos", 1)[1]
    assert "O rastro parou aqui" in tail


def test_render_dossier_next_steps_appears_once_covering_every_target(
    conn: sqlite3.Connection,
) -> None:
    interp = vo.interpret(conn, "valor m2 do item", "fc_root")
    origin = vo.trace_value(conn, interp, [f"{TABELA_A}#valor", f"{TMP_CALC}#resultado"])
    stops = [chain.stop for chain in origin.chains]
    assert any("hipótese" in s for s in stops) and "cadastro encontrado" in stops
    text = vo.render_dossier(origin)
    assert text.count("# Próximos passos sugeridos") == 1
    tail = text.rsplit("# Próximos passos sugeridos", 1)[1]
    assert "ajudar a confirmar olhando o trecho de código" in tail
    assert "montar a consulta SQL que leria este valor na tela encontrada" in tail


def test_render_dossier_next_steps_is_deterministic(conn: sqlite3.Connection) -> None:
    interp = vo.interpret(conn, "valor m2 do item", "fc_root")
    origin = vo.trace_value(conn, interp, [f"{TABELA_A}#valor"])
    assert vo.render_dossier(origin) == vo.render_dossier(origin)


def test_render_dossier_without_chains_has_no_next_steps_section() -> None:
    interp = vo.Interpretation("x", None, (), (), (), ())
    origin = vo.ValueOrigin(interp, (), ())
    text = vo.render_dossier(origin)
    assert "Próximos passos sugeridos" not in text


def test_hint_prompt_is_short_and_lists_known_labels(conn: sqlite3.Connection) -> None:
    interp = vo.interpret(conn, "valor m2 do item", "fc_root")
    prompt = vo.hint_prompt(interp, conn)
    assert len(prompt.split()) < 300
    assert "Valor m2 do item" in prompt or "Fator extra m2" in prompt
