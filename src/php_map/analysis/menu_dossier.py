"""Dossiê de um item de menu: o que a tela calcula e o que ela cadastra.

O analista parte de «Cadastro > Procedimentos > Cálculo IPTU > Cálculo Parcial». O dossiê junta,
sem IA: os arquivos que implementam o menu (`IMPLEMENTS_ACTION`), as funções da base que eles
alcançam (a mesma alcançabilidade da origem de valor, `value_origin._reach_from_php`) com a
contagem de fórmulas de cada uma, e as tabelas que os arquivos e os DAOs gravam, com os campos do
dicionário. O que não se resolve vai para `untraced` com o motivo, sobretudo a chamada dinâmica
(`execute 'select ' || nome_da_funcao`), que **não** é «sem cálculo».
"""

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from php_map.analysis import value_origin as vo
from php_map.analysis.field_search import routine_names
from php_map.analysis.formulas import Resolver
from php_map.errors import QueryError

DEFAULT_MAX_FUNCTIONS = 10
MAX_COUNTED_FUNCTIONS = 40  # funções cujo texto é lido para contar fórmulas
MAX_TABLES = 12
MAX_COLUMNS = 8
MAX_WRITE_HOPS = 3
MAX_WRITE_NODES = 300
MAX_CANDIDATES = 10
_WRITE_EDGES = ("INSERTS", "UPDATES", "DELETES")
_DAO_EDGES = ("CALLS", "INSTANTIATES")
_MENU_ID = re.compile(r"^(?:BusinessAction:)?(?:menu:)?(\d+)$", re.IGNORECASE)


class AmbiguousMenuError(QueryError):
    """O trecho do caminho casa com mais de um menu; `candidates` traz `(id, caminho)`."""

    def __init__(self, message: str, candidates: Sequence[tuple[str, str]]) -> None:
        super().__init__(message)
        self.candidates = tuple(candidates)


@dataclass(frozen=True, slots=True)
class MenuFunction:
    routine_id: str
    formulas_total: int


@dataclass(frozen=True, slots=True)
class MenuTable:
    table_id: str
    label: str | None
    columns: tuple[tuple[str, str | None], ...]  # (coluna, nome no dicionário), até MAX_COLUMNS
    columns_more: int
    via: tuple[str, ...]  # quem grava: `Method:cl_tabrecjm::incluir`, `File:...`


@dataclass(frozen=True, slots=True)
class MenuDossier:
    menu_id: str
    menu_path: str | None
    name: str | None
    files: tuple[str, ...]
    functions: tuple[MenuFunction, ...]
    functions_total: int  # funções da base alcançadas
    functions_more: int  # as que ficaram de fora do corte
    tables_written: tuple[MenuTable, ...]
    tables_more: int
    untraced: tuple[tuple[str, str], ...]


# --------------------------------------------------------------------------- achar o menu


def _paths_of(attrs: dict[str, Any]) -> list[str]:
    paths = attrs.get("menu_paths")
    found = [p for p in paths if isinstance(p, str) and p] if isinstance(paths, list) else []
    single = attrs.get("menu_path")
    if isinstance(single, str) and single and single not in found:
        found.insert(0, single)
    return found


def _words_of(text: str) -> str:
    return " ".join(vo.normalize(w) for w in re.findall(r"[A-Za-z0-9À-ÿ²]+", text))


def resolve_menu(conn: sqlite3.Connection, value: str) -> str:
    """O id de um item de menu: `BusinessAction:menu:1533`, `menu:1533`, só o número, o caminho
    inteiro ou um trecho dele (sem acento e sem diferença de caixa). `QueryError` se não achar;
    `AmbiguousMenuError` (com os candidatos) se o trecho casar com mais de um."""
    text = value.strip()
    if not text:
        raise QueryError("informe o menu: o id ou um trecho do caminho")
    by_id = _MENU_ID.match(text)
    if by_id or text.startswith("BusinessAction:"):
        node_id = f"BusinessAction:menu:{by_id.group(1)}" if by_id else text
        row = conn.execute(
            "SELECT 1 FROM nodes WHERE id = ? AND label = 'BusinessAction'", (node_id,)
        ).fetchone()
        if row:
            return node_id
        raise QueryError(f"menu não encontrado: {node_id}")
    wanted = _words_of(text)
    if not wanted:
        raise QueryError("informe o menu: o id ou um trecho do caminho")
    exact: list[tuple[str, str]] = []
    partial: list[tuple[str, str]] = []
    for node_id, raw in conn.execute("SELECT id, attrs FROM nodes WHERE label = 'BusinessAction'"):
        for path in _paths_of(json.loads(raw)):
            words = _words_of(path)
            if words == wanted:
                exact.append((node_id, path))
                break
            if wanted in words:
                partial.append((node_id, path))
                break
    found = exact or partial
    if len(found) == 1:
        return found[0][0]
    if not found:
        raise QueryError(f"nenhum menu casa com {text!r}")
    shown = "; ".join(f"{node_id} ({path})" for node_id, path in found[:MAX_CANDIDATES])
    raise AmbiguousMenuError(
        f"mais de um menu casa com {text!r}; use o id: {shown}", found[:MAX_CANDIDATES]
    )


# --------------------------------------------------------------------------- o que o menu toca


def _written_tables(
    conn: sqlite3.Connection, res: Resolver, roots: Sequence[str]
) -> list[tuple[str, tuple[str, ...]]]:
    """`(tabela, quem grava)` pelos arquivos do menu e pelos DAOs e métodos que eles chamam, até
    `MAX_WRITE_HOPS` níveis. Tabela de infraestrutura (a auditoria) fica de fora."""
    found: dict[str, list[str]] = {}
    seen = set(roots)
    frontier = list(roots)
    for _level in range(MAX_WRITE_HOPS):
        if not frontier or len(seen) >= MAX_WRITE_NODES:
            break
        marks = ",".join("?" for _ in frontier)
        for src, dst in conn.execute(
            f"SELECT src, dst FROM edges WHERE src IN ({marks}) "  # noqa: S608
            f"AND type IN ({','.join('?' for _ in _WRITE_EDGES)}) ORDER BY id",
            (*frontier, *_WRITE_EDGES),
        ):
            if dst.startswith("Table:") and not res.attrs(dst).get("is_infra"):
                found.setdefault(dst, [])
                if src not in found[dst]:
                    found[dst].append(src)
        following: list[str] = []
        for (dst,) in conn.execute(
            f"SELECT DISTINCT dst FROM edges WHERE src IN ({marks}) "  # noqa: S608
            f"AND type IN ({','.join('?' for _ in _DAO_EDGES)}) ORDER BY id",
            (*frontier, *_DAO_EDGES),
        ):
            if dst not in seen and not dst.startswith(("File:", "PgFunction:", "Table:")):
                seen.add(dst)
                following.append(dst)
        frontier = following[: MAX_WRITE_NODES - len(seen)]
    return [(table, tuple(writers)) for table, writers in found.items()]


def _audit_table(table_id: str) -> bool:
    """`configuracoes.db_acount*`: a trilha de auditoria que todo DAO grava, não é da tela."""
    return vo.short(table_id).startswith("configuracoes.db_acount")


def _table_entry(res: Resolver, table_id: str, via: tuple[str, ...]) -> MenuTable:
    attrs = res.attrs(table_id)
    info = attrs.get("catalog") or {}
    label = info.get("label") if isinstance(info.get("label"), str) else None
    catalog = attrs.get("columns_catalog") or {}
    columns = tuple(
        (str(column), str(item.get("label")) if isinstance(item.get("label"), str) else None)
        for column, item in catalog.items()
    )
    return MenuTable(
        table_id, label, columns[:MAX_COLUMNS], max(0, len(columns) - MAX_COLUMNS), via
    )


def _dynamic_notes(conn: sqlite3.Connection, routines: Sequence[str]) -> list[tuple[str, str]]:
    if not routines:
        return []
    marks = ",".join("?" for _ in routines)
    try:
        rows = conn.execute(
            "SELECT routine_id, MIN(line) FROM routine_queries "  # noqa: S608
            f"WHERE kind = 'dynamic' AND routine_id IN ({marks}) GROUP BY routine_id",
            list(routines),
        ).fetchall()
    except sqlite3.OperationalError:  # mapa antigo, sem as consultas das funções
        return []
    line_of = {routine: int(line) for routine, line in rows}
    return [
        (
            f"{vo.short(routine).partition('(')[0].removeprefix('public.')}:{line_of[routine]}",
            "chamada dinâmica (`execute` com texto montado na hora): o que ela chama só se sabe "
            "em tempo de execução, e o mapa não rastreia",
        )
        for routine in routines
        if routine in line_of
    ]


def menu_dossier(
    conn: sqlite3.Connection, menu: str, *, max_functions: int = DEFAULT_MAX_FUNCTIONS
) -> MenuDossier:
    """Tudo o que o item de menu toca: arquivos, funções da base (com as fórmulas) e tabelas
    gravadas (com os campos do dicionário). Determinístico, sem IA."""
    menu_id = resolve_menu(conn, menu)
    res = Resolver(conn)
    attrs = res.attrs(menu_id)
    paths = _paths_of(attrs)
    name = attrs.get("name") if isinstance(attrs.get("name"), str) else None
    untraced: list[tuple[str, str]] = []
    files = tuple(
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT src FROM edges WHERE dst = ? AND type = 'IMPLEMENTS_ACTION' "
            "ORDER BY src",
            (menu_id,),
        )
    )
    if not files:
        untraced.append((menu_id, "nenhum arquivo implementa este menu"))
        return MenuDossier(
            menu_id, paths[0] if paths else None, name, (), (), 0, 0, (), 0, tuple(untraced)
        )
    reach = vo._reach_from_php(conn, files)
    counted = reach[:MAX_COUNTED_FUNCTIONS]
    if len(reach) > len(counted):
        untraced.append(
            (
                "contagem de fórmulas",
                f"só as {len(counted)} primeiras de {len(reach)} funções alcançadas foram lidas",
            )
        )
    counts: list[MenuFunction] = []
    for routine in counted:
        try:
            model = res.model(routine)
            total = len(model.formulas()) if model is not None else 0
        except Exception as exc:  # uma função difícil não derruba o dossiê do menu
            total = 0
            untraced.append((vo.short(routine), f"fórmulas não lidas ({type(exc).__name__})"))
        counts.append(MenuFunction(routine, total))
    counts.sort(key=lambda f: (-f.formulas_total, vo.short(f.routine_id)))
    shown = counts[: max(1, max_functions)]
    untraced += _dynamic_notes(conn, [f.routine_id for f in shown])
    written = [
        (table, via) for table, via in _written_tables(conn, res, files) if not _audit_table(table)
    ]
    tables = tuple(_table_entry(res, table, via) for table, via in written[:MAX_TABLES])
    return MenuDossier(
        menu_id=menu_id,
        menu_path=paths[0] if paths else None,
        name=name,
        files=files,
        functions=tuple(shown),
        functions_total=len(reach),
        functions_more=max(0, len(reach) - len(shown)),
        tables_written=tables,
        tables_more=max(0, len(written) - len(tables)),
        untraced=tuple(untraced),
    )


def menu_dossier_json(dossier: MenuDossier) -> dict[str, Any]:
    data = asdict(dossier)
    data["untraced"] = [{"what": what, "reason": reason} for what, reason in dossier.untraced]
    return dict(json.loads(json.dumps(data, ensure_ascii=False)))


def render_menu_dossier(dossier: MenuDossier) -> str:
    title = dossier.menu_path
    if title is None:  # sem caminho legível: o id e o arquivo, sem inventar um caminho
        shown = ", ".join(vo.short(f) for f in dossier.files[:2])
        title = f"{dossier.menu_id} ({shown})" if shown else dossier.menu_id
    parts = [
        f"# Dossiê do menu: {title}",
        "",
        "> Gerado pelo php-map, sem IA: os arquivos que implementam o menu, as funções da "
        "base que eles alcançam (com as fórmulas) e as tabelas que a tela grava (com os campos "
        "do dicionário).",
        "",
        f"- menu: `{dossier.menu_id}`",
    ]
    if dossier.menu_path is None:
        parts.append("- sem caminho legível (mapa sem `--catalog-db` ou item sem texto)")
    parts += ["", "## Arquivos que implementam o menu", ""]
    if dossier.files:
        parts += [f"- `{vo.short(f)}`" for f in dossier.files]
    else:
        parts.append("Nenhum arquivo implementa este menu.")
    parts += ["", "## Funções da base que o menu alcança", ""]
    if dossier.functions:
        parts += ["| Função | Fórmulas |", "|---|---|"]
        names = routine_names(tuple(f.routine_id for f in dossier.functions))
        parts += [
            f"| `{name}` | {f.formulas_total} |"
            for name, f in zip(names, dossier.functions, strict=True)
        ]
        if dossier.functions_more:
            parts += ["", f"_e mais {dossier.functions_more} função(ões) alcançada(s)_"]
    else:
        parts.append("Nenhuma função da base é alcançada pelos arquivos do menu.")
    parts += ["", "## Tabelas que a tela grava", ""]
    if dossier.tables_written:
        for table in dossier.tables_written:
            head = f"### {vo.short(table.table_id)}" + (f" ({table.label})" if table.label else "")
            parts += [head, ""]
            if table.via:
                parts.append(
                    "- gravada por: " + ", ".join(f"`{vo.short(v)}`" for v in table.via[:3])
                )
            if table.columns:
                shown = ", ".join(
                    f"`{c}` {label}" if label else f"`{c}`" for c, label in table.columns
                )
                more = f" e mais {table.columns_more}" if table.columns_more else ""
                parts.append(f"- campos do dicionário: {shown}{more}")
            parts.append("")
        if dossier.tables_more:
            parts += [f"_e mais {dossier.tables_more} tabela(s)_", ""]
    else:
        parts += ["Nenhuma tabela é gravada pelos arquivos do menu.", ""]
    parts += ["## Não rastreado", ""]
    if dossier.untraced:
        parts += [f"- `{what}`: {reason}" for what, reason in dossier.untraced]
    else:
        parts.append("Nada ficou sem rastro.")
    return "\n".join(parts).rstrip() + "\n"


__all__ = [
    "AmbiguousMenuError",
    "MenuDossier",
    "MenuFunction",
    "MenuTable",
    "menu_dossier",
    "menu_dossier_json",
    "render_menu_dossier",
    "resolve_menu",
]
