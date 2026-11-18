"""Busca de campos por termo de negócio, no banco inteiro: sem IA.

O analista pergunta "onde é cadastrado o valor do metro quadrado do terreno?" sem saber o nome
técnico. A pergunta "de onde vem um valor" casa termos × rótulos do dicionário **só no alcance**
de uma função; aqui o alcance é o dicionário todo (`attrs.columns_catalog` de cada `Table`),
com a **mesma regra de placar** (`value_origin.label_terms_score`), cortada em `MIN_SCORE`:
abaixo disso é ruído (o
sinônimo `terreno`/`lote` traz `vacinas.vac_aplicalote`). Quem **é usado em cálculo** (lido por
alguma `PgFunction`, `routine_fields`) passa na frente, e cada candidato diz quem calcula com ele
e em qual menu a tabela é cadastrada (o caminho tabela → escritor → tela → menu).
"""

import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from php_map.analysis import value_origin as vo
from php_map.analysis.formulas import Resolver
from php_map.errors import QueryError
from php_map.graph import routines as routine_store

MIN_SCORE = 0.6  # abaixo disso é ruído (um dos dois termos só, por sinônimo)
DEFAULT_LIMIT = 10
MAX_LIMIT = 30
MAX_READERS_SHOWN = 5
MAX_MENUS_SHOWN = 2
MAX_RANKED = 400  # candidatos cujo uso em cálculo é medido (o resto só entra na contagem)
MENU_WINDOW = 60  # quantos candidatos recebem o menu antes do corte final (não depende do limit)
USE_BONUS = 0.1  # por uso em cálculo, proporcional a min(leitores, USE_CAP) / USE_CAP
USE_CAP = 7  # leitores a partir dos quais o bônus de uso não cresce (valor medido: 7)
MENU_BONUS = 0.05  # a tabela tem menu de cadastro
READ_ROLES = ("read", "filter", "join")
GROUP_CALC, GROUP_REGISTER, GROUP_NONE = "calculo", "cadastro", "sem_cadastro"
GROUPS = (GROUP_CALC, GROUP_REGISTER, GROUP_NONE)

# Palavras que sozinhas não dizem de que campo se fala. Uma busca só com elas não roda.
_GENERIC = frozenset(
    [
        "valor",
        "codigo",
        "data",
        "numero",
        "tipo",
        "nome",
        "descricao",
        "total",
        "quantidade",
        "qtde",
        "campo",
        "coluna",
        "tabela",
        "sequencial",
    ]
)


@dataclass(frozen=True, slots=True)
class FieldHit:
    table_id: str  # Table:cadastro.facevalor
    column: str  # j81_valorterreno
    label: str  # o nome da coluna no dicionário
    table_label: str | None
    score: float  # placar do rótulo + bônus de uso em cálculo + bônus de menu
    matched: tuple[str, ...]  # as classes de sentido do termo que o rótulo cobre
    readers: tuple[str, ...]  # até MAX_READERS_SHOWN PgFunction que leem a coluna
    readers_total: int
    writers_total: int  # nós que gravam a tabela (INSERTS/UPDATES)
    menus: tuple[str, ...]  # até MAX_MENUS_SHOWN caminhos de menu onde a tabela é cadastrada
    group: str  # calculo | cadastro | sem_cadastro
    note: str | None = None  # por que não achou o menu (quando não achou)


@dataclass(frozen=True, slots=True)
class FieldSearch:
    term: str
    terms: tuple[str, ...]
    hits: tuple[FieldHit, ...]
    total: int  # colunas com placar >= MIN_SCORE (os `hits` são as primeiras)
    more: int  # total - len(hits)
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Entry:
    table_id: str
    column: str
    label: str
    table_label: str | None
    label_terms: frozenset[str]


# O dicionário muda só com um novo scan: a chave é o arquivo e a data dele. Uma conexão em memória
# (testes) não tem arquivo e não usa cache.
_CACHE: dict[tuple[str, int], list[_Entry]] = {}
# Onde cada tabela é cadastrada (tabela → escritor → tela → menu) custa várias consultas: guarda o
# resultado pela mesma chave do dicionário.
_Cadastre = tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str | None]
_CADASTRE_CACHE: dict[tuple[str, int], dict[str, _Cadastre]] = {}


def _db_file(conn: sqlite3.Connection) -> Path | None:
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            return Path(file)
    return None


def _read_index(conn: sqlite3.Connection) -> list[_Entry]:
    entries: list[_Entry] = []
    for table_id, raw in conn.execute("SELECT id, attrs FROM nodes WHERE label = 'Table'"):
        attrs = json.loads(raw)
        catalog = attrs.get("columns_catalog")
        if not isinstance(catalog, dict) or not catalog:
            continue
        info = attrs.get("catalog") or {}
        table_label = info.get("label") if isinstance(info.get("label"), str) else None
        for column, column_info in catalog.items():
            label = (column_info or {}).get("label")
            if isinstance(label, str) and label.strip():
                terms = vo.terms_of(label)
                if terms:
                    entries.append(_Entry(table_id, column, label.strip(), table_label, terms))
    return entries


def dictionary_index(conn: sqlite3.Connection) -> list[_Entry]:
    """Todas as colunas com rótulo no dicionário. Com o banco em arquivo, a 1ª busca lê os `attrs`
    e as seguintes usam o cache (chave: caminho e `mtime`)."""
    file = _db_file(conn)
    if file is None:
        return _read_index(conn)
    try:
        key = (str(file.resolve()), file.stat().st_mtime_ns)
    except OSError:
        return _read_index(conn)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    for stale in [k for k in _CACHE if k[0] == key[0]]:
        del _CACHE[stale]
    _CACHE[key] = _read_index(conn)
    return _CACHE[key]


def clear_cache() -> None:
    _CACHE.clear()
    _CADASTRE_CACHE.clear()


def _cadastre_memo(conn: sqlite3.Connection) -> dict[str, _Cadastre]:
    """O dicionário de resultados de `cadastre` desta conexão: compartilhado entre as buscas quando
    o banco é um arquivo (chave: caminho e `mtime`), descartável quando é só memória."""
    file = _db_file(conn)
    if file is None:
        return {}
    try:
        key = (str(file.resolve()), file.stat().st_mtime_ns)
    except OSError:
        return {}
    for stale in [k for k in _CADASTRE_CACHE if k[0] == key[0] and k != key]:
        del _CADASTRE_CACHE[stale]
    return _CADASTRE_CACHE.setdefault(key, {})


def _module_tables(conn: sqlite3.Connection, module: str) -> frozenset[str]:
    if not vo._has_table(conn, "analytics_tables"):
        raise QueryError("este mapa não tem a classificação de módulo")
    wanted = module if module.startswith("Module:") else f"Module:{module}"
    rows = conn.execute(
        "SELECT table_id FROM analytics_tables, json_each(owner_modules) WHERE json_each.value = ?",
        (wanted,),
    ).fetchall()
    return frozenset(r[0] for r in rows)


def _readers(conn: sqlite3.Connection, table_id: str, column: str) -> list[str]:
    marks = ",".join("?" for _ in READ_ROLES)
    rows = conn.execute(
        "SELECT DISTINCT routine_id FROM routine_fields "  # noqa: S608
        f"WHERE table_id = ? AND column_name = ? AND role IN ({marks}) ORDER BY routine_id",
        (table_id, column, *READ_ROLES),
    ).fetchall()
    return [r[0] for r in rows]


def _writers_total(conn: sqlite3.Connection, table_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT src) FROM edges WHERE dst = ? AND type IN ('INSERTS', 'UPDATES')",
        (table_id,),
    ).fetchone()
    return int(row[0])


def _use_bonus(readers_total: int) -> float:
    return USE_BONUS * min(readers_total, USE_CAP) / USE_CAP


def field_search(
    conn: sqlite3.Connection,
    term: str,
    *,
    limit: int = DEFAULT_LIMIT,
    module: str | None = None,
) -> FieldSearch:
    """As colunas do dicionário cujo nome casa com `term`, no banco inteiro: as usadas em cálculo
    na frente. `module` (ex.: `cadastro`) limita às tabelas dele."""
    limit = max(1, min(limit, MAX_LIMIT))
    text = term.strip()
    terms = vo.terms_of(text)
    shown_terms = tuple(sorted(terms))
    if not terms:
        return FieldSearch(
            text,
            shown_terms,
            (),
            0,
            0,
            ("o termo não tem palavras úteis depois de tirar as palavras vazias (de, do, a…)",),
        )
    if terms <= _GENERIC:
        words = ", ".join(shown_terms)
        return FieldSearch(
            text,
            shown_terms,
            (),
            0,
            0,
            (
                f"termo genérico demais ({words}): diga também de quê "
                "(ex.: «valor do metro quadrado do terreno»); nenhuma busca foi feita",
            ),
        )
    allowed = _module_tables(conn, module) if module else None
    scored: list[tuple[float, _Entry, tuple[str, ...]]] = []
    for entry in dictionary_index(conn):
        if allowed is not None and entry.table_id not in allowed:
            continue
        score, matched = vo.label_terms_score(entry.label_terms, terms)
        if score >= MIN_SCORE:
            scored.append((score, entry, matched))
    if not scored:
        return FieldSearch(
            text,
            shown_terms,
            (),
            0,
            0,
            (f"nenhuma coluna do dicionário casa com o termo (placar mínimo {MIN_SCORE})",),
        )
    # 1ª ordem: o placar do rótulo; os MAX_RANKED melhores têm o uso em cálculo medido
    scored.sort(key=lambda s: (-s[0], s[1].table_id, s[1].column))
    has_fields = routine_store.has_fields(conn)
    warnings: list[str] = []
    if not has_fields:
        warnings.append("este mapa não tem os campos das funções: sem o uso em cálculo")
    ranked: list[tuple[float, _Entry, tuple[str, ...], list[str]]] = []
    for score, entry, matched in scored[:MAX_RANKED]:
        readers = _readers(conn, entry.table_id, entry.column) if has_fields else []
        ranked.append((score + _use_bonus(len(readers)), entry, matched, readers))
    ranked.sort(key=lambda r: (-r[0], r[1].table_id, r[1].column))
    # o menu de cadastro só de quem pode aparecer (a subida até a tela custa várias consultas): o
    # bônus de 0,05 pode subir um candidato da 11ª para a 5ª posição (medido em «juros de mora»,
    # `tabrecjm.k02_juros`), então os `MENU_WINDOW` primeiros do pré-placar o recebem; a janela não
    # depende do `limit`, para o top 10 ser o mesmo qualquer que seja o tamanho pedido
    resolver = Resolver(conn)
    memo = _cadastre_memo(conn)
    hits: list[FieldHit] = []
    for score, entry, matched, readers in ranked[: max(MENU_WINDOW, limit)]:
        if entry.table_id not in memo:
            memo[entry.table_id] = resolver.cadastre(entry.table_id)
        _writers, screens, menus, reason = memo[entry.table_id]
        shown_menus = menus[:MAX_MENUS_SHOWN]
        if readers:
            group = GROUP_CALC
        elif menus or screens:
            group = GROUP_REGISTER
        else:
            group = GROUP_NONE
        hits.append(
            FieldHit(
                table_id=entry.table_id,
                column=entry.column,
                label=entry.label,
                table_label=entry.table_label,
                score=round(score + (MENU_BONUS if menus else 0.0), 3),
                matched=matched,
                readers=tuple(readers[:MAX_READERS_SHOWN]),
                readers_total=len(readers),
                writers_total=0,
                menus=shown_menus,
                group=group,
                note=None if menus else reason,
            )
        )
    hits.sort(key=lambda h: (-h.score, h.table_id, h.column))
    hits = [
        replace(h, writers_total=_writers_total(conn, h.table_id))  # só dos que aparecem
        for h in hits[:limit]
    ]
    return FieldSearch(
        text, shown_terms, tuple(hits), len(scored), len(scored) - len(hits), tuple(warnings)
    )


def field_search_json(result: FieldSearch) -> dict[str, Any]:
    return dict(json.loads(json.dumps(asdict(result), ensure_ascii=False)))


_GROUP_TITLES = {
    GROUP_CALC: "Usadas em cálculo",
    GROUP_REGISTER: "Só cadastradas (nenhuma função da base lê)",
    GROUP_NONE: "Sem cadastro conhecido",
}


def routine_names(routine_ids: tuple[str, ...]) -> list[str]:
    """`fc_calcula` em vez de `public.fc_calcula(int4,...)`; a assinatura só volta quando duas
    funções da lista têm o mesmo nome."""
    bare = [vo.short(r).removeprefix("public.") for r in routine_ids]
    names = [b.partition("(")[0] for b in bare]
    return [b if names.count(n) > 1 else n for b, n in zip(bare, names, strict=True)]


def _hit_lines(position: int, hit: FieldHit) -> list[str]:
    name = f"{vo.short(hit.table_id)}.{hit.column}"
    lines = [f"{position}. `{name}` — {hit.label} (placar {hit.score:.2f})"]
    if hit.table_label:
        lines.append(f"   - tabela: {hit.table_label}")
    if hit.readers:
        more = hit.readers_total - len(hit.readers)
        shown = ", ".join(f"`{n}`" for n in routine_names(hit.readers))
        suffix = f" e mais {more}" if more > 0 else ""
        lines.append(f"   - lida por {hit.readers_total} função(ões) da base: {shown}{suffix}")
    elif hit.writers_total:
        lines.append(f"   - nenhuma função da base lê; {hit.writers_total} a gravam")
    else:
        lines.append("   - nenhuma função da base lê nem grava")
    if hit.menus:
        lines += [f"   - cadastrada em: {menu}" for menu in hit.menus]
    else:
        lines.append(
            f"   - sem menu de cadastro rastreado: {hit.note or 'tabela sem tela no mapa'}"
        )
    return lines


def render_field_search(result: FieldSearch) -> str:
    """O resultado em Markdown: os candidatos em três grupos, os de cálculo primeiro."""
    parts = [
        f"# Busca de campo: {result.term}",
        "",
        "> Gerado pelo php-map, sem IA: colunas cujo nome no dicionário de dados casa com o "
        "termo, no banco inteiro, com quem calcula com elas e onde são cadastradas. A resposta "
        "pode depender do caso (ex.: cada município tem a sua função de cálculo).",
        "",
    ]
    if result.terms:
        parts += ["Termos entendidos: " + ", ".join(result.terms), ""]
    for warning in result.warnings:
        parts += [f"**Aviso:** {warning}", ""]
    position = 0
    for group in GROUPS:
        members = [h for h in result.hits if h.group == group]
        if not members:
            continue
        parts += [f"## {_GROUP_TITLES[group]}", ""]
        for hit in members:
            position += 1
            parts += _hit_lines(position, hit)
        parts.append("")
    if result.more:
        parts += [f"_e mais {result.more} coluna(s) com placar de {MIN_SCORE:.1f} ou mais_", ""]
    if not result.hits and not result.warnings:
        parts += ["Nenhuma coluna casou.", ""]
    return "\n".join(parts).rstrip() + "\n"


__all__ = [
    "FieldHit",
    "FieldSearch",
    "clear_cache",
    "dictionary_index",
    "field_search",
    "field_search_json",
    "render_field_search",
]
