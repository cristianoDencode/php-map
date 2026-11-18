"""Mapa de teste das fórmulas: trechos do e-cidade real escritos à mão em miniatura.

Os fatos vêm do `out/scan60` (conferidos em 2026-11-07): `fc_calcula` tem registro de laço sobre
subconsulta de `arrecad`, chama `fc_juros`, e `fc_juros` lê `tabrecjm`, `tabrec` e `infla`. Três
tabelas cobrem os três motivos de "não rastreado":

- `caixa.tabrecjm`: `cl_tabrecjm::incluir` ← `cai1_recejm001.php` ← menu com caminho (rastreado);
- `caixa.tabrec`: a ação da tela não tem `menu_path` ("menu sem caminho");
- `inflatores.infla`: a tela não implementa nenhuma ação ("tela de cadastro sem item de menu");
- `caixa.arrecad` e `caixa.calend`: ninguém grava pela tela ("tabela sem tela de cadastro").
"""

from collections.abc import Mapping, Sequence
from pathlib import Path

from cross_graphs import save as _save

from php_map.graph import routines
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Edge, EdgeSource, EdgeType, Node, NodeLabel
from php_map.ingest.plpgsql_rules import scan_routine
from php_map.ingest.sql_fields import analyze_statement
from php_map.ingest.table_directory import TableDirectory
from php_map.mcp.catalog import ProjectCatalog

PROJECT = "ecidade-f"
READ_AT = "2026-11-07T10:00:00-03:00"

CALC = "PgFunction:public.fc_calc_total(int4,date)"
JUROS = "PgFunction:public.fc_juros_t(int4,date,bool)"
AMB_DATE = "PgFunction:public.fc_amb(int4,date)"
AMB_TEXT = "PgFunction:public.fc_amb(int4,text)"
TRIGGER = "PgFunction:public.fc_trg_total()"
# duas sobrecargas, a 1ª só repassa para a 2ª (`CALLS_RPC` interna à família)
DELEGA_A = "PgFunction:public.fc_delega(int4)"
DELEGA_B = "PgFunction:public.fc_delega(int4,int4)"

ARRECAD = "Table:caixa.arrecad"
TABRECJM = "Table:caixa.tabrecjm"
TABREC = "Table:caixa.tabrec"
INFLA = "Table:inflatores.infla"
CALEND = "Table:caixa.calend"
TMPCALC = "Table:public.tmpcalc"

MENU_TABRECJM = "Arrecadação > Cadastros > Juros e Multas > Inclusão de Tipo Juro e Multa"

CALC_DDL = """CREATE OR REPLACE FUNCTION public.fc_calc_total(integer, date)
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
declare
  numpre        alias for $1;
  dtvenc        alias for $2;
  valor_receita numeric default 0;
  receita       integer;
  correcao      numeric;
  juro          numeric;
  total         numeric;
  valor_hist    numeric default 0;
  fator         numeric;
  texto         text;
  record_alias  record;
begin
  -- total := a * b - c   (comentário com operador: não é conta)
  texto := 'a * b + c';
  total := 0;
  total := correcao;
  fator := dtvenc::numeric(20,10);
  for record_alias in select *
        from (select k00_receit, k00_valor from arrecad where k00_numpre = numpre) as xx loop
    receita       := record_alias.k00_receit;
    valor_receita := record_alias.k00_valor;
    juro := round((correcao + valor_receita)
                  * fc_juros_t(receita, dtvenc, false)::numeric(20,10), 2);
    valor_hist := valor_hist + round(valor_receita, 2);
  end loop;
  select k02_corr * 2 into correcao from tabrecjm where k02_codjm = 1;
  update tmpcalc set vtotal = juro + correcao where vnumpre = numpre;
  insert into tmpcalc (vnumpre, vtotal) values (numpre, juro * 2);
  perform fc_amb(numpre, dtvenc);
  total := fc_amb(numpre, dtvenc) + 1;
  return total + juro;
end;
$function$
"""

JUROS_DDL = """CREATE OR REPLACE FUNCTION public.fc_juros_t(integer, date, boolean)
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
declare
  receita   alias for $1;
  datavenc  alias for $2;
  lraise    alias for $3;
  nperc     numeric;
  nfator    numeric;
  ninfla    numeric;
  dtferiado date;
begin
  select k02_juros into nperc
    from tabrecjm inner join tabrec on tabrec.k02_codjm = tabrecjm.k02_codjm
   where k02_codigo = receita;
  select i02_valor into ninfla from infla where i02_codigo = 1;
  select k13_data into dtferiado from calend where k13_data = datavenc;
  nfator := coalesce(nperc, 0) / 100 * ninfla;
  return nfator;
end;
$function$
"""

AMB_DATE_DDL = """CREATE OR REPLACE FUNCTION public.fc_amb(integer, date)
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
begin
  return 1;
end;
$function$
"""

AMB_TEXT_DDL = AMB_DATE_DDL.replace("integer, date", "integer, text")

TRIGGER_DDL = """CREATE OR REPLACE FUNCTION public.fc_trg_total()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
  NEW.vtotal := NEW.vtotal + 1;
  return NEW;
end;
$function$
"""

DELEGA_A_DDL = """CREATE OR REPLACE FUNCTION public.fc_delega(integer)
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
begin
  return fc_delega($1, 0);
end;
$function$
"""

DELEGA_B_DDL = """CREATE OR REPLACE FUNCTION public.fc_delega(integer, integer)
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
declare
  total numeric;
begin
  total := $1 * 2 + $2;
  return total;
end;
$function$
"""

FUNCTIONS: Mapping[str, str] = {
    CALC: CALC_DDL,
    JUROS: JUROS_DDL,
    AMB_DATE: AMB_DATE_DDL,
    AMB_TEXT: AMB_TEXT_DDL,
    TRIGGER: TRIGGER_DDL,
    DELEGA_A: DELEGA_A_DDL,
    DELEGA_B: DELEGA_B_DDL,
}

# tabela → colunas e o nome do dicionário
TABLES: Mapping[str, Mapping[str, str | None]] = {
    ARRECAD: {"k00_numpre": "Numpre", "k00_receit": "Receita", "k00_valor": "Valor"},
    TABRECJM: {"k02_codjm": "Código", "k02_juros": "juros", "k02_corr": "inflator para correcao"},
    TABREC: {"k02_codigo": "Receita", "k02_codjm": "Código do juro e multa"},
    INFLA: {"i02_codigo": "codigo do inflator", "i02_valor": "valor do inflator"},
    CALEND: {"k13_data": "data do feriado"},
    TMPCALC: {"vnumpre": None, "vtotal": None},
}


def _edge(src: str, dst: str, kind: EdgeType, line: int = 1) -> Edge:
    return Edge(
        src, dst, kind, EdgeSource.SQL_AST, 1.0, {"path": src.partition(":")[2], "line": line}
    )


def function_node(node_id: str) -> Node:
    signature = node_id.partition("(")[2].rstrip(")")
    name = node_id.partition(".")[2].partition("(")[0]
    args = signature.split(",") if signature else []
    return Node(
        node_id,
        NodeLabel.PG_FUNCTION,
        {"name": name, "schema": "public", "args": args, "returns": "numeric"},
    )


def add_cadastre(
    b: GraphBuilder, table: str, dao: str, screen: str, menu: str | None, path: str | None
) -> None:
    """`cl_x::incluir` grava a tabela; a tela chama o método; a tela implementa a ação `menu`."""
    method = f"Method:{dao}::incluir"
    b.add_node(Node(method, NodeLabel.METHOD, {"name": "incluir"}))
    b.add_edge(_edge(method, table, EdgeType.INSERTS, 10))
    file_id = f"File:{screen}"
    b.add_node(Node(file_id, NodeLabel.FILE, {"path": screen, "name": screen}))
    b.add_edge(_edge(file_id, method, EdgeType.CALLS, 5))
    if menu is not None:
        attrs = {"menu_path": path} if path else {}
        b.add_node(Node(menu, NodeLabel.BUSINESS_ACTION, attrs))
        b.add_edge(
            Edge(
                file_id,
                menu,
                EdgeType.IMPLEMENTS_ACTION,
                EdgeSource.ECIDADE_CATALOG,
                1.0,
                {"path": screen, "line": 1},
            )  # fmt: skip
        )


def build_builder() -> GraphBuilder:
    b = GraphBuilder()
    for node_id in FUNCTIONS:
        b.add_node(function_node(node_id))
    for table_id, columns in TABLES.items():
        schema, _dot, name = table_id.partition(":")[2].partition(".")
        catalog = {
            c: {"label": label, "description": label} for c, label in columns.items() if label
        }
        b.add_node(
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
        b, TABRECJM, "cl_tabrecjm", "cai1_recejm001.php", "BusinessAction:menu:1317", MENU_TABRECJM
    )
    add_cadastre(b, TABREC, "cl_tabrec", "cai1_tabrec001.php", "BusinessAction:menu:3656", None)
    add_cadastre(b, INFLA, "cl_infla", "inf1_infla001.php", None, None)
    b.add_edge(_edge(DELEGA_A, DELEGA_B, EdgeType.CALLS_RPC, 2))  # repasse
    return b


def _line(ddl: str, marker: str) -> int:
    for number, text in enumerate(ddl.split("\n"), start=1):
        if marker in text:
            return number
    raise AssertionError(f"marcador ausente: {marker}")


def _end_line(ddl: str, line: int) -> int:
    """A última linha da instrução que começa em `line` (termina em `;` ou em `loop`)."""
    lines = ddl.splitlines()
    for number in range(line, len(lines) + 1):
        text = lines[number - 1].rstrip()
        if text.endswith((";", " loop")):
            return number
    return line


def _directory() -> TableDirectory:
    directory = TableDirectory()
    for table_id, columns in TABLES.items():
        schema, _dot, name = table_id.partition(":")[2].partition(".")
        directory.add(schema, name, columns=tuple(columns))
    return directory


# (função, trecho que marca a linha, SQL como o scan lê)
STATEMENTS: Sequence[tuple[str, str, str]] = (
    (
        CALC,
        "for record_alias in",
        "select * from (select k00_receit, k00_valor from arrecad where k00_numpre = numpre) as xx",
    ),
    (
        CALC,
        "select k02_corr * 2",
        "select k02_corr * 2 into correcao from tabrecjm where k02_codjm = 1",
    ),
    (CALC, "update tmpcalc", "update tmpcalc set vtotal = juro + correcao where vnumpre = numpre"),
    (
        CALC,
        "insert into tmpcalc",
        "insert into tmpcalc (vnumpre, vtotal) values (numpre, juro * 2)",
    ),
    (
        JUROS,
        "select k02_juros",
        "select k02_juros into nperc from tabrecjm inner join tabrec"
        " on tabrec.k02_codjm = tabrecjm.k02_codjm where k02_codigo = receita",
    ),
    (JUROS, "select i02_valor", "select i02_valor into ninfla from infla where i02_codigo = 1"),
    (
        JUROS,
        "select k13_data",
        "select k13_data into dtferiado from calend where k13_data = datavenc",
    ),
)

VARIABLES = (
    "numpre", "dtvenc", "juro", "correcao", "receita", "datavenc", "nperc", "ninfla", "dtferiado",
)  # fmt: skip


def write_texts(
    db: Path, functions: Mapping[str, str], statements: Sequence[tuple[str, str, str]]
) -> None:
    """Grava `routine_sources`, regras/perfil e `routine_queries`/`routine_fields`."""
    make = routines.make_source
    rows = []
    for node_id, ddl in functions.items():
        schema, _dot, rest = node_id.partition(":")[2].partition(".")
        rows.append(make(node_id, schema, rest.partition("(")[0], "plpgsql", ddl, READ_AT))
    routines.write_routines(db, rows)
    routines.write_rules(db, [(i, *scan_routine(d)) for i, d in functions.items()])
    directory = _directory()
    query_rows = []
    seqs: dict[str, int] = {}
    for node_id, marker, sql in statements:
        ddl = functions[node_id]
        line = _line(ddl, marker)
        seq = seqs.get(node_id, 0)
        seqs[node_id] = seq + 1
        facts = analyze_statement(sql, directory=directory, variables=VARIABLES)
        query_rows.append(routines.QueryRow(node_id, seq, line, _end_line(ddl, line), facts))
    routines.write_fields(db, query_rows)


def build_db(tmp_path: Path, name: str = "formulas") -> Path:
    db = _save(tmp_path, name, build_builder(), {"project": PROJECT, "source": "x"})
    write_texts(db, FUNCTIONS, STATEMENTS)
    return db


def build_catalog(tmp_path: Path) -> ProjectCatalog:
    return ProjectCatalog.from_dbs([build_db(tmp_path)])


def plain_db(tmp_path: Path, functions: Mapping[str, str], name: str = "plain") -> Path:
    """Um mapa mínimo: só as funções (e o texto delas), sem tabelas nem consultas."""
    b = GraphBuilder()
    for node_id in functions:
        b.add_node(function_node(node_id))
    db = _save(tmp_path, name, b, {"project": PROJECT, "source": "x"})
    write_texts(db, functions, ())
    return db


def simple_ddl(name: str, args: str, body: str, declare: str = "") -> str:
    """Uma função plpgsql com o `body` entre `begin` e `end` (e `declare` se houver variáveis)."""
    declare = "declare\n" + declare if declare else ""
    return (
        f"CREATE OR REPLACE FUNCTION public.{name}({args})\n RETURNS numeric\n"
        f" LANGUAGE plpgsql\nAS $function$\n{declare}begin\n{body}\nend;\n$function$\n"
    )
