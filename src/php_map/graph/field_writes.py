"""Campos que o código PHP preenche antes de gravar pelo DAO.

O DAO legado (`classes/db_x_classe.php`) recebe os valores por dois caminhos: a **atribuição**
`$oDao->campo = expr` antes do `incluir`/`alterar` e o **`$_POST`**, que `atualizacampos()` lê
campo a campo quando a atribuição não preencheu. O mapa sabia que `cl_x::incluir` grava a tabela
(`INSERTS`/`UPDATES`), mas não **coluna por coluna**. Este módulo liga os dois.

Fluxo (mesmo molde da `routine_fields`):

1. o resolvedor de chamadas (`resolve/calls.py`) entrega uma `PendingWrite` por chamada
   `incluir`/`alterar` resolvida para um método de DAO, com as atribuições do extrator (`sets`);
   o `FieldWritesCollector` também guarda os `form_fields` de cada arquivo;
2. `build_field_writes` roda depois do estágio `sql` (as `INSERTS`/`UPDATES` do DAO já existem) e
   produz as linhas;
3. `store_field_writes` grava a tabela derivada `php_field_writes` no `.db`, sem nó nem aresta
   nova (a ontologia fica como está). A aresta repetida só guarda a 1ª ocorrência, por isso o
   valor não vai em atributo da `CALLS`.

O valor é o **texto do código** (`$iTpEmpresaSocio`, `32`), nunca o valor resolvido.
"""

import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from php_map.errors import StoreError
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import EdgeType, NodeLabel, key_of
from php_map.ingest.records import FieldSet, FormField, PhpFileRecord

FIELD_WRITES_TABLE = "php_field_writes"
HOW_ASSIGN = "atribuicao"
HOW_FORM = "formulario"
KIND_FORM = "formulario"
SOURCE = "PHP_AST"
# O formulário só vale quando a atribuição não preencheu o campo: a confiança cai.
FORM_FACTOR = 0.8
# `incluir` grava com `INSERTS`; `alterar`, com `UPDATES`.
_EDGE_OF_METHOD = {"incluir": EdgeType.INSERTS.value, "alterar": EdgeType.UPDATES.value}
_INCLUDE_TYPES = (EdgeType.INCLUDES, EdgeType.REQUIRES)
# (src, dao_method, table_id, column_name, how, path, line): a deduplicação das linhas
RowKey = tuple[str, str, str, str, str, str, int]

_SCHEMA = f"""
DROP TABLE IF EXISTS {FIELD_WRITES_TABLE};
CREATE TABLE {FIELD_WRITES_TABLE} (
    src TEXT NOT NULL,
    dao_method TEXT NOT NULL,
    table_id TEXT NOT NULL,
    column_name TEXT NOT NULL,
    how TEXT NOT NULL,
    value TEXT NOT NULL,
    value_kind TEXT NOT NULL,
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    call_line INTEGER NOT NULL,
    confidence REAL NOT NULL,
    source TEXT NOT NULL
);
CREATE INDEX idx_{FIELD_WRITES_TABLE}_table_column ON {FIELD_WRITES_TABLE}(table_id, column_name);
CREATE INDEX idx_{FIELD_WRITES_TABLE}_src ON {FIELD_WRITES_TABLE}(src);
"""
_COLUMNS = (
    "src, dao_method, table_id, column_name, how, value, value_kind, path, line, call_line, "
    "confidence, source"
)


@dataclass(frozen=True, slots=True)
class PendingWrite:
    """Uma chamada `incluir`/`alterar` resolvida para um método de DAO (decidida pelo resolvedor).

    `scope` vazio é o topo do arquivo (tela procedural, onde o formulário vale).
    `confidence` é a da `CALLS` (1/n no receptor ambíguo)."""

    src: str  # File: | Method: | Function: de quem chama
    dao_method: str  # Method:cl_x::incluir
    method_name: str  # incluir | alterar
    path: str
    scope: str
    call_line: int
    sets: tuple[FieldSet, ...]
    confidence: float


@dataclass(frozen=True, slots=True)
class FieldWriteRow:
    """Uma linha de `php_field_writes`: quem preenche a coluna, como e com que valor."""

    src: str
    dao_method: str
    table_id: str
    column_name: str
    how: str  # atribuicao | formulario
    value: str
    value_kind: str  # literal|null|variavel|parametro|chamada|expressao|formulario
    path: str
    line: int
    call_line: int
    confidence: float
    source: str = SOURCE


@dataclass(slots=True)
class FieldWritesCollector:
    """Recolhido pelo resolvedor na 2ª passada: as chamadas e os campos de formulário."""

    pending: list[PendingWrite] = field(default_factory=list)
    form_fields: dict[str, tuple[FormField, ...]] = field(default_factory=dict)

    def add_record(self, record: PhpFileRecord) -> None:
        if record.form_fields:
            self.form_fields[record.path] = tuple(record.form_fields)

    def add_call(self, pending: PendingWrite) -> None:
        self.pending.append(pending)


@dataclass(frozen=True, slots=True)
class WriteView:
    """O que a montagem das linhas precisa do grafo (do builder no scan; testável sem ele)."""

    # (método do DAO, tipo da aresta `INSERTS`/`UPDATES`) → ((tabela, confiança), ...)
    tables_of: Mapping[tuple[str, str], tuple[tuple[str, float], ...]]
    columns: Mapping[str, frozenset[str]]  # tabela → colunas em minúsculas
    infra: frozenset[str]  # tabelas `is_infra` (`db_acount*`)
    includes: Mapping[str, tuple[str, ...]]  # arquivo → arquivos que ele inclui (1 nível)


@dataclass(frozen=True, slots=True)
class BuildStats:
    not_column: int = 0  # `erro_status`, `numrows`… (não são coluna da tabela)
    calls_with_sets: int = 0
    calls_with_form: int = 0
    no_columns: int = 0  # tabela sem colunas conhecidas (placeholder): nada a validar


def view_from_builder(builder: GraphBuilder, pending: Sequence[PendingWrite]) -> WriteView:
    """Lê do builder só o que as chamadas pendentes tocam (métodos de DAO e includes das telas)."""
    daos = {p.dao_method for p in pending}
    top_files = {p.src for p in pending if p.scope == ""}
    tables: dict[tuple[str, str], list[tuple[str, float]]] = {}
    includes: dict[str, list[str]] = {}
    wanted = set(_EDGE_OF_METHOD.values())
    for edge in builder.iter_edges():
        edge_type = edge.type.value
        if edge_type in wanted and edge.src in daos:
            tables.setdefault((edge.src, edge_type), []).append((edge.dst, edge.confidence))
        elif edge.type in _INCLUDE_TYPES and edge.src in top_files and edge.dst.startswith("File:"):
            includes.setdefault(key_of(edge.src), []).append(key_of(edge.dst))
    touched = {table for items in tables.values() for table, _c in items}
    columns: dict[str, frozenset[str]] = {}
    infra: set[str] = set()
    for node in builder.iter_nodes():
        if node.label != NodeLabel.TABLE or node.id not in touched:
            continue
        columns[node.id] = frozenset(str(c).lower() for c in node.attrs.get("columns") or ())
        if node.attrs.get("is_infra"):
            infra.add(node.id)
    return WriteView(
        {key: tuple(items) for key, items in tables.items()},
        columns,
        frozenset(infra),
        {path: tuple(items) for path, items in includes.items()},
    )


def _form_value(widget: str, name: str) -> str:
    """Como o campo aparece na tela: `db_input('k02_juros')` ou `<select name="k02_corr">`."""
    if widget.startswith("html:"):
        return f'<{widget.removeprefix("html:")} name="{name}">'
    return f"{widget}('{name}')"


def build_field_writes(
    pending: Iterable[PendingWrite],
    view: WriteView,
    form_fields_by_path: Mapping[str, Sequence[FormField]],
) -> tuple[list[FieldWriteRow], BuildStats]:
    """Produz as linhas de `php_field_writes` (função pura).

    Para cada chamada, as tabelas são as que o DAO insere (`incluir`) ou atualiza (`alterar`),
    sem as de infraestrutura. A confiança da linha é a menor entre a da chamada e a da aresta do
    DAO para a tabela. Atribuição: cada `set` que é coluna da tabela. Formulário: só na tela
    procedural (escopo vazio), com os campos do próprio arquivo e dos que ele inclui (um nível),
    que são coluna e não foram atribuídos nesta chamada; confiança 0,8 ×.
    """
    rows: dict[RowKey, FieldWriteRow] = {}
    stats: Counter[str] = Counter()
    for call in pending:
        edge_type = _EDGE_OF_METHOD.get(call.method_name)
        if edge_type is None:
            continue
        forms = _forms_of(call, view, form_fields_by_path) if call.scope == "" else ()
        used_sets = used_form = False
        for table_id, table_confidence in view.tables_of.get((call.dao_method, edge_type), ()):
            if table_id in view.infra:
                continue
            columns = view.columns.get(table_id, frozenset())
            if not columns:
                stats["no_columns"] += 1
                continue
            confidence = min(call.confidence, table_confidence)
            assigned: set[str] = set()
            for item in call.sets:
                if item.field not in columns:
                    stats["not_column"] += 1
                    continue
                assigned.add(item.field)
                used_sets = True
                _put(
                    rows,
                    FieldWriteRow(
                        call.src,
                        call.dao_method,
                        table_id,
                        item.field,
                        HOW_ASSIGN,
                        item.value,
                        item.kind,
                        call.path,
                        item.line,
                        call.call_line,
                        confidence,
                    ),  # fmt: skip
                )
            for path, form in forms:
                if form.name not in columns or form.name in assigned:
                    continue
                used_form = True
                _put(
                    rows,
                    FieldWriteRow(
                        call.src,
                        call.dao_method,
                        table_id,
                        form.name,
                        HOW_FORM,
                        _form_value(form.widget, form.name),
                        KIND_FORM,
                        path,
                        form.line,
                        call.call_line,
                        round(confidence * FORM_FACTOR, 6),
                    ),  # fmt: skip
                )
        stats["calls_with_sets"] += 1 if used_sets else 0
        stats["calls_with_form"] += 1 if used_form else 0
    ordered = sorted(
        rows.values(),
        key=lambda r: (r.table_id, r.column_name, r.how, r.src, r.path, r.line, r.dao_method),
    )
    return ordered, BuildStats(
        not_column=stats["not_column"],
        calls_with_sets=stats["calls_with_sets"],
        calls_with_form=stats["calls_with_form"],
        no_columns=stats["no_columns"],
    )


def _forms_of(
    call: PendingWrite,
    view: WriteView,
    form_fields_by_path: Mapping[str, Sequence[FormField]],
) -> list[tuple[str, FormField]]:
    """Campos de formulário do arquivo da chamada e dos que ele inclui (um nível)."""
    found: list[tuple[str, FormField]] = []
    for path in (call.path, *view.includes.get(call.path, ())):
        found.extend((path, form) for form in form_fields_by_path.get(path, ()))
    return found


def _put(rows: dict[RowKey, FieldWriteRow], row: FieldWriteRow) -> None:
    key = (row.src, row.dao_method, row.table_id, row.column_name, row.how, row.path, row.line)
    rows.setdefault(key, row)


# --------------------------------------------------------------------------- tabela no .db


def store_field_writes(conn: sqlite3.Connection, rows: Sequence[FieldWriteRow]) -> dict[str, int]:
    """Recria `php_field_writes` numa transação e devolve as contagens do resumo do scan."""
    data = [
        (
            r.src, r.dao_method, r.table_id, r.column_name, r.how, r.value, r.value_kind, r.path,
            r.line, r.call_line, r.confidence, r.source,
        )
        for r in rows
    ]  # fmt: skip
    started = time.perf_counter()
    try:
        with conn:
            conn.executescript("BEGIN;" + _SCHEMA)
            conn.executemany(
                f"INSERT INTO {FIELD_WRITES_TABLE} ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                data,
            )
    except sqlite3.Error as exc:
        raise StoreError(f"campos preenchidos pelo código PHP não gravados: {exc}") from exc
    counts = Counter(r.how for r in rows)
    return {
        "rows": len(rows),
        HOW_ASSIGN: counts[HOW_ASSIGN],
        HOW_FORM: counts[HOW_FORM],
        "milliseconds": round((time.perf_counter() - started) * 1000),
    }


def has_field_writes(conn: sqlite3.Connection) -> bool:
    """O banco tem a tabela (mapas antigos não têm)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (FIELD_WRITES_TABLE,)
    ).fetchone()
    return row is not None


def field_writes(
    conn: sqlite3.Connection, table_id: str, column: str | None = None
) -> list[FieldWriteRow] | None:
    """Linhas da tabela (e da coluna, se dada). `None` quando o mapa é antigo."""
    if not has_field_writes(conn):
        return None
    sql = f"SELECT {_COLUMNS} FROM {FIELD_WRITES_TABLE} WHERE table_id = ?"
    args: list[str] = [table_id]
    if column is not None:
        sql += " AND column_name = ?"
        args.append(column.lower())
    sql += " ORDER BY column_name, how, src, path, line"
    return [
        FieldWriteRow(
            str(src),
            str(dao),
            str(table),
            str(col),
            str(how),
            str(value),
            str(kind),
            str(path),
            int(line),
            int(call_line),
            float(confidence),
            str(source),
        )  # fmt: skip
        for (
            src,
            dao,
            table,
            col,
            how,
            value,
            kind,
            path,
            line,
            call_line,
            confidence,
            source,
        ) in conn.execute(sql, args)
    ]
