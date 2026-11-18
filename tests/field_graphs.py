"""Mapa de teste do dossiê de um campo, sobre o de `formula_graphs.py`.

Os fatos vêm do `out/scan60` (conferidos em 2026-11-07):

- `caixa.arrecad.k00_valor` é gravado por 26 funções (aqui: `fc_grava_valor`, `fc_a_copia` e, com
  `writers=26`, `fc_w01`…`fc_w26`) e lido por funções que o citam pelo nome;
- `caixa.tabrecjm.k02_juros` tem 0 escritas e 0 leituras por nome; `fc_le_tudo` faz `select *`;
- `cadastro.iptubase.j01_matric` é gravado por uma cópia (`fc_atualiza_base`) e tem menu;
- `public.tmpcalc.vtotal` não tem rótulo no dicionário nem menu, e é gravado por `fc_calc_total`
  (um `update` com conta e um `insert` com outra conta).
"""

from collections.abc import Mapping, Sequence
from pathlib import Path

from cross_graphs import save as _save
from formula_graphs import (
    FUNCTIONS,
    PROJECT,
    READ_AT,
    STATEMENTS,
    TABLES,
    VARIABLES,
    _end_line,
    _line,
    add_cadastre,
    build_builder,
    function_node,
)

from php_map.graph import routines
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Node, NodeLabel
from php_map.ingest.plpgsql_rules import scan_routine
from php_map.ingest.sql_fields import analyze_statement
from php_map.ingest.table_directory import TableDirectory
from php_map.mcp.catalog import ProjectCatalog

ARRECAD = "Table:caixa.arrecad"
DISBANCO = "Table:caixa.disbanco"
IPTUBASE = "Table:cadastro.iptubase"
TABRECJM = "Table:caixa.tabrecjm"
TMPCALC = "Table:public.tmpcalc"
SEMROTULO = "Table:caixa.semrotulo"

GRAVA = "PgFunction:public.fc_grava_valor(int4)"
COPIA = "PgFunction:public.fc_a_copia(int4)"
LE_TUDO = "PgFunction:public.fc_le_tudo()"
ATUALIZA = "PgFunction:public.fc_atualiza_base(int4)"
CALC = "PgFunction:public.fc_calc_total(int4,date)"
JUROS = "PgFunction:public.fc_juros_t(int4,date,bool)"

MENU_IPTUBASE = "Cadastro > Matrícula > Inclusão de Matrícula"

EXTRA_TABLES: Mapping[str, Mapping[str, str | None]] = {
    DISBANCO: {"k00_numpre": "Numpre", "vlrpago": "Valor pago"},
    IPTUBASE: {"j01_matric": "Matrícula do Imóvel", "j01_numcgm": "CGM"},
    SEMROTULO: {"campo_a": None},
}

GRAVA_DDL = """CREATE OR REPLACE FUNCTION public.fc_grava_valor(integer)
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
declare
  numpre alias for $1;
  taxa   numeric;
  base   numeric;
begin
  select k02_juros into taxa from tabrecjm where k02_codjm = 1;
  base := taxa / 100 + 1;
  update arrecad set k00_valor = k00_valor * base where k00_numpre = numpre;
  return base;
end;
$function$
"""

COPIA_DDL = """CREATE OR REPLACE FUNCTION public.fc_a_copia(integer)
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
declare
  numpre alias for $1;
begin
  insert into arrecad (k00_numpre, k00_valor) select k00_numpre, vlrpago from disbanco;
  return 1;
end;
$function$
"""

LE_TUDO_DDL = """CREATE OR REPLACE FUNCTION public.fc_le_tudo()
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
declare
  linha record;
begin
  select * into linha from tabrecjm limit 1;
  return 1;
end;
$function$
"""

ATUALIZA_DDL = """CREATE OR REPLACE FUNCTION public.fc_atualiza_base(integer)
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
declare
  matric alias for $1;
begin
  insert into iptubase (j01_matric) values (matric);
  return 1;
end;
$function$
"""

LEITOR_DDL = """CREATE OR REPLACE FUNCTION public.fc_leitor_valor()
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
declare
  total numeric;
begin
  select sum(k00_valor) into total from arrecad where k00_valor > 0 order by k00_valor;
  return total;
end;
$function$
"""

LEITOR = "PgFunction:public.fc_leitor_valor()"
LACO = "PgFunction:public.fc_laco(int4)"
UNIAO = "PgFunction:public.fc_uniao()"

UNIAO_DDL = """CREATE OR REPLACE FUNCTION public.fc_uniao()
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
begin
  insert into iptubase (j01_numcgm)
    select k00_numpre from disbanco union select k00_numpre from disbanco;
  return 1;
end;
$function$
"""

LACO_DDL = """CREATE OR REPLACE FUNCTION public.fc_laco(integer)
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
declare
  numpre alias for $1;
  rcalc  record;
begin
  for rcalc in select k02_codigo * 2 as dobro from tabrec loop
    insert into disbanco (k00_numpre, vlrpago) values (numpre, rcalc.dobro);
  end loop;
  return 1;
end;
$function$
"""


def _writer(n: int) -> tuple[str, str]:
    node_id = f"PgFunction:public.fc_w{n:02d}(int4)"
    ddl = (
        f"CREATE OR REPLACE FUNCTION public.fc_w{n:02d}(integer)\n RETURNS numeric\n"
        " LANGUAGE plpgsql\nAS $function$\nbegin\n"
        f"  update arrecad set k00_valor = k00_valor + {n} where k00_numpre = 1;\n"
        "  return 1;\nend;\n$function$\n"
    )
    return node_id, ddl


def functions(writers: int = 0) -> dict[str, str]:
    found: dict[str, str] = {
        **FUNCTIONS,
        GRAVA: GRAVA_DDL,
        COPIA: COPIA_DDL,
        LE_TUDO: LE_TUDO_DDL,
        ATUALIZA: ATUALIZA_DDL,
        LEITOR: LEITOR_DDL,
        LACO: LACO_DDL,
        UNIAO: UNIAO_DDL,
    }
    for n in range(1, writers + 1):
        node_id, ddl = _writer(n)
        found[node_id] = ddl
    return found


def tables() -> dict[str, Mapping[str, str | None]]:
    return {**TABLES, **EXTRA_TABLES}


def statements(writers: int = 0) -> list[tuple[str, str, str]]:
    found = [
        *STATEMENTS,
        (
            GRAVA,
            "select k02_juros",
            "select k02_juros into taxa from tabrecjm where k02_codjm = 1",
        ),
        (
            GRAVA,
            "update arrecad",
            "update arrecad set k00_valor = k00_valor * base where k00_numpre = numpre",
        ),
        (
            COPIA,
            "insert into arrecad",
            "insert into arrecad (k00_numpre, k00_valor) select k00_numpre, vlrpago from disbanco",
        ),
        (LE_TUDO, "select * into", "select * from tabrecjm limit 1"),
        (
            UNIAO,
            "insert into iptubase",
            "insert into iptubase (j01_numcgm) select k00_numpre from disbanco "
            "union select k00_numpre from disbanco",
        ),
        (LACO, "for rcalc in", "select k02_codigo * 2 as dobro from tabrec"),
        (
            LACO,
            "insert into disbanco",
            "insert into disbanco (k00_numpre, vlrpago) values (numpre, rcalc.dobro)",
        ),
        (ATUALIZA, "insert into iptubase", "insert into iptubase (j01_matric) values (matric)"),
        (
            LEITOR,
            "select sum",
            "select sum(k00_valor) from arrecad where k00_valor > 0 order by k00_valor",
        ),
    ]
    for n in range(1, writers + 1):
        found.append(
            (
                _writer(n)[0],
                "update arrecad",
                f"update arrecad set k00_valor = k00_valor + {n} where k00_numpre = 1",
            )
        )
    return found


def _directory() -> TableDirectory:
    directory = TableDirectory()
    for table_id, columns in tables().items():
        schema, _dot, name = table_id.partition(":")[2].partition(".")
        directory.add(schema, name, columns=tuple(columns))
    return directory


def build_builder_with_tables(writers: int) -> GraphBuilder:
    builder = build_builder()
    for node_id in functions(writers):
        if node_id not in FUNCTIONS:
            builder.add_node(function_node(node_id))
    for table_id, columns in EXTRA_TABLES.items():
        schema, _dot, name = table_id.partition(":")[2].partition(".")
        catalog = {
            c: {"label": label, "description": label} for c, label in columns.items() if label
        }
        builder.add_node(
            Node(
                table_id,
                NodeLabel.TABLE,
                {
                    "name": name,
                    "schema": schema,
                    "columns": list(columns),
                    "columns_catalog": catalog,
                },
            )
        )
    add_cadastre(
        builder,
        IPTUBASE,
        "cl_iptubase",
        "cad1_iptubase001.php",
        "BusinessAction:menu:900",
        MENU_IPTUBASE,
    )
    return builder


def write_texts(
    db: Path,
    funcs: Mapping[str, str],
    stmts: Sequence[tuple[str, str, str]],
) -> None:
    make = routines.make_source
    rows = []
    for node_id, ddl in funcs.items():
        schema, _dot, rest = node_id.partition(":")[2].partition(".")
        rows.append(make(node_id, schema, rest.partition("(")[0], "plpgsql", ddl, READ_AT))
    routines.write_routines(db, rows)
    routines.write_rules(db, [(i, *scan_routine(d)) for i, d in funcs.items()])
    directory = _directory()
    variables = (*VARIABLES, "taxa", "base", "numpre", "matric", "total")
    query_rows = []
    seqs: dict[str, int] = {}
    for node_id, marker, sql in stmts:
        ddl = funcs[node_id]
        line = _line(ddl, marker)
        seq = seqs.get(node_id, 0)
        seqs[node_id] = seq + 1
        facts = analyze_statement(sql, directory=directory, variables=variables)
        query_rows.append(routines.QueryRow(node_id, seq, line, _end_line(ddl, line), facts))
    routines.write_fields(db, query_rows)


def build_db(tmp_path: Path, name: str = "campos", *, writers: int = 0) -> Path:
    db = _save(
        tmp_path, name, build_builder_with_tables(writers), {"project": PROJECT, "source": "x"}
    )
    write_texts(db, functions(writers), statements(writers))
    return db


def build_catalog(tmp_path: Path, *, writers: int = 0) -> ProjectCatalog:
    return ProjectCatalog.from_dbs([build_db(tmp_path, writers=writers)])


__all__ = ["PROJECT", "build_catalog", "build_db"]
