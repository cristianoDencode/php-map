"""Mapa de teste da busca por termo e do dossiê de menu, em miniatura do `out/scan60`.

Os fatos vêm do `out/scan60` (conferidos em 2026-11-07):

- "valor do metro quadrado do terreno" casa 8 colunas ≥ 0,6; só as lidas por alguma função da base
  (`facevalor.j81_valorterreno`, `zonasvalor.j51_valorm2t`) vão para «Usadas em cálculo»;
  `face.j37_valor` só é cadastrada; `iptucalc.j23_m2terr` só é gravada por função (sem tela);
- "lote" vale como `terreno` e traz ruído: `vacinas.*lote` a 0,65 em "área do terreno";
- `fc_calculoiptu` chama a função do município por `execute` dinâmico (o menu 1533 a alcança).
"""

from collections.abc import Mapping, Sequence
from pathlib import Path

from cross_graphs import save as _save
from formula_graphs import (
    PROJECT,
    READ_AT,
    _end_line,
    _line,
    add_cadastre,
    function_node,
    simple_ddl,
)
from formula_graphs import build_builder as _base_builder

from php_map.graph import routines
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Edge, EdgeSource, EdgeType, Node, NodeLabel
from php_map.ingest.plpgsql_rules import scan_routine
from php_map.ingest.sql_fields import analyze_statement
from php_map.ingest.table_directory import TableDirectory
from php_map.mcp.catalog import ProjectCatalog

FACEVAL = "Table:cadastro.faceval"
ZONASVAL = "Table:cadastro.zonasval"
FACE = "Table:cadastro.face"
IPTUCALC = "Table:cadastro.iptucalc"
SETORLOC = "Table:cadastro.setorloc"
VACINA = "Table:vacinas.vacina"
ITBI = "Table:itbi.itbi"
IPTUNUMP = "Table:cadastro.iptunump"
IPTUCALLOG = "Table:cadastro.iptucalclog"
AUDITORIA = "Table:configuracoes.db_acount"

MENU_FACEVAL = "Cadastro > Cadastros > Valores da Face > Inclusão"
MENU_FACE = "Cadastro > Cadastros > Face de Quadra > Inclusão"
MENU_CALC = "Cadastro > Procedimentos > Cálculo IPTU > Cálculo Parcial"
MENU_EXCLUI = "Cadastro > Procedimentos > Cálculo IPTU > Exclusão de Cálculo Parcial"
MENU_ORFAO = "Cadastro > Procedimentos > Menu sem tela"
MENU_CALC_ID = "BusinessAction:menu:1533"
MENU_EXCLUI_ID = "BusinessAction:menu:1578"
MENU_ORFAO_ID = "BusinessAction:menu:2000"
MENU_SEM_CAMINHO_ID = "BusinessAction:menu:3656"  # do `formula_graphs` (cai1_tabrec001.php)
MENU_JUROS_ID = "BusinessAction:menu:1317"  # do `formula_graphs` (cai1_recejm001.php)

VVT_FACE = "PgFunction:public.fc_vvt_face(int4)"
VVT_ZONA = "PgFunction:public.fc_vvt_zona(int4)"
VVT_ZONA2 = "PgFunction:public.fc_vvt_zona2(int4)"
GRAVA_CALC = "PgFunction:public.fc_grava_calc(int4)"
CALCULOIPTU = "PgFunction:public.fc_calculoiptu(int4)"
VERIFICA = "PgFunction:public.fc_verificacalculo(int4)"
SEM_FORMULA = "PgFunction:public.fc_sem_formula(int4)"

# tabela → coluna → nome no dicionário
TERM_TABLES: Mapping[str, Mapping[str, str | None]] = {
    FACEVAL: {"j81_face": "Face", "j81_valorterreno": "Valor m2 terreno"},
    ZONASVAL: {"j51_zona": "Zona", "j51_valorm2t": "Valor Terreno (m²)"},
    FACE: {"j37_face": "Face", "j37_valor": "Valor M2 terreno"},
    IPTUCALC: {"j23_matric": "Matricula", "j23_m2terr": "Valor M2 Terreno"},
    SETORLOC: {"j05_setor": "Setor", "j05_valor": "Valor m² terreno"},
    VACINA: {"vac_codigo": "Código", "vac_aplicalote": "Aplica lote"},
    ITBI: {"it01_areaterreno": "Área do terreno", "it01_areatrans": "Área transmitida do terreno"},
    IPTUNUMP: {"j20_matric": "Matrícula", "j20_numpre": "Numpre"},
    IPTUCALLOG: {"j27_codigo": "Sequencial", "j27_data": "Data do calculo"},
    AUDITORIA: {"id_acount": "Código da auditoria"},
}
TABLE_LABELS = {FACEVAL: "Valores da face por ano", ITBI: "ITBI"}


def _ddl(name: str, body: str, declare: str = "") -> str:
    return simple_ddl(name, "integer", body, declare)


FUNCTIONS: Mapping[str, str] = {
    VVT_FACE: _ddl(
        "fc_vvt_face",
        "  select j81_valorterreno into v_m2 from faceval where j81_face = $1;\n"
        "  v_vvt := v_m2 * 2;\n  return v_vvt;",
        "  v_m2 numeric;\n  v_vvt numeric;\n",
    ),
    VVT_ZONA: _ddl(
        "fc_vvt_zona",
        "  select j51_valorm2t into v_m2 from zonasval where j51_zona = $1;\n  return v_m2;",
        "  v_m2 numeric;\n",
    ),
    VVT_ZONA2: _ddl(
        "fc_vvt_zona2",
        "  select j51_valorm2t into v_m2 from zonasval where j51_zona = $1;\n  return v_m2 + 1;",
        "  v_m2 numeric;\n",
    ),
    GRAVA_CALC: _ddl(
        "fc_grava_calc",
        "  insert into iptucalc (j23_matric, j23_m2terr) values ($1, 10);\n  return 1;",
    ),
    CALCULOIPTU: _ddl(
        "fc_calculoiptu",
        "  v_a := $1 * 2;\n  v_b := v_a + 1;\n  execute 'select ' || v_nome;\n  return v_b;",
        "  v_a numeric;\n  v_b numeric;\n  v_nome text;\n",
    ),
    VERIFICA: _ddl("fc_verificacalculo", "  v_a := $1 + 1;\n  return v_a;", "  v_a numeric;\n"),
    SEM_FORMULA: _ddl("fc_sem_formula", "  perform 1;\n  return 1;"),
}

# (função, trecho que marca a linha, SQL como o scan lê; "" = `execute`, kind dynamic)
STATEMENTS: Sequence[tuple[str, str, str]] = (
    (
        VVT_FACE,
        "select j81_valorterreno",
        "select j81_valorterreno from faceval where j81_face = 1",
    ),
    (VVT_ZONA, "select j51_valorm2t", "select j51_valorm2t from zonasval where j51_zona = 1"),
    (VVT_ZONA2, "select j51_valorm2t", "select j51_valorm2t from zonasval where j51_zona = 1"),
    (
        GRAVA_CALC,
        "insert into iptucalc",
        "insert into iptucalc (j23_matric, j23_m2terr) values (1, 10)",
    ),
    (CALCULOIPTU, "execute", ""),
)


def _edge(src: str, dst: str, kind: EdgeType, line: int = 1) -> Edge:
    return Edge(
        src, dst, kind, EdgeSource.SQL_AST, 1.0, {"path": src.partition(":")[2], "line": line}
    )


def _php_menu(
    b: GraphBuilder,
    menu_id: str,
    path: str | None,
    screen: str,
    *,
    rpc: Sequence[str] = (),
    writes: Sequence[str] = (),
    dao: tuple[str, str] | None = None,
) -> None:
    """Um item de menu, a tela que o implementa, as funções que ela chama (`CALLS_RPC`), as
    tabelas que ela grava e um DAO (`classe`, `tabela`) que ela chama."""
    file_id = f"File:{screen}"
    b.add_node(Node(file_id, NodeLabel.FILE, {"path": screen, "name": screen}))
    attrs = {"name": (path or "").rpartition(" > ")[2], "menu_path": path} if path else {}
    b.add_node(Node(menu_id, NodeLabel.BUSINESS_ACTION, attrs))
    b.add_edge(
        Edge(
            file_id,
            menu_id,
            EdgeType.IMPLEMENTS_ACTION,
            EdgeSource.ECIDADE_CATALOG,
            1.0,
            {"path": screen, "line": 1},
        )  # fmt: skip
    )
    for routine in rpc:
        b.add_edge(_edge(file_id, routine, EdgeType.CALLS_RPC))
    for table in writes:
        b.add_edge(_edge(file_id, table, EdgeType.INSERTS))
    if dao is not None:
        cls, table = dao
        method = f"Method:{cls}::incluir"
        b.add_node(Node(method, NodeLabel.METHOD, {"name": "incluir"}))
        b.add_edge(_edge(file_id, method, EdgeType.CALLS))
        b.add_edge(_edge(method, table, EdgeType.INSERTS))
        b.add_edge(_edge(method, AUDITORIA, EdgeType.INSERTS))


def build_builder() -> GraphBuilder:
    b = _base_builder()
    for node_id in FUNCTIONS:
        b.add_node(function_node(node_id))
    for table_id, columns in TERM_TABLES.items():
        schema, _dot, name = table_id.partition(":")[2].partition(".")
        catalog = {
            c: {"label": label, "description": label} for c, label in columns.items() if label
        }
        attrs: dict[str, object] = {
            "name": name,
            "schema": schema,
            "columns": list(columns),
            "columns_catalog": catalog,
        }
        if table_id in TABLE_LABELS:
            attrs["catalog"] = {"label": TABLE_LABELS[table_id]}
        b.add_node(Node(table_id, NodeLabel.TABLE, attrs))
    # onde as tabelas são cadastradas
    add_cadastre(
        b, FACEVAL, "cl_faceval", "cad1_faceval001.php", "BusinessAction:menu:900", MENU_FACEVAL
    )
    add_cadastre(b, FACE, "cl_face", "cad1_face004.php", "BusinessAction:menu:910", MENU_FACE)
    add_cadastre(b, ZONASVAL, "cl_zonasval", "cad1_zonasval001.php", None, None)
    # quem escreve a tabela sem tela: só uma função da base
    b.add_edge(_edge(GRAVA_CALC, IPTUCALC, EdgeType.INSERTS, 5))
    # os menus do dossiê
    _php_menu(
        b, MENU_CALC_ID, MENU_CALC, "cad4_calciptu.php", rpc=(CALCULOIPTU, VERIFICA),
        writes=(IPTUCALLOG,), dao=("cl_iptunump", IPTUNUMP),
    )  # fmt: skip
    _php_menu(
        b, MENU_EXCLUI_ID, MENU_EXCLUI, "cad4_excluicalciptu.php",
        rpc=(VERIFICA, CALCULOIPTU, SEM_FORMULA),
    )  # fmt: skip
    b.add_node(Node(MENU_ORFAO_ID, NodeLabel.BUSINESS_ACTION, {"menu_path": MENU_ORFAO}))
    return b


def _directory() -> TableDirectory:
    directory = TableDirectory()
    for table_id, columns in TERM_TABLES.items():
        schema, _dot, name = table_id.partition(":")[2].partition(".")
        directory.add(schema, name, columns=tuple(columns))
    return directory


def write_texts(db: Path) -> None:
    make = routines.make_source
    rows = []
    for node_id, ddl in FUNCTIONS.items():
        schema, _dot, rest = node_id.partition(":")[2].partition(".")
        rows.append(make(node_id, schema, rest.partition("(")[0], "plpgsql", ddl, READ_AT))
    routines.write_routines(db, rows)
    routines.write_rules(db, [(i, *scan_routine(d)) for i, d in FUNCTIONS.items()])
    directory = _directory()
    query_rows = []
    seqs: dict[str, int] = {}
    for node_id, marker, sql in STATEMENTS:
        ddl = FUNCTIONS[node_id]
        line = _line(ddl, marker)
        seq = seqs.get(node_id, 0)
        seqs[node_id] = seq + 1
        facts = analyze_statement(sql, directory=directory, variables=("v_m2", "v_vvt"))
        query_rows.append(routines.QueryRow(node_id, seq, line, _end_line(ddl, line), facts))
    routines.write_fields(db, query_rows)


def build_db(tmp_path: Path, name: str = "termos") -> Path:
    db = _save(tmp_path, name, build_builder(), {"project": PROJECT, "source": "x"})
    write_texts(db)
    return db


def build_catalog(tmp_path: Path) -> ProjectCatalog:
    return ProjectCatalog.from_dbs([build_db(tmp_path)])


__all__ = ["PROJECT", "build_catalog", "build_db"]
