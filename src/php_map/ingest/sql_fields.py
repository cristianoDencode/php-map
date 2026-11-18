"""Campos e consultas por instrução SQL dentro de uma função da base.

Função pura sobre o texto de **uma** instrução SQL: acha as tabelas (pelo `TableDirectory`, que
já resolve o nome sem schema pelo `search_path`) e as colunas que ela lê, grava, filtra,
junta ou ordena — o papel vem do **contexto sintático** (cláusula onde a coluna aparece), nunca
do nome da coluna.

**Variável × coluna:** o nome declarado no PL/pgSQL (`DECLARE`, parâmetro, `FOR`, campo de
`record`) nunca vira coluna — `variables` entra na busca **antes** do schema.

**Escopo por instrução:** cada `SELECT` (inclusive os dois lados de um `UNION` e o de um CTE)
tem o seu próprio conjunto de tabelas; uma coluna sem qualificação só é buscada nas tabelas do
`FROM`/`JOIN` da **mesma** instrução — não sobe para quem a envolve (correlação não é resolvida:
melhor deixar de fora do que inventar).
"""

import re
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass, field

import pglast
from pglast import ast
from pglast.visitors import Visitor

from php_map.graph.model import table_id
from php_map.ingest.plpgsql_rules import _function_body
from php_map.ingest.sql_schema import (
    _ASSIGNMENT,
    _MAY_REFERENCE,
    _SQL_START,
    _STATEMENT_FIELDS,
    _plpgsql_expressions,
    _trigger_as_record,
    regex_refs,
    split_statements,
)
from php_map.ingest.table_directory import TableDirectory
from php_map.sqlutil import TableOp

MAX_TEXT = 600
ROLES = ("read", "write", "filter", "join", "sort")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class FieldUse:
    table: str  # `Table:<schema>.<nome>`
    column: str  # nome da coluna ou "*"
    role: str  # read | write | filter | join | sort
    confidence: float
    source: str = "SQL_AST"  # SQL_AST | SQL_REGEX


@dataclass(frozen=True, slots=True)
class StatementFacts:
    kind: str  # select | insert | update | delete | dynamic | parse_fallback
    text: str  # normalizada (espaço colapsado), até MAX_TEXT caracteres
    tables: tuple[str, ...]
    fields: tuple[FieldUse, ...]
    unresolved: tuple[str, ...]  # colunas sem tabela candidata ("qualificador.coluna" ou só o nome)
    source: str = "SQL_AST"  # SQL_AST (o pglast leu) ou SQL_REGEX (fallback, só tabelas)
    confidence: float = 1.0  # 1.0 no SQL_AST; 0.5 no SQL_REGEX, como o fallback do schema


def _normalize_text(sql: str) -> str:
    text = _SPACES.sub(" ", sql).strip()
    return text[:MAX_TEXT]


# --------------------------------------------------------------------------- escopo de tabelas


@dataclass(slots=True)
class _Scope:
    """Tabelas conhecidas nesta instrução (ou neste lado de um `UNION`/CTE): alias → ID do nó e
    as colunas de cada uma, para resolver coluna sem qualificação."""

    alias_map: dict[str, str] = field(default_factory=dict)
    columns_of: dict[str, frozenset[str]] = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)
    # Tabelas candidatas para `NEW`/`OLD` (função de trigger usada em mais de uma tabela → 1/n).
    trigger_tables: tuple[str, ...] = ()

    def add_table(
        self, directory: TableDirectory, schema: str | None, name: str, alias: str | None
    ) -> str:
        resolved_schema, _rule = directory.resolve(name, schema)
        tid = table_id(name, resolved_schema)
        info = directory.info(resolved_schema, name)
        self.columns_of[tid] = frozenset(c.lower() for c in info.columns) if info else frozenset()
        self.alias_map[(alias or name).lower()] = tid
        self.alias_map.setdefault(name.lower(), tid)
        if tid not in self.tables:
            self.tables.append(tid)
        return tid

    def add_known(self, tid: str, alias: str | None, columns: frozenset[str] = frozenset()) -> None:
        """Tabela já resolvida em outro escopo (ex.: a tabela de `NEW`/`OLD` do trigger)."""
        if alias:
            self.alias_map[alias.lower()] = tid
        self.columns_of.setdefault(tid, columns)
        if tid not in self.tables:
            self.tables.append(tid)


def _alias_of(range_var: ast.RangeVar) -> str | None:
    return range_var.alias.aliasname if range_var.alias is not None else None


class _ColumnCollector(Visitor):
    def __init__(self) -> None:
        self.refs: list[ast.ColumnRef] = []

    def visit_ColumnRef(self, _ancestors: object, node: ast.ColumnRef) -> None:
        self.refs.append(node)


def _columns_in(node: ast.Node | tuple[ast.Node, ...] | None) -> list[ast.ColumnRef]:
    if node is None:
        return []
    collector = _ColumnCollector()
    collector(node)
    return collector.refs


def _column_parts(ref: ast.ColumnRef) -> tuple[str | None, str]:
    """`(qualificador ou None, nome da coluna ou "*")`."""
    parts = list(ref.fields or ())
    if not parts:
        return None, "*"
    last = parts[-1]
    name = "*" if isinstance(last, ast.A_Star) else str(getattr(last, "sval", "") or "")
    qualifier = None
    if len(parts) > 1 and isinstance(parts[-2], ast.String) and parts[-2].sval:
        qualifier = parts[-2].sval
    return qualifier, name


# --------------------------------------------------------------------------- FROM / JOIN


def _collect_from(
    node: object,
    scope: _Scope,
    directory: TableDirectory,
    join_refs: list[tuple[ast.ColumnRef, str]],
    cte_names: frozenset[str] = frozenset(),
) -> None:
    """Preenche `scope` com as tabelas do `FROM`; junta as colunas do `ON`/`USING` em `join_refs`
    (resolvidas depois, já com os dois lados do join no escopo).

    `cte_names`: nomes de CTE da mesma instrução — nunca são tratados como tabela desconhecida;
    o repasse direto (`_cte_passthrough`) já colocou a tabela real no escopo antes desta chamada,
    e a CTE que não é repasse fica de propósito sem tabela (não inventa).
    """
    if isinstance(node, ast.RangeVar) and node.relname:
        if node.schemaname is None and node.relname.lower() in cte_names:
            return
        scope.add_table(directory, node.schemaname, node.relname, _alias_of(node))
    elif isinstance(node, ast.JoinExpr):
        _collect_from(node.larg, scope, directory, join_refs, cte_names)
        _collect_from(node.rarg, scope, directory, join_refs, cte_names)
        for ref in _columns_in(node.quals):
            join_refs.append((ref, "join"))
        # `USING (col, …)`: a coluna existe nos dois lados; sem qualificador, resolvida como
        # qualquer coluna sem qualificação (entra pela busca normal, não precisa de ColumnRef).
    # `RangeSubselect` (subconsulta) e `RangeFunction` (função na posição de tabela): não dão
    # tabela nenhuma para o escopo — colunas que as qualificarem ficam sem candidata (não inventa).


# --------------------------------------------------------------------------- resolução


_TRIGGER_PSEUDO_VARS = frozenset(
    {
        "tg_op", "tg_name", "tg_when", "tg_level", "tg_relid", "tg_relname",
        "tg_table_name", "tg_table_schema", "tg_nargs", "tg_argv",
    }
)  # fmt: skip


def _resolve_field(
    ref: ast.ColumnRef,
    role: str,
    scope: _Scope,
    variables: Collection[str],
    fields: list[FieldUse],
    unresolved: list[str],
) -> None:
    qualifier, column = _column_parts(ref)
    known_variables = {v.lower() for v in variables} | _TRIGGER_PSEUDO_VARS
    if column == "*":
        if qualifier:
            tid = scope.alias_map.get(qualifier.lower())
            if tid is not None:
                fields.append(FieldUse(tid, "*", role, 1.0))
            elif qualifier.lower() not in known_variables:
                unresolved.append(f"{qualifier}.*")
        else:
            for tid in scope.tables:
                fields.append(FieldUse(tid, "*", role, 1.0))
        return
    if qualifier and qualifier.lower() in ("new", "old") and scope.trigger_tables:
        lowered_col = column.lower()
        confidence = (
            1.0 if len(scope.trigger_tables) == 1 else round(1 / len(scope.trigger_tables), 4)
        )
        for tid in scope.trigger_tables:
            fields.append(FieldUse(tid, lowered_col, role, confidence))
        return
    if qualifier:
        tid = scope.alias_map.get(qualifier.lower())
        if tid is not None:
            fields.append(FieldUse(tid, column.lower(), role, 1.0))
        elif qualifier.lower() not in known_variables:
            # Nem tabela/alias conhecido, nem variável: pode ser campo de `record` (`reg.campo`)
            # ou alvo mesmo sem candidata — nos dois casos não inventa.
            unresolved.append(f"{qualifier}.{column}")
        return
    lowered = column.lower()
    if lowered in known_variables:
        return  # variável do PL/pgSQL, não é coluna
    candidates = [t for t in scope.tables if lowered in scope.columns_of.get(t, frozenset())]
    if not candidates:
        unresolved.append(column)
        return
    confidence = 1.0 if len(candidates) == 1 else round(1 / len(candidates), 4)
    for tid in candidates:
        fields.append(FieldUse(tid, lowered, role, confidence))


def _bare_columns(names: Sequence[ast.Node], role: str) -> list[tuple[str, str]]:
    """`USING (col, …)`: os nomes já são literais (sem qualificador)."""
    return [(str(n.sval), role) for n in names if isinstance(n, ast.String) and n.sval]


# --------------------------------------------------------------------------- SELECT


def _analyze_select_body(
    stmt: ast.SelectStmt,
    directory: TableDirectory,
    variables: Collection[str],
    fields: list[FieldUse],
    unresolved: list[str],
    tables: list[str],
    trigger_tables: Sequence[str] = (),
) -> None:
    if stmt.op != pglast.enums.SetOperation.SETOP_NONE:
        for side in (stmt.larg, stmt.rarg):
            if isinstance(side, ast.SelectStmt):
                _analyze_select_body(
                    side, directory, variables, fields, unresolved, tables, trigger_tables
                )
        return

    scope = _Scope(trigger_tables=tuple(trigger_tables))
    cte_scope, cte_names = _cte_passthrough(stmt, directory)
    scope.alias_map.update(cte_scope.alias_map)
    scope.columns_of.update(cte_scope.columns_of)
    join_refs: list[tuple[ast.ColumnRef, str]] = []
    for item in stmt.fromClause or ():
        _collect_from(item, scope, directory, join_refs, cte_names)
    for item in stmt.fromClause or ():
        if not isinstance(item, ast.JoinExpr):
            continue
        for name, role in _bare_columns(item.usingClause or (), "join"):
            lowered = name.lower()
            candidates = [t for t in scope.tables if lowered in scope.columns_of.get(t, ())]
            confidence = 1.0 if len(candidates) == 1 else round(1 / max(len(candidates), 1), 4)
            for tid in candidates:
                fields.append(FieldUse(tid, lowered, role, confidence))
    for tid in scope.tables:
        if tid not in tables:
            tables.append(tid)
    for ref, role in join_refs:
        _resolve_field(ref, role, scope, variables, fields, unresolved)
    for rt in stmt.targetList or ():
        if isinstance(rt, ast.ResTarget):
            for ref in _columns_in(rt.val):
                _resolve_field(ref, "read", scope, variables, fields, unresolved)
    for ref in _columns_in(stmt.whereClause):
        _resolve_field(ref, "filter", scope, variables, fields, unresolved)
    for ref in _columns_in(stmt.havingClause):
        _resolve_field(ref, "filter", scope, variables, fields, unresolved)
    for expr in stmt.groupClause or ():
        for ref in _columns_in(expr):
            _resolve_field(ref, "sort", scope, variables, fields, unresolved)
    for sort_by in stmt.sortClause or ():
        target = sort_by.node if isinstance(sort_by, ast.SortBy) else sort_by
        for ref in _columns_in(target):
            _resolve_field(ref, "sort", scope, variables, fields, unresolved)


def _cte_star_passthrough_table(cte: ast.CommonTableExpr) -> ast.RangeVar | None:
    """O `RangeVar` de `WITH cte AS (SELECT * FROM tabela)`; `None` se não é esse repasse direto
    (qualquer outra coisa, inclusive `SELECT col FROM tabela`, não inventa de onde vêm as
    colunas)."""
    inner = cte.ctequery
    if not isinstance(inner, ast.SelectStmt) or inner.op != pglast.enums.SetOperation.SETOP_NONE:
        return None
    from_items = inner.fromClause or ()
    targets = inner.targetList or ()
    if len(from_items) != 1 or len(targets) != 1:
        return None
    rv, only_target = from_items[0], targets[0]
    if not isinstance(rv, ast.RangeVar) or not rv.relname:
        return None
    if not isinstance(only_target, ast.ResTarget) or not isinstance(only_target.val, ast.ColumnRef):
        return None
    return rv if _column_parts(only_target.val)[1] == "*" else None


def _cte_passthrough(
    stmt: ast.SelectStmt, directory: TableDirectory
) -> tuple[_Scope, frozenset[str]]:
    """`WITH cte AS (SELECT * FROM tabela)`: `cte` passa a apontar para `tabela`, no escopo
    devolvido. O nome de **toda** CTE desta instrução (repasse ou não) volta em `cte_names`: o
    `FROM`/`JOIN` nunca trata um nome de CTE como tabela desconhecida — a que não é repasse fica
    de propósito sem tabela nenhuma (não inventa de onde vêm as colunas)."""
    scope = _Scope()
    names: set[str] = set()
    with_clause = getattr(stmt, "withClause", None)
    for cte in getattr(with_clause, "ctes", None) or ():
        if not isinstance(cte, ast.CommonTableExpr) or not cte.ctename:
            continue
        names.add(cte.ctename.lower())
        rv = _cte_star_passthrough_table(cte)
        if rv is None or not rv.relname:
            continue
        resolved_schema, _rule = directory.resolve(rv.relname, rv.schemaname)
        tid = table_id(rv.relname, resolved_schema)
        info = directory.info(resolved_schema, rv.relname)
        columns = frozenset(c.lower() for c in info.columns) if info else frozenset()
        scope.add_known(tid, cte.ctename, columns)
    return scope, frozenset(names)


# --------------------------------------------------------------------------- INSERT/UPDATE/DELETE


def _analyze_insert(
    stmt: ast.InsertStmt,
    directory: TableDirectory,
    variables: Collection[str],
    trigger_tables: Sequence[str] = (),
) -> tuple[list[str], list[FieldUse], list[str]]:
    fields: list[FieldUse] = []
    unresolved: list[str] = []
    tables: list[str] = []
    if stmt.relation is None or not stmt.relation.relname:
        return tables, fields, unresolved
    scope = _Scope(trigger_tables=tuple(trigger_tables))
    target_tid = scope.add_table(directory, stmt.relation.schemaname, stmt.relation.relname, None)
    tables.append(target_tid)
    cols = [c for c in stmt.cols or () if isinstance(c, ast.ResTarget) and c.name]
    if cols:
        for col in cols:
            fields.append(FieldUse(target_tid, str(col.name).lower(), "write", 1.0))
    else:
        fields.append(FieldUse(target_tid, "*", "write", 1.0))
    if isinstance(stmt.selectStmt, ast.SelectStmt):
        _analyze_select_body(
            stmt.selectStmt, directory, variables, fields, unresolved, tables, trigger_tables
        )
    if isinstance(stmt.onConflictClause, ast.OnConflictClause):
        for rt in stmt.onConflictClause.targetList or ():
            if isinstance(rt, ast.ResTarget) and rt.name:
                fields.append(FieldUse(target_tid, str(rt.name).lower(), "write", 1.0))
            if isinstance(rt, ast.ResTarget):
                for ref in _columns_in(rt.val):
                    _resolve_field(ref, "read", scope, variables, fields, unresolved)
    for ref in _columns_in(getattr(stmt, "returningList", None)):
        _resolve_field(ref, "read", scope, variables, fields, unresolved)
    return tables, fields, unresolved


def _analyze_update(
    stmt: ast.UpdateStmt,
    directory: TableDirectory,
    variables: Collection[str],
    trigger_tables: Sequence[str] = (),
) -> tuple[list[str], list[FieldUse], list[str]]:
    fields: list[FieldUse] = []
    unresolved: list[str] = []
    tables: list[str] = []
    if stmt.relation is None or not stmt.relation.relname:
        return tables, fields, unresolved
    scope = _Scope(trigger_tables=tuple(trigger_tables))
    target_tid = scope.add_table(
        directory, stmt.relation.schemaname, stmt.relation.relname, _alias_of(stmt.relation)
    )
    tables.append(target_tid)
    join_refs: list[tuple[ast.ColumnRef, str]] = []
    for item in stmt.fromClause or ():
        _collect_from(item, scope, directory, join_refs)
    for tid in scope.tables:
        if tid not in tables:
            tables.append(tid)
    for rt in stmt.targetList or ():
        if not isinstance(rt, ast.ResTarget):
            continue
        if rt.name:
            fields.append(FieldUse(target_tid, str(rt.name).lower(), "write", 1.0))
        for ref in _columns_in(rt.val):
            _resolve_field(ref, "read", scope, variables, fields, unresolved)
    for ref in _columns_in(stmt.whereClause):
        _resolve_field(ref, "filter", scope, variables, fields, unresolved)
    for ref, role in join_refs:
        _resolve_field(ref, role, scope, variables, fields, unresolved)
    return tables, fields, unresolved


def _analyze_delete(
    stmt: ast.DeleteStmt,
    directory: TableDirectory,
    variables: Collection[str],
    trigger_tables: Sequence[str] = (),
) -> tuple[list[str], list[FieldUse], list[str]]:
    fields: list[FieldUse] = []
    unresolved: list[str] = []
    tables: list[str] = []
    if stmt.relation is None or not stmt.relation.relname:
        return tables, fields, unresolved
    scope = _Scope(trigger_tables=tuple(trigger_tables))
    target_tid = scope.add_table(
        directory, stmt.relation.schemaname, stmt.relation.relname, _alias_of(stmt.relation)
    )
    tables.append(target_tid)
    join_refs: list[tuple[ast.ColumnRef, str]] = []
    for item in stmt.usingClause or ():
        _collect_from(item, scope, directory, join_refs)
    for tid in scope.tables:
        if tid not in tables:
            tables.append(tid)
    for ref in _columns_in(stmt.whereClause):
        _resolve_field(ref, "filter", scope, variables, fields, unresolved)
    for ref, role in join_refs:
        _resolve_field(ref, role, scope, variables, fields, unresolved)
    return tables, fields, unresolved


# --------------------------------------------------------------------------- entrada


_KIND_OF: dict[type, str] = {
    ast.SelectStmt: "select",
    ast.InsertStmt: "insert",
    ast.UpdateStmt: "update",
    ast.DeleteStmt: "delete",
}


def analyze_statement(
    sql: str,
    *,
    directory: TableDirectory,
    variables: Collection[str] = (),
    trigger_tables: Sequence[str] = (),
    kind_hint: str | None = None,
) -> StatementFacts:
    """As tabelas e os campos (com o papel) de uma instrução SQL só, lida como texto.

    `trigger_tables`: tabelas candidatas para `NEW`/`OLD` (função de trigger; mais de uma tabela
    usa a mesma função → todas, com `1/n`). Falha do `pglast` cai no mesmo fallback por regex do
    schema (`sql_schema.regex_refs`): só tabelas, sem colunas, `source="SQL_REGEX"` e
    confiança 0,5; sem nenhuma tabela achada nem por regex, `kind="dynamic"` (texto incompleto,
    SQL dinâmico já sem placeholder…).
    """
    text = _normalize_text(sql)
    try:
        stmts = pglast.parse_sql(sql)
    except pglast.parser.ParseError:
        return _regex_fallback(sql, text, directory)
    stmt = next((s.stmt for s in stmts if type(s.stmt) in _KIND_OF), None)
    if stmt is None:
        return StatementFacts(kind_hint or "dynamic", text, (), (), ())
    kind = _KIND_OF[type(stmt)]

    if isinstance(stmt, ast.SelectStmt):
        fields: list[FieldUse] = []
        unresolved: list[str] = []
        tables: list[str] = []
        _analyze_select_body(stmt, directory, variables, fields, unresolved, tables, trigger_tables)
    elif isinstance(stmt, ast.InsertStmt):
        tables, fields, unresolved = _analyze_insert(stmt, directory, variables, trigger_tables)
    elif isinstance(stmt, ast.UpdateStmt):
        tables, fields, unresolved = _analyze_update(stmt, directory, variables, trigger_tables)
    elif isinstance(stmt, ast.DeleteStmt):
        tables, fields, unresolved = _analyze_delete(stmt, directory, variables, trigger_tables)
    else:
        return StatementFacts(kind_hint or "dynamic", text, (), (), ())

    return StatementFacts(kind, text, tuple(tables), tuple(fields), tuple(unresolved))


def _regex_fallback(sql: str, text: str, directory: TableDirectory) -> StatementFacts:
    tables: list[str] = []
    for ref in regex_refs(sql):
        if isinstance(ref, TableOp):
            resolved_schema, _rule = directory.resolve(ref.name, ref.schema)
            tid = table_id(ref.name, resolved_schema)
            if tid not in tables:
                tables.append(tid)
    if not tables:
        return StatementFacts("dynamic", text, (), (), ())
    return StatementFacts("parse_fallback", text, tuple(tables), (), (), "SQL_REGEX", 0.5)


# --------------------------------------------------------------------------- separar instruções


@dataclass(frozen=True, slots=True)
class RawStatement:
    text: str  # pronto para `analyze_statement`; vazio quando `dynamic`
    line: int  # linha absoluta no `ddl` (1 = CREATE, como `routine_sources`)
    dynamic: bool = False  # `EXECUTE`: não dá para resolver tabela montada em tempo de execução


def statements_of(ddl: str, is_trigger: bool = False) -> tuple[list[RawStatement], frozenset[str]]:
    """Cada instrução/expressão SQL do corpo de uma função (texto, como `pg_get_functiondef`
    devolve), pronta para `analyze_statement`, e os nomes de variável declarados **segundo o
    próprio parser PL/pgSQL** (parâmetros, `DECLARE`, variável de `FOR` sobre linhas e campo de
    `record` — `PLpgSQL_var`/`PLpgSQL_rec`/`PLpgSQL_recfield` do `datums`): mais completo que
    `RoutineProfile.declared` (que só lê o texto do `DECLARE`), porque o `FOR rec IN
    SELECT …` não passa por lá. Reaproveita o mesmo parser (`pglast.parse_plpgsql`) e a mesma
    reescrita de função de trigger do carregamento do schema — nunca duplica: se o corpo não
    entra nesse parser (a mesma falha conhecida das `RETURNS trigger`), cai no mesmo fallback por
    instrução que `sql_schema._body_statement_refs` usa (sem variáveis: usa as de `declared`)."""
    body_range = _function_body(ddl)
    if body_range is None:
        return [], frozenset()
    body_start, body_end = body_range
    body = ddl[body_start:body_end]
    body_line = 1 + ddl.count("\n", 0, body_start)
    source = (_trigger_as_record(ddl, body) if is_trigger else None) or ddl
    try:
        tree = pglast.parse_plpgsql(source)
    except Exception:  # noqa: BLE001 - JSON malformado do libpg_query
        return list(_fallback_statements(body, body_line)), frozenset()
    results: list[RawStatement] = []
    for item in _plpgsql_expressions(tree):
        line = body_line + (item.lineno - 1 if item.lineno else 0)
        if item.dynamic:
            results.append(RawStatement("", line, dynamic=True))
            continue
        text = _prepared_text(item.query, item.field in _STATEMENT_FIELDS)
        if text:
            results.append(RawStatement(text, line))
    return results, _datum_names(tree)


_DATUM_KEYS = ("PLpgSQL_var", "PLpgSQL_rec", "PLpgSQL_recfield", "PLpgSQL_row")
_DATUM_NAME_FIELD = {
    "PLpgSQL_var": "refname",
    "PLpgSQL_rec": "refname",
    "PLpgSQL_row": "refname",
    "PLpgSQL_recfield": "fieldname",
}


def _datum_names(tree: object) -> frozenset[str]:
    """Nomes de todas as variáveis que o parser PL/pgSQL conhece (`datums` do `PLpgSQL_function`,
    inclusive as implícitas: `found`, a variável de `FOR` sobre linhas, campos de `record`)."""
    names: set[str] = set()
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            for key, value in node.items():
                if key in _DATUM_KEYS and isinstance(value, dict):
                    name = value.get(_DATUM_NAME_FIELD[key])
                    if isinstance(name, str) and name:
                        names.add(name.lower())
                stack.append(value)
    return frozenset(names)


def _prepared_text(query: str, statement: bool) -> str | None:
    """O texto pronto para `pglast.parse_sql`: instrução completa como está; expressão (`cond`,
    atribuição…) ganha `SELECT` na frente quando tem algo que vale a pena buscar."""
    text = query.strip()
    if not text:
        return None
    if statement:
        return text
    assignment = _ASSIGNMENT.match(text)
    if assignment:
        text = assignment.group(1)
    if not _MAY_REFERENCE.search(text):
        return None
    return text if _SQL_START.match(text) else f"SELECT {text}"


def _fallback_statements(body: str, body_line: int) -> Iterator[RawStatement]:
    for start, stop in split_statements(body):
        piece = body[start:stop]
        match = _SQL_START.search(piece)
        if match is None:
            continue
        line = body_line + body.count("\n", 0, start + match.start())
        yield RawStatement(piece[match.start() :], line)
