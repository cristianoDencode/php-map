"""Do que a pessoa digita para o nó do grafo: tela, tabela, módulo, endpoint, função.

Cada resolvedor devolve o ID canônico ou levanta `BriefingError` com uma mensagem em português
e, quando há mais de uma possibilidade, a lista delas (para a pessoa escolher).
"""

import difflib
import json
import sqlite3
from typing import Any

from php_map.briefing.model import BriefingError
from php_map.web import menus

MAX_CANDIDATES = 8


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _exists(conn: sqlite3.Connection, node_id: str, label: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM nodes WHERE id = ? AND label = ?", (node_id, label)
    ).fetchone()
    return row is not None


def _choose(kind: str, value: str, candidates: list[str]) -> str:
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise BriefingError(f"{kind} não encontrado(a): {value}")
    shown = "\n".join(f"- {c}" for c in candidates[:MAX_CANDIDATES])
    more = f"\n(+{len(candidates) - MAX_CANDIDATES})" if len(candidates) > MAX_CANDIDATES else ""
    raise BriefingError(
        f"mais de um(a) {kind} casa com {value!r}; use o identificador completo:\n{shown}{more}"
    )


def resolve_screens(conn: sqlite3.Connection, value: str) -> list[str]:
    """Os arquivos (`File:…`) da tela: um caminho, um ID, um item de menu (`BusinessAction:menu:…`)
    ou um trecho do caminho de menu ("Emissão Geral de IPTU")."""
    text = value.strip()
    if not text:
        raise BriefingError("informe a tela")
    if text.startswith("BusinessAction:"):
        rows = conn.execute(
            "SELECT DISTINCT src FROM edges WHERE dst = ? AND type = 'IMPLEMENTS_ACTION'"
            " AND src LIKE 'File:%' ORDER BY src",
            (text,),
        ).fetchall()
        if not rows:
            raise BriefingError(f"o item de menu {text} não abre um arquivo conhecido")
        return [r[0] for r in rows]
    if text.startswith("File:"):
        if not _exists(conn, text, "File"):
            raise BriefingError(f"arquivo não encontrado: {text}")
        return [text]
    match = menus.resolve_file(conn, text)
    if match.file:
        return [match.file]
    if match.candidates:
        _choose("arquivo", text, list(match.candidates))
    rows = conn.execute(
        "SELECT id, json_extract(attrs, '$.menu_path') FROM nodes WHERE label = 'BusinessAction'"
        " AND json_extract(attrs, '$.menu_path') LIKE ? ESCAPE '\\' ORDER BY id LIMIT 12",
        (f"%{_like(text)}%",),
    ).fetchall()
    if len(rows) == 1:
        return resolve_screens(conn, rows[0][0])
    if rows:
        shown = [f"{r[0]} — {r[1]}" for r in rows]
        _choose("item de menu", text, shown)
    raise BriefingError(f"tela não encontrada: {text}")


def resolve_table(conn: sqlite3.Connection, value: str) -> str:
    text = value.strip()
    if not text:
        raise BriefingError("informe a tabela")
    if text.startswith("Table:"):
        if _exists(conn, text, "Table"):
            return text
        raise BriefingError(f"tabela não encontrada: {text}")
    if "." in text:
        candidate = f"Table:{text}"
        if _exists(conn, candidate, "Table"):
            return candidate
        raise BriefingError(f"tabela não encontrada: {text}")
    rows = conn.execute(
        "SELECT id FROM nodes WHERE label = 'Table' AND id LIKE ? ESCAPE '\\' ORDER BY id",
        (f"Table:%.{_like(text)}",),
    ).fetchall()
    return _choose("tabela", text, [r[0] for r in rows])


def resolve_module(conn: sqlite3.Connection, value: str) -> str:
    text = value.strip()
    if not text:
        raise BriefingError("informe o módulo")
    candidate = text if text.startswith("Module:") else f"Module:{text.lower()}"
    if _exists(conn, candidate, "Module"):
        return candidate
    rows = conn.execute("SELECT id, attrs FROM nodes WHERE label = 'Module' ORDER BY id").fetchall()
    wanted = text.lower()
    by_name = [
        r[0]
        for r in rows
        if str(json.loads(r[1]).get("name", "")).lower() == wanted
        or r[0].lower() == f"module:{wanted}"
    ]
    if by_name:
        return _choose("módulo", text, by_name)
    names = [r[0].partition(":")[2] for r in rows]
    close = difflib.get_close_matches(text.lower(), [n.lower() for n in names], n=3)
    hint = f" Você quis dizer: {', '.join(close)}?" if close else ""
    if not rows:
        raise BriefingError("este mapa não tem módulos (scan sem o catálogo do banco)")
    raise BriefingError(f"módulo não encontrado: {text}.{hint}")


def resolve_endpoint(conn: sqlite3.Connection, value: str) -> str:
    text = value.strip()
    if not text:
        raise BriefingError("informe o endpoint")
    if text.startswith("Endpoint:"):
        if _exists(conn, text, "Endpoint"):
            return text
        raise BriefingError(f"endpoint não encontrado: {text}")
    rows = conn.execute(
        "SELECT id FROM nodes WHERE label = 'Endpoint' AND id LIKE ? ESCAPE '\\'"
        " ORDER BY id LIMIT 20",
        (f"Endpoint:%:{_like(text)}",),
    ).fetchall()
    return _choose("endpoint", text, [r[0] for r in rows])


def resolve_routine(conn: sqlite3.Connection, value: str) -> str:
    """Função da base: `PgFunction:…`, `schema.nome` ou só o nome (várias assinaturas → lista)."""
    text = value.strip()
    if not text:
        raise BriefingError("informe a função")
    if text.startswith("PgFunction:"):
        if _exists(conn, text, "PgFunction"):
            return text
        raise BriefingError(f"função não encontrada: {text}")
    pattern = f"PgFunction:{_like(text)}(%" if "." in text else f"PgFunction:%.{_like(text)}(%"
    rows = conn.execute(
        "SELECT id FROM nodes WHERE label = 'PgFunction' AND id LIKE ? ESCAPE '\\' ORDER BY id",
        (pattern,),
    ).fetchall()
    return _choose("função", text, [r[0] for r in rows])


def resolve_column(conn: sqlite3.Connection, value: str) -> str:
    """`schema.tabela.coluna` ou `tabela.coluna` → `Table:schema.nome#coluna`."""
    table_part, _sep, column = value.strip().rpartition(".")
    if not table_part or not column:
        raise BriefingError("informe a coluna como `tabela.coluna` ou `schema.tabela.coluna`")
    table = resolve_table(conn, table_part)
    row = conn.execute("SELECT attrs FROM nodes WHERE id = ?", (table,)).fetchone()
    attrs: dict[str, Any] = json.loads(row[0]) if row else {}
    columns = [str(c) for c in attrs.get("columns") or []]
    if column.lower() not in [c.lower() for c in columns]:
        close = difflib.get_close_matches(column.lower(), [c.lower() for c in columns], n=3)
        hint = f" Parecidas: {', '.join(close)}." if close else ""
        raise BriefingError(f"a tabela {table.partition(':')[2]} não tem a coluna {column}.{hint}")
    return f"{table}#{column.lower()}"


def resolver(kind: str) -> Any:
    return {
        "tela": resolve_screens,
        "tabela": resolve_table,
        "modulo": resolve_module,
        "endpoint": resolve_endpoint,
        "funcao": resolve_routine,
        "coluna": resolve_column,
    }[kind]
