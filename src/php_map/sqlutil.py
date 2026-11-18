"""Análise de instruções SQL com pglast, compartilhada pelo schema e pelo SQL no PHP."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pglast
from pglast import ast
from pglast.visitors import Visitor

from php_map.graph.model import EdgeType


@dataclass(frozen=True, slots=True)
class TableOp:
    """Tabela lida (`QUERIES`) ou alterada (`INSERTS`/`UPDATES`/`DELETES`)."""

    op: EdgeType
    name: str
    schema: str | None = None


@dataclass(frozen=True, slots=True)
class FuncCallRef:
    name: str
    schema: str | None = None
    # Nº de argumentos da chamada (desempata sobrecargas); `None` se não dá para saber.
    nargs: int | None = None


SqlRef = TableOp | FuncCallRef

_MUTATIONS: dict[type[ast.Node], EdgeType] = {
    ast.InsertStmt: EdgeType.INSERTS,
    ast.UpdateStmt: EdgeType.UPDATES,
    ast.DeleteStmt: EdgeType.DELETES,
}


class _Collector(Visitor):
    """Uma passada juntando tudo; a classificação vem depois (a ordem de visita não importa)."""

    def __init__(self) -> None:
        self.range_vars: list[ast.RangeVar] = []
        self.targets: dict[int, EdgeType] = {}
        self.skipped: set[int] = set()
        self.ctes: set[str] = set()
        self.calls: list[FuncCallRef] = []

    def visit(self, ancestors: Any, node: ast.Node) -> None:
        if isinstance(node, ast.RangeVar):
            self.range_vars.append(node)
        elif isinstance(node, (ast.InsertStmt, ast.UpdateStmt, ast.DeleteStmt)):
            if node.relation is not None:
                self.targets[id(node.relation)] = _MUTATIONS[type(node)]
        elif isinstance(node, ast.TruncateStmt):
            for relation in node.relations or ():
                if isinstance(relation, ast.RangeVar):
                    self.targets[id(relation)] = EdgeType.DELETES
        elif isinstance(node, ast.IntoClause) and node.rel is not None:
            # `SELECT … INTO v` (PL/pgSQL) ou `SELECT … INTO nova_tabela`: não é leitura.
            self.skipped.add(id(node.rel))
        elif isinstance(node, ast.CommonTableExpr) and node.ctename:
            self.ctes.add(node.ctename.lower())
        elif isinstance(node, ast.FuncCall) and node.funcname:
            parts = [p.sval.lower() for p in node.funcname if isinstance(p, ast.String) and p.sval]
            if parts:
                schema = parts[-2] if len(parts) > 1 else None
                nargs = None if node.func_variadic or node.agg_star else len(node.args or ())
                self.calls.append(FuncCallRef(parts[-1], schema, nargs))


def analyze_statement(node: ast.Node) -> list[SqlRef]:
    """Tabelas lidas/alteradas e funções chamadas numa instrução, sem repetição e em ordem.

    `INSERT/UPDATE/DELETE` → o alvo (`TRUNCATE` conta como `DELETES`); demais `RangeVar` →
    `QUERIES`, exceto nomes de CTE e o destino de `SELECT … INTO`.
    """
    collector = _Collector()
    collector(node)
    refs: list[SqlRef] = []
    for rv in collector.range_vars:
        if id(rv) in collector.skipped or not rv.relname:
            continue
        schema = rv.schemaname.lower() if rv.schemaname else None
        name = rv.relname.lower()
        op = collector.targets.get(id(rv))
        if op is None:
            if schema is None and name in collector.ctes:
                continue
            op = EdgeType.QUERIES
        refs.append(TableOp(op, name, schema))
    refs.extend(collector.calls)
    return list(dict.fromkeys(refs))


def is_sequence_name(name: str) -> bool:
    """Sequência, não tabela (`select last_value from x_seq`): nome termina em `_seq`."""
    return name.rsplit(".", 1)[-1].lower().endswith("_seq")


def fitting_overloads(
    ids: Sequence[str], arity: Mapping[str, tuple[int, float]], nargs: int | None
) -> list[str]:
    """Sobrecargas que aceitam `nargs` argumentos (`arity`: mín. e máx., ∞ com `VARIADIC`).

    Com uma só sobrecarga, sem `nargs` ou se nenhuma aceita (tipos/defaults que o parser não
    vê), devolve todas.
    """
    if nargs is None or len(ids) <= 1:
        return list(ids)
    fitting = [i for i in ids if i in arity and arity[i][0] <= nargs <= arity[i][1]]
    return fitting or list(ids)


def analyze_sql(sql: str) -> list[SqlRef]:
    """Parseia (pode lançar `pglast.parser.ParseError`) e analisa todas as instruções."""
    refs: list[SqlRef] = []
    for raw in pglast.parse_sql(sql):
        if raw.stmt is not None:
            refs.extend(analyze_statement(raw.stmt))
    return list(dict.fromkeys(refs))
