"""Leitor de schema SQL: `pg_dump --schema-only` ou os incrementais `db/v2.3.*`.

Cria `Table` e `PgFunction`, as arestas `TRIGGERS` e, a partir do corpo das funções,
`QUERIES`/`INSERTS`/`UPDATES`/`DELETES` e `CALLS_RPC`. Uma instrução com erro vira
pendência `sql_parse_error` e o carregamento continua.

As tabelas criadas entram num `TableDirectory`, e as referências sem
schema (corpo das funções, tabela do trigger) são qualificadas pelo `search_path` só no fim do
carregamento, com o diretório completo (a tabela pode ser criada num arquivo posterior). Com
`load_schema_snapshot`, o schema vem do `pg_catalog` (`ingest/schema_db.py`): as tabelas vão só
para o diretório e as funções e os triggers passam por este mesmo leitor.
"""

import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pglast
from pglast import ast, parser
from pglast.enums import FunctionParameterMode, ObjectType

from php_map.config import ECIDADE_SEARCH_PATH
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    pgfunction_id,
    rpccall_id,
    table_id,
)
from php_map.ingest.table_directory import RULES, TableDirectory, parse_search_path
from php_map.resolve.unresolved import UnresolvedWriter
from php_map.sqlutil import (
    FuncCallRef,
    SqlRef,
    TableOp,
    analyze_sql,
    fitting_overloads,
    is_sequence_name,
)

if TYPE_CHECKING:
    from php_map.ingest.schema_db import DbRoutine, SchemaSnapshot

SQL_REGEX_CONFIDENCE = 0.7
SNIPPET_MAX = 200
_VERSION_DIR = re.compile(r"^v\d+(?:\.\d+)+$")
_SIGNATURE_MODES = frozenset(
    {
        FunctionParameterMode.FUNC_PARAM_IN,
        FunctionParameterMode.FUNC_PARAM_INOUT,
        FunctionParameterMode.FUNC_PARAM_VARIADIC,
        FunctionParameterMode.FUNC_PARAM_DEFAULT,
    }
)
_OBJECT_NAME = re.compile(
    r"\b(?:table|function|view|index|trigger|sequence|type)\s+(?:if\s+(?:not\s+)?exists\s+)?"
    r"([\w.\"]+)",
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(r"^\s*[\w.\[\]\"]+\s*:=\s*(.*)$", re.DOTALL)
_SQL_START = re.compile(r"\b(select|insert|update|delete|with)\b", re.IGNORECASE)
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_IDENT = r"([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)"
_REGEX_OPS = (
    (EdgeType.INSERTS, re.compile(rf"\binsert\s+into\s+{_IDENT}", re.IGNORECASE)),
    (EdgeType.UPDATES, re.compile(rf"\bupdate\s+(?:only\s+)?{_IDENT}", re.IGNORECASE)),
    (EdgeType.DELETES, re.compile(rf"\bdelete\s+from\s+(?:only\s+)?{_IDENT}", re.IGNORECASE)),
    (
        EdgeType.QUERIES,
        re.compile(rf"(?<!delete )\b(?:from|join)\s+{_IDENT}\b(?!\s*\()", re.IGNORECASE),
    ),
)
_REGEX_STOPWORDS = frozenset({"select", "lateral", "only", "unnest", "generate_series"})
_DYNAMIC_STMTS = frozenset({"PLpgSQL_stmt_dynexecute", "PLpgSQL_stmt_dynfors"})
# Funções de trigger (ver `_trigger_as_record`).
_RETURNS_TRIGGER = re.compile(r"returns\s+trigger\b", re.IGNORECASE)
_BODY_START = re.compile(r"\b(declare|begin)\b", re.IGNORECASE)
_TRIGGER_VARIABLES = (
    "new record; old record; tg_op text; tg_name text; tg_when text; tg_level text; "
    "tg_relid oid; tg_relname text; tg_table_name text; tg_table_schema text; "
    "tg_nargs int4; tg_argv text[];"
)
# Campos do JSON do PL/pgSQL que guardam uma instrução SQL completa (os demais são expressões).
_STATEMENT_FIELDS = frozenset({"sqlstmt", "query"})
# \' com nº par de barras antes: escape antigo de aspas (standard_conforming_strings=off).
_BACKSLASH_QUOTE = re.compile(r"(?<!\\)((?:\\\\)*)\\'")
# Funções do e-cidade que executam DDL passado como string (`select fc_executa_ddl('create …')`).
DDL_EXECUTORS = frozenset({"fc_executa_ddl"})
# Só estas instruções viram nó/aresta: o resto (INSERT de carga, GRANT…) nem é parseado.
_RELEVANT_START = re.compile(r"(?:create|drop)\b", re.IGNORECASE)
# Expressão PL/pgSQL sem chamada nem tabela não produz referência: não vale o parse.
_MAY_REFERENCE = re.compile(r"\(|\b(?:from|into|update|delete|join)\b", re.IGNORECASE)


@dataclass(slots=True)
class SchemaStats:
    files: int = 0
    statements: int = 0
    tables: int = 0
    functions: int = 0
    triggers: int = 0
    drops: int = 0
    parse_errors: int = 0
    plpgsql_fallbacks: int = 0
    regex_fallbacks: int = 0
    dynamic_sql: int = 0
    rpc_calls: int = 0
    ignored_statements: int = 0
    escaped_files: int = 0
    executed_ddl: int = 0
    sequences_dropped: int = 0
    # fonte (`files` ou `db:<host>/<base>`), `search_path`, tabelas conhecidas,
    # referências sem schema por regra e tabelas do banco que viraram nó (`materialized`).
    source: str = "files"
    search_path: list[str] = field(default_factory=list)
    directory_tables: int = 0
    table_refs: dict[str, int] = field(default_factory=lambda: dict.fromkeys(RULES, 0))
    materialized: int = 0
    # funções lidas do banco cujo texto não gerou nó `PgFunction` (parse falhou).
    routines_without_node: list[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class _Ref:
    ref: SqlRef
    line: int
    source: EdgeSource


@dataclass(slots=True)
class _Loader:
    builder: GraphBuilder
    unresolved: UnresolvedWriter
    stats: SchemaStats = field(default_factory=SchemaStats)
    created: set[str] = field(default_factory=set)
    functions: dict[str, set[str]] = field(default_factory=dict)  # nome → IDs (sobrecargas)
    function_schema: dict[str, str] = field(default_factory=dict)
    arity: dict[str, tuple[int, float]] = field(default_factory=dict)  # (mín., máx.) de args
    calls: dict[str, list[tuple[FuncCallRef, str, int]]] = field(default_factory=dict)
    # Tabelas do corpo, qualificadas no fim (`_resolve_table_refs`): (ref, arquivo, linha, fonte).
    table_refs: dict[str, list[tuple[TableOp, str, int, EdgeSource]]] = field(default_factory=dict)
    # (nome da tabela, schema ou None, função, arquivo, linha): tabela qualificada no fim.
    triggers: list[tuple[str, str | None, FuncCallRef, str, int]] = field(default_factory=list)
    directory: TableDirectory = field(default_factory=lambda: TableDirectory(ECIDADE_SEARCH_PATH))
    # IDs dos `PgFunction` na ordem em que foram criados (liga o texto ao nó).
    created_functions: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- arquivos


def expand_paths(paths: Sequence[Path]) -> list[tuple[Path, str]]:
    """`(arquivo, caminho exibido)` na ordem de carga.

    Arquivo → ele mesmo. Diretório com `v2.3.*` → incrementais por versão natural, e dentro da
    versão `pre` → `ddl` → `up/pre` → `up/ddl` → outros → `pos` (`down/` é ignorado).
    Outro diretório → todos os `*.sql` em ordem alfabética.
    """
    result: list[tuple[Path, str]] = []
    for path in paths:
        if path.is_file():
            result.append((path, path.name))
            continue
        versions = [d for d in path.iterdir() if d.is_dir() and _VERSION_DIR.match(d.name)]
        if versions:
            for version in sorted(versions, key=_version_key):
                files = [
                    f for f in version.rglob("*.sql") if "down" not in f.relative_to(version).parts
                ]
                ranked = sorted((_incremental_rank(f, version), f) for f in files)
                result.extend((file, file.relative_to(path).as_posix()) for _rank, file in ranked)
        else:
            for file in sorted(path.rglob("*.sql"), key=lambda f: f.relative_to(path).as_posix()):
                result.append((file, file.relative_to(path).as_posix()))
    return result


def _version_key(directory: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in directory.name[1:].split("."))


def _incremental_rank(file: Path, version: Path) -> tuple[int, str]:
    relative = file.relative_to(version)
    in_up = relative.parts[0] == "up" and len(relative.parts) > 1
    name = file.name.lower()
    if name.startswith("pos"):
        rank = 6
    elif name.startswith("pre"):
        rank = 2 if in_up else 0
    elif name.startswith("ddl"):
        rank = 3 if in_up else 1
    else:
        rank = 4 if in_up else 5
    return rank, relative.as_posix()


def normalize_escapes(text: str) -> str:
    """`\\'` → `''`: os incrementais usam o escape antigo do PostgreSQL, que o pglast (modo
    `standard_conforming_strings=on`) não entende. Sem isso, até a divisão em instruções erra."""
    return _BACKSLASH_QUOTE.sub(lambda m: m.group(1) + "''", text)


def read_sql(path: Path) -> str:
    """UTF-8 e, se falhar, latin-1 (os incrementais são UTF-8 com CRLF)."""
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return text.replace("\r\n", "\n")


# --------------------------------------------------------------------------- divisão


def split_statements(text: str) -> list[tuple[int, int]]:
    """Intervalos `(início, fim)` de cada instrução. Usa o scanner do pglast; se ele falhar,
    divide por `;` fora de strings, identificadores entre aspas, comentários e `$tag$…$tag$`."""
    try:
        return [(s.start, s.stop) for s in parser.split(text, with_parser=False, only_slices=True)]
    except Exception:  # noqa: BLE001 - scanner do libpg_query: qualquer falha cai no fallback
        return _manual_split(text)


def _manual_split(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = i = 0
    size = len(text)
    while i < size:
        ch = text[i]
        if ch in "'\"":
            end = text.find(ch, i + 1)
            while end != -1 and text[end + 1 : end + 2] == ch:  # '' escapado
                end = text.find(ch, end + 2)
            i = size if end == -1 else end + 1
        elif text.startswith("--", i):
            end = text.find("\n", i)
            i = size if end == -1 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = size if end == -1 else end + 2
        elif ch == "$" and (match := re.match(r"\$[A-Za-z_]*\$", text[i:])):
            tag = match.group(0)
            end = text.find(tag, i + len(tag))
            i = size if end == -1 else end + len(tag)
        elif ch == ";":
            if text[start:i].strip():
                spans.append((start, i))
            start = i = i + 1
        else:
            i += 1
    if text[start:].strip():
        spans.append((start, size))
    return spans


def first_code_offset(text: str) -> int:
    """Posição do primeiro caractere que não é espaço nem comentário."""
    i = 0
    while i < len(text):
        if text[i].isspace():
            i += 1
        elif text.startswith("--", i):
            end = text.find("\n", i)
            i = len(text) if end == -1 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end == -1 else end + 2
        else:
            return i
    return i


# --------------------------------------------------------------------------- carga


def load_schema(
    paths: Sequence[Path],
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    directory: TableDirectory | None = None,
) -> SchemaStats:
    """DDL de arquivos. `directory` (opcional) recebe as tabelas criadas e dá o `search_path`
    das referências sem schema; sem ele, vale o `search_path` de uma instalação padrão do
    e-Cidade."""
    started = time.perf_counter()
    loader = _Loader(builder, unresolved)
    if directory is not None:
        loader.directory = directory
    for path, display in expand_paths(paths):
        loader.stats.files += 1
        text = read_sql(path)
        if "\\'" in text:
            loader.stats.escaped_files += 1
            text = normalize_escapes(text)
        _load_file(loader, text, display)
    _finish(loader)
    loader.stats.seconds = round(time.perf_counter() - started, 2)
    return loader.stats


def load_schema_snapshot(
    snapshot: "SchemaSnapshot",
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    directory: TableDirectory | None = None,
    routines: "list[tuple[str, DbRoutine]] | None" = None,
) -> SchemaStats:
    """Schema lido do banco (`schema_db.read_schema_db`).

    As tabelas vão só para o diretório (sem nó: o pipeline materializa as referenciadas); as
    funções e os triggers (`pg_get_functiondef`/`pg_get_triggerdef`) passam por `_load_file`,
    com `db:<schema>.<nome>` como arquivo da evidência.

    `routines` recebe `(ID do nó, função)` de cada função cujo texto gerou um nó,
    com o texto **original** (antes de `normalize_escapes`). É por aqui, e não pelo cabeçalho do
    texto, que as sobrecargas (`lpad(int4,int4)` e `lpad(varchar,int4,int4)`) ficam com o texto
    certo: o ID normaliza os tipos.
    """
    started = time.perf_counter()
    loader = _Loader(builder, unresolved)
    loader.directory = directory if directory is not None else TableDirectory()
    loader.directory.search_path = parse_search_path(snapshot.search_path)
    loader.directory.builtin_functions = snapshot.builtin_functions
    loader.stats.source = snapshot.source
    for table in snapshot.tables:
        loader.directory.add(table.schema, table.name, table.kind, table.columns)
    function_count = len(snapshot.functions)
    for index, routine in enumerate((*snapshot.functions, *snapshot.triggers)):
        text = routine.ddl.rstrip().rstrip(";") + ";\n"  # o pg_get_*def não termina em `;`
        if "\\'" in text:
            loader.stats.escaped_files += 1
            text = normalize_escapes(text)
        before = len(loader.created_functions)
        _load_file(loader, text, f"db:{routine.schema}.{routine.name}")
        if index >= function_count:
            continue  # trigger: o texto não vai para `routine_sources`
        created = loader.created_functions[before:]
        if created and routines is not None:
            routines.append((created[0], routine))
        elif not created:
            loader.stats.routines_without_node.append(f"{routine.schema}.{routine.name}")
    _finish(loader)
    loader.stats.seconds = round(time.perf_counter() - started, 2)
    return loader.stats


def _finish(loader: _Loader) -> None:
    """Resolve o que depende do carregamento inteiro: triggers, tabelas e RPCs."""
    _resolve_triggers(loader)
    _resolve_table_refs(loader)
    _resolve_rpc_calls(loader)
    stats, directory = loader.stats, loader.directory
    stats.search_path = list(directory.search_path)
    stats.directory_tables = len(directory)
    stats.table_refs = dict(directory.counts)


def _load_file(loader: _Loader, text: str, display: str, first_line: int = 1) -> None:
    """Carrega um texto SQL. `first_line` desloca as linhas (DDL dentro de `fc_executa_ddl`)."""
    counted_until, counted_line = 0, first_line  # contagem incremental de linhas
    for start, stop in split_statements(text):
        chunk = text[start:stop]
        code = first_code_offset(chunk)
        if code >= len(chunk):
            continue  # só comentários
        counted_line += text.count("\n", counted_until, start)
        counted_until = start
        base_line = counted_line
        line = base_line + chunk.count("\n", 0, code)
        loader.stats.statements += 1
        if not _is_relevant(chunk[code:]):
            loader.stats.ignored_statements += 1
            continue
        try:
            raws = pglast.parse_sql(chunk)
        except pglast.parser.ParseError as exc:
            loader.stats.parse_errors += 1
            snippet = " ".join(chunk[code:].split())[:SNIPPET_MAX]
            match = _OBJECT_NAME.search(chunk[code:])
            hint = match.group(1).strip('"').lower() if match else None
            loader.unresolved.add("sql_parse_error", display, line, f"{snippet} [{exc}]", hint=hint)
            continue
        for raw in raws:
            if raw.stmt is not None:
                _statement(loader, raw.stmt, chunk, display, line, base_line)


def _is_relevant(code: str) -> bool:
    if _RELEVANT_START.match(code):
        return True
    head = code[:400].lower()
    return head.startswith("select") and any(name in head for name in DDL_EXECUTORS)


def _statement(
    loader: _Loader, stmt: ast.Node, chunk: str, display: str, line: int, base_line: int
) -> None:
    if isinstance(stmt, ast.CreateStmt):
        _create_table(loader, stmt, display, line)
    elif isinstance(stmt, ast.CreateFunctionStmt):
        _create_function(loader, stmt, chunk, display, line, base_line)
    elif isinstance(stmt, ast.CreateTrigStmt):
        _create_trigger(loader, stmt, display, line)
    elif isinstance(stmt, ast.DropStmt):
        _drop(loader, stmt)
    elif (inner := executed_ddl(stmt)) is not None:
        # DDL dentro de string: carrega como um arquivo (dividido e filtrado; há strings com
        # um CREATE seguido de 10 mil INSERTs, lentas demais para parsear de uma vez).
        loader.stats.executed_ddl += 1
        _load_file(loader, inner, display, first_line=line)
    else:
        loader.stats.ignored_statements += 1  # SET, COMMENT, GRANT, ALTER, DML de carga…


def executed_ddl(stmt: ast.Node) -> str | None:
    """Texto de `select fc_executa_ddl('<ddl>')`, senão `None`."""
    if not isinstance(stmt, ast.SelectStmt) or not stmt.targetList or len(stmt.targetList) != 1:
        return None
    target = stmt.targetList[0]
    call = target.val if isinstance(target, ast.ResTarget) else None
    if not isinstance(call, ast.FuncCall) or not call.args or not call.funcname:
        return None
    _schema, name = _function_name(call.funcname)
    first = call.args[0]
    if name not in DDL_EXECUTORS or not isinstance(first, ast.A_Const):
        return None
    value = getattr(first.val, "sval", None)
    return value if isinstance(value, str) else None


def _create_table(loader: _Loader, stmt: ast.CreateStmt, display: str, line: int) -> None:
    relation = stmt.relation
    if relation is None or not relation.relname:
        return
    schema = (relation.schemaname or "public").lower()
    name = relation.relname.lower()
    node_id = table_id(name, schema)
    columns = [
        element.colname
        for element in stmt.tableElts or ()
        if isinstance(element, ast.ColumnDef) and element.colname
    ]
    loader.builder.add_node(
        Node(
            node_id,
            NodeLabel.TABLE,
            {
                "name": name,
                "schema": schema,
                "columns": columns,
                "source_file": display,
                "line": line,
                "is_infra": name.startswith("db_acount"),
            },
        )
    )
    loader.created.add(node_id)
    loader.directory.add(schema, name, "table", columns)
    loader.stats.tables += 1


def type_name(type_: ast.TypeName | None) -> str:
    """Nome do tipo como o pglast entrega, sem `pg_catalog.`, em minúsculas (`int4`, `varchar`)."""
    if type_ is None or not type_.names:
        return "unknown"
    parts = [p.sval.lower() for p in type_.names if isinstance(p, ast.String) and p.sval]
    name = parts[-1] if parts else "unknown"
    if len(parts) > 1 and parts[0] != "pg_catalog":
        name = ".".join(parts)
    return name + "[]" * len(type_.arrayBounds or ())


def _function_name(names: Sequence[ast.Node] | None) -> tuple[str | None, str]:
    parts = [p.sval.lower() for p in names or () if isinstance(p, ast.String) and p.sval]
    return (parts[-2] if len(parts) > 1 else None), (parts[-1] if parts else "")


def _create_function(
    loader: _Loader,
    stmt: ast.CreateFunctionStmt,
    chunk: str,
    display: str,
    line: int,
    base_line: int,
) -> None:
    schema, name = _function_name(stmt.funcname)
    params = [
        p
        for p in stmt.parameters or ()
        if isinstance(p, ast.FunctionParameter) and p.mode in _SIGNATURE_MODES
    ]
    argtypes = [type_name(p.argType) for p in params]
    required = sum(1 for p in params if p.defexpr is None)
    variadic = any(p.mode == FunctionParameterMode.FUNC_PARAM_VARIADIC for p in params)
    node_id = pgfunction_id(schema, name, argtypes)
    language, body = "sql", None
    for option in stmt.options or ():
        if not isinstance(option, ast.DefElem):
            continue
        if option.defname == "language" and isinstance(option.arg, ast.String):
            language = (option.arg.sval or "sql").lower()
        elif option.defname == "as":
            values = option.arg if isinstance(option.arg, tuple) else (option.arg,)
            if values and isinstance(values[0], ast.String):
                body = values[0].sval
    returns = None
    if stmt.returnType is not None:
        returns = ("setof " if stmt.returnType.setof else "") + type_name(stmt.returnType)

    if node_id in loader.created:  # CREATE OR REPLACE (ou redefinição) da mesma assinatura
        loader.builder.remove_edges_from(node_id)
    loader.calls[node_id] = []
    loader.table_refs[node_id] = []
    loader.builder.add_node(
        Node(
            node_id,
            NodeLabel.PG_FUNCTION,
            {
                "name": name,
                "schema": schema or "public",
                "args": argtypes,
                "language": language,
                "returns": returns,
                # Aridade: o builder desempata sobrecargas pelo builder (max None = VARIADIC).
                "min_args": required,
                "max_args": None if variadic else len(params),
                "source_file": display,
                "line": line,
            },
        ),
        replace=True,
    )
    loader.created.add(node_id)
    loader.created_functions.append(node_id)
    loader.functions.setdefault(name, set()).add(node_id)
    loader.function_schema[node_id] = schema or "public"
    loader.arity[node_id] = (required, float("inf") if variadic else len(params))
    loader.stats.functions += 1

    if body is None:
        return
    offset = chunk.find(body)
    body_line = base_line + chunk.count("\n", 0, offset) if offset >= 0 else line
    if language == "plpgsql":
        refs = _plpgsql_refs(
            loader, chunk, body, body_line, node_id, display, is_trigger=returns == "trigger"
        )
    elif language == "sql":
        refs = _body_statement_refs(loader, body, body_line)
    else:
        refs = []
    for item in refs:
        _add_ref(loader, node_id, item, display)


def _add_ref(loader: _Loader, node_id: str, item: _Ref, display: str) -> None:
    ref = item.ref
    if isinstance(ref, TableOp):
        if is_sequence_name(ref.name):  # `select last_value from x_seq` não é tabela
            loader.stats.sequences_dropped += 1
            return
        # Qualificada no fim (`_resolve_table_refs`): a tabela pode vir num arquivo posterior.
        loader.table_refs.setdefault(node_id, []).append((ref, display, item.line, item.source))
    else:
        loader.calls[node_id].append((ref, display, item.line))


# --------------------------------------------------------------------------- PL/pgSQL


def _trigger_as_record(chunk: str, body: str) -> str | None:
    """Reescreve uma função `RETURNS trigger` para o `parse_plpgsql` aceitar.

    O libpg_query gera JSON inválido para as variáveis `NEW`/`OLD` de funções de trigger.
    Trocamos `RETURNS trigger` por `RETURNS record` e declaramos as variáveis de trigger no
    `DECLARE`, sem acrescentar quebras de linha (os números de linha continuam valendo).
    """
    offset = chunk.find(body)
    if offset < 0:
        return None
    head = _RETURNS_TRIGGER.sub("returns record", chunk[:offset], count=1)
    match = _BODY_START.search(body)
    if match is None:
        return None
    injected = f"declare {_TRIGGER_VARIABLES} "
    if match.group(1).lower() == "declare":
        new_body = body[: match.start()] + injected + body[match.end() :]
    else:
        new_body = body[: match.start()] + injected + body[match.start() :]
    return head + new_body + chunk[offset + len(body) :]


def _plpgsql_refs(
    loader: _Loader,
    chunk: str,
    body: str,
    body_line: int,
    node_id: str,
    display: str,
    is_trigger: bool = False,
) -> list[_Ref]:
    source = _trigger_as_record(chunk, body) if is_trigger else chunk
    try:
        tree = pglast.parse_plpgsql(source or chunk)
    except Exception:  # noqa: BLE001 - JSON malformado do libpg_query ou reescrita que falhou
        loader.stats.plpgsql_fallbacks += 1
        return _body_statement_refs(loader, body, body_line)
    refs: list[_Ref] = []
    for item in _plpgsql_expressions(tree):
        line = body_line + (item.lineno - 1 if item.lineno else 0)
        if item.dynamic:
            loader.stats.dynamic_sql += 1
            loader.unresolved.add(
                "dynamic_sql", display, line, f"EXECUTE em {node_id}", hint=node_id
            )
            continue
        found = _expression_refs(item.query, item.field in _STATEMENT_FIELDS)
        refs.extend(_Ref(ref, line, EdgeSource.SQL_AST) for ref in found or ())
    return refs


@dataclass(frozen=True, slots=True)
class _PlExpr:
    query: str
    lineno: int | None
    field: str  # campo do JSON que contém a expressão (sqlstmt, query, cond, expr…)
    dynamic: bool = False


def _plpgsql_expressions(tree: Any) -> Iterator[_PlExpr]:
    """Cada `PLpgSQL_expr` do JSON do `parse_plpgsql`, com a linha e o campo de origem."""
    stack: list[tuple[Any, int | None, str]] = [(tree, None, "")]
    while stack:
        node, lineno, field = stack.pop()
        if isinstance(node, list):
            stack.extend((item, lineno, field) for item in node)
        elif isinstance(node, dict):
            lineno = node.get("lineno", lineno)
            for key, value in node.items():
                if key == "PLpgSQL_expr" and isinstance(value, dict) and "query" in value:
                    yield _PlExpr(value["query"], lineno, field)
                    continue
                is_dynamic = key in _DYNAMIC_STMTS or (
                    key == "PLpgSQL_stmt_open" and isinstance(value, dict) and "dynquery" in value
                )
                if is_dynamic and isinstance(value, dict):
                    yield _PlExpr("", value.get("lineno", lineno), key, dynamic=True)
                stack.append((value, lineno, key))


def _expression_refs(query: str, statement: bool) -> list[SqlRef] | None:
    """Parseia uma expressão PL/pgSQL.

    `statement=True` (campos `sqlstmt`/`query`): instrução completa (`select`, `prepare`,
    `truncate`, `create temp table … as`…). Senão é expressão (`cond`, `expr`, atribuição) e
    vira `SELECT <expr>`; sem parêntese nem palavra de tabela, nem é parseada.
    """
    text = query.strip()
    if not text:
        return []
    if statement:
        candidates: tuple[str, ...] = (text, f"SELECT {text}")
    else:
        assignment = _ASSIGNMENT.match(text)
        if assignment:
            text = assignment.group(1)
        if not _MAY_REFERENCE.search(text):
            return []
        # PERFORM chega como `expr` já com `SELECT …`.
        candidates = (text,) if _SQL_START.match(text) else (f"SELECT {text}",)
    for candidate in candidates:
        try:
            return analyze_sql(candidate)
        except pglast.parser.ParseError:
            continue
    return None


def _body_statement_refs(loader: _Loader, body: str, body_line: int) -> list[_Ref]:
    """Fallback: cada instrução SQL do corpo, a partir da 1ª palavra-chave SQL.

    Parseou → `SQL_AST` (é o texto exato do código); não parseou → regex `SQL_REGEX` 0.7.
    """
    clean = _COMMENTS.sub(lambda m: "\n" * m.group(0).count("\n"), body)
    refs: list[_Ref] = []
    for start, stop in split_statements(clean):
        piece = clean[start:stop]
        match = _SQL_START.search(piece)
        if match is None:
            continue
        line = body_line + clean.count("\n", 0, start + match.start())
        sql = piece[match.start() :]
        try:
            found = analyze_sql(sql)
        except pglast.parser.ParseError:
            loader.stats.regex_fallbacks += 1
            refs.extend(_Ref(ref, line, EdgeSource.SQL_REGEX) for ref in regex_refs(sql))
            continue
        refs.extend(_Ref(ref, line, EdgeSource.SQL_AST) for ref in found)
    return refs


def regex_refs(sql: str) -> list[SqlRef]:
    """Último recurso: tabelas por regex (`from|join|into|update|delete from`)."""
    refs: list[SqlRef] = []
    for op, pattern in _REGEX_OPS:
        for match in pattern.finditer(sql):
            name = match.group(1).lower()
            if name.split(".")[-1] in _REGEX_STOPWORDS:
                continue
            schema, _, table = name.rpartition(".")
            refs.append(TableOp(op, table, schema or None))
    return list(dict.fromkeys(refs))


# --------------------------------------------------------------------------- triggers, DROP, RPC


def _create_trigger(loader: _Loader, stmt: ast.CreateTrigStmt, display: str, line: int) -> None:
    relation = stmt.relation
    if relation is None or not relation.relname:
        return
    schema, name = _function_name(stmt.funcname)
    table_schema = relation.schemaname.lower() if relation.schemaname else None
    loader.triggers.append(
        (relation.relname.lower(), table_schema, FuncCallRef(name, schema), display, line)
    )
    loader.stats.triggers += 1


def _drop(loader: _Loader, stmt: ast.DropStmt) -> None:
    for obj in stmt.objects or ():
        node_id = None
        if stmt.removeType == ObjectType.OBJECT_TABLE and isinstance(obj, tuple):
            schema, name = _function_name(obj)
            node_id = table_id(name, schema)
            if node_id in loader.created:
                loader.directory.discard(schema or "public", name)
        elif stmt.removeType == ObjectType.OBJECT_FUNCTION and isinstance(obj, ast.ObjectWithArgs):
            schema, name = _function_name(obj.objname)
            args = [type_name(t) for t in obj.objargs or () if isinstance(t, ast.TypeName)]
            node_id = pgfunction_id(schema, name, args)
            loader.functions.get(name, set()).discard(node_id)
            loader.calls.pop(node_id, None)
            loader.table_refs.pop(node_id, None)
        # Só remove o que foi criado neste carregamento (DROP de objeto desconhecido é ruído).
        if node_id is not None and node_id in loader.created:
            loader.builder.drop_node(node_id)
            loader.created.discard(node_id)
            loader.stats.drops += 1


def _candidates(loader: _Loader, ref: FuncCallRef) -> list[str]:
    """Sobrecargas com esse nome (e schema, se qualificado) que aceitam o nº de argumentos.

    Se nenhuma aceita (tipos/defaults que o parser não vê), fica com todas pelo nome.
    """
    if ref.schema is None and loader.directory.is_builtin(ref.name):
        return []  # nativa (`substr`, `lpad`): o `pg_catalog` vem antes
    ids = sorted(loader.functions.get(ref.name, ()))
    if ref.schema is not None:
        ids = [i for i in ids if loader.function_schema.get(i) == ref.schema]
    return fitting_overloads(ids, loader.arity, ref.nargs)


def _resolve_triggers(loader: _Loader) -> None:
    for name, schema, ref, display, line in loader.triggers:
        table = table_id(name, loader.directory.resolve(name, schema)[0])
        ids = _candidates(loader, ref)
        # Função de trigger não tem argumentos: com sobrecargas, fica a de assinatura vazia.
        empty = pgfunction_id(ref.schema, ref.name, [])
        target = ids[0] if len(ids) == 1 else empty
        loader.builder.add_edge(
            Edge(
                table,
                target,
                EdgeType.TRIGGERS,
                EdgeSource.SQL_AST,
                1.0,
                {"path": display, "line": line},
            )
        )


def _resolve_table_refs(loader: _Loader) -> None:
    """Arestas de tabela do corpo das funções, com o schema pelo `search_path`."""
    for node_id, refs in loader.table_refs.items():
        if node_id not in loader.created:
            continue
        for ref, display, line, source in refs:
            schema, _rule = loader.directory.resolve(ref.name, ref.schema)
            confidence = 1.0 if source == EdgeSource.SQL_AST else SQL_REGEX_CONFIDENCE
            loader.builder.add_edge(
                Edge(
                    node_id,
                    table_id(ref.name, schema),
                    ref.op,
                    source,
                    confidence,
                    {"path": display, "line": line},
                )
            )


def _resolve_rpc_calls(loader: _Loader) -> None:
    """`CALLS_RPC` do corpo das funções: sobrecarga única 1.0; várias `1/n`; `fc_*` fora do
    schema → `RpcCall`; demais (nativas: `now`, `coalesce`…) são ignoradas."""
    for node_id, calls in loader.calls.items():
        if node_id not in loader.created:
            continue
        for ref, display, line in calls:
            evidence = {"path": display, "line": line}
            ids = _candidates(loader, ref)
            if ids:
                for target in ids:
                    confidence = 1.0 / len(ids)
                    if len(ids) > 1:
                        evidence = {**evidence, "candidates": ids}
                    loader.builder.add_edge(
                        Edge(
                            node_id,
                            target,
                            EdgeType.CALLS_RPC,
                            EdgeSource.SQL_AST,
                            confidence,
                            evidence,
                        )
                    )
                loader.stats.rpc_calls += 1
            elif ref.name.startswith("fc_"):
                loader.builder.add_edge(
                    Edge(
                        node_id,
                        rpccall_id(ref.name),
                        EdgeType.CALLS_RPC,
                        EdgeSource.SQL_AST,
                        1.0,
                        evidence,
                    )
                )
                loader.stats.rpc_calls += 1
