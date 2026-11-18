"""Leitor do dicionário de dados e dos menus do e-cidade.

Lê as instruções SQL das 8 tabelas de sistema (`db_sysmodulo`, `db_sysarquivo`, `db_sysarqmod`,
`db_syscampo`, `db_sysarqcamp`, `db_modulos`, `db_itensmenu`, `db_menu`) de um
`pg_dump --data-only --column-inserts` ou dos incrementais `e-cidade/db`, na ordem de
`expand_paths`, e devolve o estado final das linhas. Não conhece o grafo (ver
`resolve/catalog.py`).

Suportado: `INSERT … VALUES` (com ou sem lista de colunas), `DELETE … WHERE` e
`UPDATE … SET <col> = <literal> WHERE`, com o `WHERE` feito de `<chave> = n` e
`<chave> IN (n, …)` ligados por `AND`, inclusive dentro de `select fc_executa_ddl('…')`.
O resto (`INSERT … SELECT`, subconsulta no `WHERE`, `COPY`) só é contado em `unsupported`.
"""

import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pglast
from pglast import ast
from pglast.enums import A_Expr_Kind, BoolExprType

from php_map.ingest.sql_schema import (
    DDL_EXECUTORS,
    executed_ddl,
    expand_paths,
    first_code_offset,
    normalize_escapes,
    read_sql,
    split_statements,
)

logger = logging.getLogger(__name__)

MAX_EXAMPLES = 20


@dataclass(frozen=True, slots=True)
class _TableSpec:
    key: tuple[str, ...]
    columns: tuple[str, ...]  # ordem posicional (DAO `classes/db_<tabela>_classe.php`)


TABLES: Mapping[str, _TableSpec] = {
    "db_sysmodulo": _TableSpec(
        ("codmod",), ("codmod", "nomemod", "descricao", "dataincl", "ativo")
    ),
    "db_sysarquivo": _TableSpec(
        ("codarq",),
        ("codarq", "nomearq", "descricao", "sigla", "dataincl", "rotulo", "tipotabela"),
    ),
    "db_sysarqmod": _TableSpec(("codmod", "codarq"), ("codmod", "codarq")),
    "db_syscampo": _TableSpec(
        ("codcam",),
        (
            "codcam",
            "nomecam",
            "conteudo",
            "descricao",
            "valorinicial",
            "rotulo",
            "tamanho",
            "nulo",
            "maiusculo",
            "autocompl",
            "aceitatipo",
            "tipoobj",
            "rotulorel",
        ),
    ),
    "db_sysarqcamp": _TableSpec(
        ("codarq", "codcam"), ("codarq", "codcam", "seqarq", "codsequencia")
    ),
    "db_modulos": _TableSpec(
        ("id_item",), ("id_item", "nome_modulo", "descr_modulo", "imagem", "temexerc")
    ),
    "db_itensmenu": _TableSpec(
        ("id_item",),
        ("id_item", "descricao", "help", "funcao", "itemativo", "manutencao", "desctec"),
    ),
    "db_menu": _TableSpec(
        ("id_item", "id_item_filho", "modulo"),
        ("id_item", "id_item_filho", "menusequencia", "modulo"),
    ),
}
# Colunas que `rows_to_catalog` lê. O leitor do banco só pede estas: menos dado real
# em trânsito e menos tempo exposto às paradas da rede (lição aprendida em bases reais).
USED_COLUMNS: Mapping[str, frozenset[str]] = {
    "db_sysmodulo": frozenset({"codmod", "nomemod", "descricao"}),
    "db_sysarquivo": frozenset({"codarq", "nomearq", "descricao", "sigla", "rotulo"}),
    "db_sysarqmod": frozenset({"codmod", "codarq"}),
    "db_syscampo": frozenset({"codcam", "nomecam", "descricao", "rotulo"}),
    "db_sysarqcamp": frozenset({"codarq", "codcam", "seqarq"}),
    "db_modulos": frozenset({"id_item", "nome_modulo", "descr_modulo"}),
    "db_itensmenu": frozenset({"id_item", "descricao", "help", "funcao", "itemativo", "desctec"}),
    "db_menu": frozenset({"id_item", "id_item_filho", "menusequencia", "modulo"}),
}
_NAMES = "|".join(TABLES)
# Pré-filtro: só estas instruções vão para o pglast (o resto dos incrementais é enorme).
_DML_START = re.compile(
    rf'^\s*(?:insert\s+into|delete\s+from|update)\s+(?:"?[a-z_]+"?\.)?"?(?:{_NAMES})\b',
    re.IGNORECASE,
)
_ANY_TABLE = re.compile(rf"\b(?:{_NAMES})\b", re.IGNORECASE)
# `pg_dump` sem `--column-inserts` grava COPY: não suportado, mas não pode passar em silêncio.
_COPY_START = re.compile(rf'^\s*copy\s+(?:"?[a-z_]+"?\.)?"?(?:{_NAMES})\b', re.IGNORECASE)
_TRUE = frozenset({"1", "t", "true"})
_INTEGER = re.compile(r"^-?\d+$")

Evidence = Mapping[str, Any]
Row = Mapping[str, Any]
EvidenceFn = Callable[[str, tuple[int, ...]], Evidence]  # (tabela, chave) → evidência


# --------------------------------------------------------------------------- resultado


@dataclass(frozen=True, slots=True)
class DictModule:
    """`db_sysmodulo`: módulo do dicionário de dados."""

    codmod: int
    name: str
    description: str | None
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class CatalogTable:
    """`db_sysarquivo`: tabela, com o prefixo das colunas (`sigla`) e o rótulo."""

    codarq: int
    name: str
    description: str | None
    prefix: str | None
    label: str | None
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class TableModule:
    """`db_sysarqmod`: tabela → módulo do dicionário."""

    codmod: int
    codarq: int
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class CatalogColumn:
    """`db_syscampo`: coluna, com a descrição e o rótulo de tela."""

    codcam: int
    name: str
    description: str | None
    label: str | None
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class TableColumn:
    """`db_sysarqcamp`: tabela → coluna."""

    codarq: int
    codcam: int
    sequence: int | None
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class MenuModule:
    """`db_modulos`: módulo do menu (`id_item` é o item raiz do módulo)."""

    id_item: int
    name: str
    description: str | None
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class MenuItem:
    """`db_itensmenu`: item de menu; `funcao` é o arquivo da tela (com query string)."""

    id_item: int
    description: str | None
    help: str | None
    funcao: str | None
    active: bool
    desctec: str | None
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class MenuEdge:
    """`db_menu`: `parent` (id_item) → `child` (id_item_filho) no menu do módulo `module`."""

    parent: int
    child: int
    sequence: int | None
    module: int
    evidence: Evidence


@dataclass(slots=True)
class CatalogReadStats:
    source: str = "sql"  # "sql" (arquivos) ou "db:<host>/<base>"
    files: int = 0
    statements: int = 0
    read: dict[str, int] = field(default_factory=dict)  # instruções aplicadas por tabela
    rows: dict[str, int] = field(default_factory=dict)  # linhas no estado final
    missing_tables: list[str] = field(default_factory=list)  # tabela ausente no banco
    unsupported: int = 0
    parse_errors: int = 0
    no_match: int = 0  # UPDATE/DELETE que não achou a linha (delta sobre a base ausente)
    executed_ddl: int = 0
    escaped_files: int = 0
    examples: list[str] = field(default_factory=list)  # `caminho:linha motivo`
    text_repaired: int = 0  # texto com UTF-8 gravado duas vezes, recuperado
    text_lost: int = 0  # idem, mas o acento já tinha virado `�` antes de chegar ao banco
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class CatalogData:
    modules: tuple[DictModule, ...]
    tables: tuple[CatalogTable, ...]
    table_modules: tuple[TableModule, ...]
    columns: tuple[CatalogColumn, ...]
    table_columns: tuple[TableColumn, ...]
    menu_modules: tuple[MenuModule, ...]
    menu_items: tuple[MenuItem, ...]
    menu_edges: tuple[MenuEdge, ...]
    stats: CatalogReadStats


# --------------------------------------------------------------------------- valores


def as_bool(value: Any) -> bool:
    """`'1'`, `'t'`, `'true'`, `true`, `1` → `True`; o resto (`'2'`, `'f'`, `null`…) → `False`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return isinstance(value, str) and value.strip().lower() in _TRUE


def _key_value(value: Any) -> int | None:
    """Valor de chave: as 8 tabelas têm chaves inteiras (`'10216'` também vale)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER.match(value.strip()):
        return int(value.strip())
    return None


def _text(value: Any) -> str | None:
    return None if value is None else str(value).strip()


# Um caractere UTF-8 de 2 a 4 bytes lido como Latin-1 (um caractere por byte): `ç` → `Ã§`.
# O 2º caractere em `\x80-\xbf` separa o lixo do `Ã` legítimo (`NÃO`, `RETENÇÃO`).
_DOUBLE_UTF8 = re.compile(
    r"[\xc2-\xdf][\x80-\xbf]|[\xe0-\xef][\x80-\xbf]{2}|[\xf0-\xf4][\x80-\xbf]{3}"
)


def repair_text(text: str) -> str:
    """Desfaz o UTF-8 gravado duas vezes numa base LATIN1 (`AlteraÃ§Ã£o` → `Alteração`).

    Trecho a trecho, para consertar também o texto misto (`Máximo DÃ©bito`). O `ï¿½` (o `�`
    em UTF-8) vira `�`: o acento se perdeu antes de chegar ao banco.
    """

    def decode(match: re.Match[str]) -> str:
        try:
            return match.group(0).encode("latin-1").decode("utf-8")
        except UnicodeDecodeError:
            return match.group(0)

    return _DOUBLE_UTF8.sub(decode, text)


def _literal(node: Any) -> tuple[bool, Any]:
    """`(True, valor)` para constante (texto com `strip()`), `(False, None)` para expressão."""
    if isinstance(node, ast.TypeCast):
        return _literal(node.arg)
    if not isinstance(node, ast.A_Const):
        return False, None
    if node.isnull:
        return True, None
    val = node.val
    if isinstance(val, ast.Integer):
        return True, val.ival or 0
    if isinstance(val, ast.Boolean):
        return True, bool(val.boolval)
    if isinstance(val, ast.Float):
        return True, val.fval
    if isinstance(val, ast.String):
        return True, (val.sval or "").strip()
    return False, None


# --------------------------------------------------------------------------- leitura


@dataclass(slots=True)
class _Row:
    values: dict[str, Any]
    evidence: Evidence


@dataclass(slots=True)
class _Loader:
    prefilter: bool
    stats: CatalogReadStats = field(default_factory=CatalogReadStats)
    rows: dict[str, dict[tuple[int, ...], _Row]] = field(
        default_factory=lambda: {name: {} for name in TABLES}
    )

    def note(self, evidence: Evidence, reason: str) -> None:
        note(self.stats, evidence, reason)

    def unsupported(self, evidence: Evidence, reason: str) -> None:
        self.stats.unsupported += 1
        self.note(evidence, reason)

    def applied(self, table: str) -> None:
        self.stats.read[table] = self.stats.read.get(table, 0) + 1


def load_catalog(paths: Sequence[Path], prefilter: bool = True) -> CatalogData:
    """Estado final das tabelas do catálogo, aplicando os arquivos na ordem de `expand_paths`.

    `prefilter=False` manda toda instrução para o pglast. Só existe para provar, nos testes,
    que o pré-filtro de texto não perde nada.
    """
    started = time.perf_counter()
    loader = _Loader(prefilter)
    for path, display in expand_paths(paths):
        loader.stats.files += 1
        text = read_sql(path)
        if "\\'" in text:
            loader.stats.escaped_files += 1
            text = normalize_escapes(text)
        _load_text(loader, text, display)
    loader.stats.read = dict(sorted(loader.stats.read.items()))
    rows = {name: [rows[key].values for key in sorted(rows)] for name, rows in loader.rows.items()}
    data = rows_to_catalog(rows, lambda table, key: loader.rows[table][key].evidence, loader.stats)
    loader.stats.seconds = round(time.perf_counter() - started, 2)
    return data


def note(stats: CatalogReadStats, evidence: Evidence, reason: str) -> None:
    """Exemplo `caminho:linha motivo` (ou `db:<tabela> <chave> motivo`), até `MAX_EXAMPLES`."""
    if len(stats.examples) < MAX_EXAMPLES:
        where = evidence.get("line", evidence.get("key", ""))
        stats.examples.append(f"{evidence['path']}:{where} {reason}")


def _load_text(loader: _Loader, text: str, display: str, first_line: int = 1) -> None:
    counted_until, counted_line = 0, first_line
    for start, stop in split_statements(text):
        chunk = text[start:stop]
        code = first_code_offset(chunk)
        if code >= len(chunk):
            continue
        counted_line += text.count("\n", counted_until, start)
        counted_until = start
        line = counted_line + chunk.count("\n", 0, code)
        loader.stats.statements += 1
        evidence = {"path": display, "line": line}
        if _COPY_START.match(chunk[code:]):
            loader.unsupported(evidence, "COPY (use pg_dump --column-inserts)")
            logger.warning("%s:%d: COPY de tabela do catálogo não é lido", display, line)
            continue
        if loader.prefilter and not _is_candidate(chunk[code:]):
            continue
        try:
            raws = pglast.parse_sql(chunk)
        except pglast.parser.ParseError as exc:
            loader.stats.parse_errors += 1
            loader.note(evidence, f"parse_error: {exc}")
            continue
        for raw in raws:
            _statement(loader, raw.stmt, display, line, evidence)


def _is_candidate(code: str) -> bool:
    if _DML_START.match(code):
        return True
    head = code[:400].lower()
    return (
        head.startswith("select")
        and any(name in head for name in DDL_EXECUTORS)
        and _ANY_TABLE.search(code) is not None
    )


def _statement(loader: _Loader, stmt: Any, display: str, line: int, evidence: Evidence) -> None:
    relation = getattr(stmt, "relation", None)
    table = (relation.relname or "").lower() if isinstance(relation, ast.RangeVar) else ""
    if isinstance(stmt, ast.InsertStmt) and table in TABLES:
        _insert(loader, stmt, table, evidence)
    elif isinstance(stmt, ast.UpdateStmt) and table in TABLES:
        _update(loader, stmt, table, evidence)
    elif isinstance(stmt, ast.DeleteStmt) and table in TABLES:
        _delete(loader, stmt, table, evidence)
    elif (inner := executed_ddl(stmt)) is not None:
        # Uma string pode ter milhares de INSERTs: divide e filtra antes de parsear (como no
        # schema em arquivos).
        loader.stats.executed_ddl += 1
        _load_text(loader, inner, display, first_line=line)


def _row_key(table: str, values: Mapping[str, Any]) -> tuple[int, ...] | None:
    key: list[int] = []
    for column in TABLES[table].key:
        part = _key_value(values.get(column))
        if part is None:
            return None
        key.append(part)
    return tuple(key)


def _insert(loader: _Loader, stmt: ast.InsertStmt, table: str, evidence: Evidence) -> None:
    select = stmt.selectStmt
    if not isinstance(select, ast.SelectStmt) or not select.valuesLists:
        loader.unsupported(evidence, f"INSERT … SELECT em {table}")
        return
    if stmt.cols:
        names = [(c.name or "").lower() for c in stmt.cols if isinstance(c, ast.ResTarget)]
    else:
        names = list(TABLES[table].columns)
    rows = loader.rows[table]
    for values in select.valuesLists:
        row: dict[str, Any] = {}
        # zip: valores a mais que a ordem conhecida (libcliente, ativo…) são ignorados.
        for name, expr in zip(names, values, strict=False):
            ok, value = _literal(expr)
            if ok:
                row[name] = value
        key = _row_key(table, row)
        if key is None:
            loader.unsupported(evidence, f"INSERT em {table} sem chave literal")
            continue
        rows[key] = _Row(row, evidence)  # chave repetida: a última vence
        loader.applied(table)


def _conditions(node: Any, table: str) -> dict[str, set[int]] | None:
    """`WHERE` como `{coluna da chave: valores aceitos}`; forma não suportada → `None`."""
    if node is None:
        return None
    if isinstance(node, ast.BoolExpr):
        if node.boolop != BoolExprType.AND_EXPR:
            return None
        merged: dict[str, set[int]] = {}
        for arg in node.args or ():
            part = _conditions(arg, table)
            if part is None:
                return None
            for name, accepted in part.items():
                merged[name] = merged[name] & accepted if name in merged else accepted
        return merged
    if not isinstance(node, ast.A_Expr) or not node.name:
        return None
    operator = getattr(node.name[0], "sval", None)
    column_ref: Any = node.lexpr
    other: Any = node.rexpr
    if node.kind == A_Expr_Kind.AEXPR_OP and not isinstance(column_ref, ast.ColumnRef):
        column_ref, other = other, column_ref  # `10 = id_item`
    if operator != "=" or not isinstance(column_ref, ast.ColumnRef) or not column_ref.fields:
        return None
    column = getattr(column_ref.fields[-1], "sval", None)
    if not column or column.lower() not in TABLES[table].key:
        return None
    if node.kind == A_Expr_Kind.AEXPR_OP:
        items: Sequence[Any] = (other,)
    elif node.kind == A_Expr_Kind.AEXPR_IN and isinstance(other, (tuple, list)):
        items = other
    else:
        return None
    values: set[int] = set()
    for item in items:
        ok, value = _literal(item)
        key = _key_value(value) if ok else None
        if key is None:
            return None
        values.add(key)
    return {column.lower(): values}


def _matching(
    loader: _Loader, table: str, conditions: Mapping[str, set[int]]
) -> list[tuple[int, ...]]:
    rows = loader.rows[table]
    key_columns = TABLES[table].key
    if all(len(conditions.get(c, ())) == 1 for c in key_columns):  # chave completa: acesso direto
        key = tuple(next(iter(conditions[c])) for c in key_columns)
        return [key] if key in rows else []
    positions = [(key_columns.index(c), values) for c, values in conditions.items()]
    return [key for key in rows if all(key[i] in values for i, values in positions)]


def _delete(loader: _Loader, stmt: ast.DeleteStmt, table: str, evidence: Evidence) -> None:
    conditions = None if stmt.usingClause else _conditions(stmt.whereClause, table)
    if conditions is None:
        loader.unsupported(evidence, f"DELETE em {table} com WHERE não suportado")
        return
    keys = _matching(loader, table, conditions)
    if not keys:
        loader.stats.no_match += 1
    for key in keys:
        del loader.rows[table][key]
    loader.applied(table)


def _update(loader: _Loader, stmt: ast.UpdateStmt, table: str, evidence: Evidence) -> None:
    assignments: dict[str, Any] = {}
    for target in stmt.targetList or ():
        ok, value = _literal(target.val) if isinstance(target, ast.ResTarget) else (False, None)
        if not ok or not target.name:
            loader.unsupported(evidence, f"UPDATE em {table} com valor não literal")
            return
        assignments[target.name.lower()] = value
    conditions = None if stmt.fromClause else _conditions(stmt.whereClause, table)
    if conditions is None:
        loader.unsupported(evidence, f"UPDATE em {table} com WHERE não suportado")
        return
    rows = loader.rows[table]
    keys = _matching(loader, table, conditions)
    if not keys:
        loader.stats.no_match += 1
    for key in keys:
        values = {**rows.pop(key).values, **assignments}
        new_key = _row_key(table, values)  # o UPDATE pode mudar a chave (`set id_item = 7941`)
        if new_key is None:
            loader.unsupported(evidence, f"UPDATE em {table} anula a chave")
            continue
        rows[new_key] = _Row(values, evidence)
    loader.applied(table)


# --------------------------------------------------------------------------- estado final


def rows_to_catalog(
    rows_by_table: Mapping[str, Iterable[Row]],
    evidence_of: EvidenceFn,
    stats: CatalogReadStats,
) -> CatalogData:
    """Linhas finais das 8 tabelas → `CatalogData`. Usada pelos dois leitores (arquivos e banco).

    Normaliza como o leitor de arquivos: `strip()` nos textos (as colunas `char(n)` do banco vêm com
    espaços à direita), nomes de tabela e coluna em minúsculas e `as_bool` nos booleanos.
    Linha sem chave inteira é contada em `unsupported`; chave repetida: a última vence.
    `stats.rows` recebe as linhas por tabela.
    """
    final: dict[str, list[tuple[Row, Evidence]]] = {}
    for table in TABLES:
        keyed: dict[tuple[int, ...], Row] = {}
        for values in rows_by_table.get(table, ()):
            key = _row_key(table, values)
            if key is None:
                stats.unsupported += 1
                note(stats, {"path": f"db:{table}"}, f"linha de {table} sem chave inteira")
                continue
            keyed[key] = values
        final[table] = [(keyed[key], evidence_of(table, key)) for key in sorted(keyed)]
        stats.rows[table] = len(keyed)

    def req(values: Row, column: str) -> int:
        value = _key_value(values.get(column))
        assert value is not None  # colunas da chave: garantidas por `_row_key`
        return value

    def free(value: Any) -> str | None:
        """Texto livre (descrição, rótulo, ajuda): `_text` + `repair_text`, com a contagem."""
        text = _text(value)
        if text is None:
            return None
        repaired = repair_text(text)
        if repaired != text:
            if "�" in repaired and "�" not in text:
                stats.text_lost += 1
            else:
                stats.text_repaired += 1
        return repaired

    return CatalogData(
        modules=tuple(
            DictModule(
                req(v, "codmod"), _text(v.get("nomemod")) or "", free(v.get("descricao")), ev
            )
            for v, ev in final["db_sysmodulo"]
        ),
        tables=tuple(
            CatalogTable(
                req(v, "codarq"),
                (_text(v.get("nomearq")) or "").lower(),
                free(v.get("descricao")),
                _text(v.get("sigla")),
                free(v.get("rotulo")),
                ev,
            )
            for v, ev in final["db_sysarquivo"]
        ),
        table_modules=tuple(
            TableModule(req(v, "codmod"), req(v, "codarq"), ev) for v, ev in final["db_sysarqmod"]
        ),
        columns=tuple(
            CatalogColumn(
                req(v, "codcam"),
                (_text(v.get("nomecam")) or "").lower(),
                free(v.get("descricao")),
                free(v.get("rotulo")),
                ev,
            )
            for v, ev in final["db_syscampo"]
        ),
        table_columns=tuple(
            TableColumn(req(v, "codarq"), req(v, "codcam"), _key_value(v.get("seqarq")), ev)
            for v, ev in final["db_sysarqcamp"]
        ),
        menu_modules=tuple(
            MenuModule(
                req(v, "id_item"),
                _text(v.get("nome_modulo")) or "",
                free(v.get("descr_modulo")),
                ev,
            )
            for v, ev in final["db_modulos"]
        ),
        menu_items=tuple(
            MenuItem(
                req(v, "id_item"),
                free(v.get("descricao")),
                free(v.get("help")),
                _text(v.get("funcao")),
                # Sem `itemativo` na instrução, vale o padrão da coluna no banco (ativo).
                as_bool(v["itemativo"]) if "itemativo" in v else True,
                free(v.get("desctec")),
                ev,
            )
            for v, ev in final["db_itensmenu"]
        ),
        menu_edges=tuple(
            MenuEdge(
                req(v, "id_item"),
                req(v, "id_item_filho"),
                _key_value(v.get("menusequencia")),
                req(v, "modulo"),
                ev,
            )
            for v, ev in final["db_menu"]
        ),
        stats=stats,
    )
