"""Ficha da tela e da função em português, em Markdown.

Junta o que o mapa sabe de uma tela: menu e módulo, dados gravados e lidos (com os rótulos do
dicionário de dados), funções da base chamadas, mensagens de validação e erro, anotações do autor.
Tudo **determinístico**: a da tela vem da ficha de regras da interface
(`web.rules.business_rules`); a da função vem de `routine_sources`/`routine_rules`/
`routine_profile` e `routine_queries`/`routine_fields` e da origem. Sem IA. O mesmo mapa e os
mesmos parâmetros dão o mesmo Markdown (sem a data de geração).
"""

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from php_map.analysis import routine_origin
from php_map.errors import QueryError
from php_map.graph import routines as routine_store
from php_map.web import repo, rules

MAX_TABLES = 20
MAX_COLUMNS = 8
MAX_MESSAGES = 40
MAX_NOTES = 15
MAX_FUNCTIONS = 30
MAX_RULES = 60
MAX_ENTRIES = 10
MAX_TEXT = 240
DEFAULT_SOURCE_LINES = 120
_CATEGORY_ORDER = {"erro": 0, "validação": 1, "mensagem": 2}
_SPACE = re.compile(r"\s+")
# comentário que é código desativado, não anotação do autor
_CODE = re.compile(r"\$\w+|->|;\s*\w|\bfunction\b|\bforeach\b|\bif\s*\(|set_time_limit|\w+\(\)")

# Tradução pequena e fixa: tipo sem entrada aqui aparece
# como está, sem inventar.
TYPE_PT = {
    "int2": "número inteiro",
    "int4": "número inteiro",
    "int8": "número inteiro",
    "numeric": "número decimal",
    "float4": "número decimal",
    "float8": "número decimal",
    "money": "valor em dinheiro",
    "date": "data",
    "timestamp": "data e hora",
    "timestamptz": "data e hora com fuso",
    "time": "hora",
    "text": "texto",
    "varchar": "texto",
    "bpchar": "texto",
    "char": "texto",
    "name": "texto",
    "bool": "verdadeiro/falso",
    "boolean": "verdadeiro/falso",
    "void": "nada (procedimento)",
    "trigger": "gatilho (trigger)",
    "record": "registro",
    "json": "json estruturado",
    "jsonb": "json estruturado",
}


def type_pt(type_raw: str) -> str:
    """Tradução curta de um tipo PL/pgSQL; tipo sem entrada na tabela aparece como está."""
    base = type_raw[:-2] if type_raw.endswith("[]") else type_raw
    label = TYPE_PT.get(base, base)
    return f"lista de {label}" if type_raw.endswith("[]") else label


@dataclass(frozen=True, slots=True)
class TableLine:
    table: str  # Table:schema.nome
    technical: str  # schema.nome
    label: str | None  # rótulo do dicionário
    description: str | None
    operations: tuple[str, ...]
    columns: tuple[tuple[str, str | None], ...]  # (coluna, rótulo ou None)


@dataclass(frozen=True, slots=True)
class MessageLine:
    text: str
    category: str
    file: str
    line: int


@dataclass(frozen=True, slots=True)
class FunctionCall:
    name: str  # schema.nome(args)
    resolved: bool
    path: str
    line: int | None
    error_rules: tuple[str, ...]  # mensagens de validação/erro da função, até 3
    card_id: str | None  # ID do nó, para o link da ficha (`None` se não resolvida)


@dataclass(frozen=True, slots=True)
class ScreenCard:
    entry: str  # File:<caminho>
    path: str
    title: str
    modules: tuple[str, ...]
    menus: tuple[str, ...]  # todos os caminhos de menu
    help: str | None
    technical_description: str | None
    actions: tuple[str, ...]
    writes: tuple[TableLine, ...]
    reads: tuple[TableLine, ...]
    functions: tuple[FunctionCall, ...]
    triggers: tuple[tuple[str, str], ...]  # (tabela, função)
    messages: tuple[MessageLine, ...]
    notes: tuple[MessageLine, ...]
    depth: int
    truncated: bool
    warnings: tuple[str, ...]
    scanned_at: str


def one_line(text: str, limit: int = MAX_TEXT) -> str:
    """Texto em uma linha só, sem quebra nem espaço repetido, cortado em `limit`."""
    flat = _SPACE.sub(" ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _cell(text: str) -> str:
    return one_line(text).replace("|", "\\|")


def _table_line(use: rules.TableUse) -> TableLine:
    catalog = use.catalog or {}
    return TableLine(
        table=use.table,
        technical=use.table.partition(":")[2],
        label=catalog.get("label") if isinstance(catalog.get("label"), str) else None,
        description=catalog.get("description")
        if isinstance(catalog.get("description"), str)
        else None,
        operations=tuple(use.operations),
        columns=tuple((c.column, c.label) for c in use.columns_labeled if c.label),
    )


def _error_rules(conn: sqlite3.Connection, routine_id: str, limit: int = 3) -> tuple[str, ...]:
    found = routine_store.read_rules(conn, routine_id, kinds=("validation", "error"), limit=limit)
    if found is None:
        return ()
    return tuple(one_line(i["message"]) for i in found["items"] if i["message"])


def _functions(
    conn: sqlite3.Connection, found: rules.BusinessRules, warnings: list[str]
) -> tuple[FunctionCall, ...]:
    calls = [
        FunctionCall(
            r.name or r.function.partition(":")[2],
            r.resolved,
            r.evidence.path,
            r.evidence.line,
            _error_rules(conn, r.function) if r.resolved else (),
            r.function if r.resolved else None,
        )
        for r in found.rpc
    ]
    if len(calls) > MAX_FUNCTIONS:
        warnings.append(f"funções da base: mostrando {MAX_FUNCTIONS} de {len(calls)}")
    return tuple(calls[:MAX_FUNCTIONS])


def screen_card(
    conn: sqlite3.Connection,
    entry: str,
    *,
    depth: int = rules.DEFAULT_DEPTH,
    min_confidence: float = rules.DEFAULT_MIN_CONFIDENCE,
) -> ScreenCard:
    """A ficha da tela `entry` (`emp4_x.php` ou `File:emp4_x.php`). `QueryError` se não existe."""
    found = rules.business_rules(conn, entry, depth=depth, min_confidence=min_confidence)
    warnings: list[str] = list(found.clamped)
    path = found.entry.partition(":")[2]
    menus = [m for m in found.menus if m.path]
    main = menus[0] if menus else None
    paths = tuple(dict.fromkeys(p for m in menus for p in ([m.path or ""] + m.paths) if p))
    # as mensagens do código mais próximo da tela vêm primeiro; as de infraestrutura, depois
    depth_of = {p.node: p.depth for p in found.participants}

    def closeness(m: rules.Text) -> tuple[int, int, str, int]:
        depth = depth_of.get(rules.scope_node(m.file, m.scope), 99)
        return (depth, _CATEGORY_ORDER.get(m.category or "mensagem", 2), m.file, m.line)

    messages = sorted(found.messages, key=closeness)
    author_notes = [n for n in found.notes if not _CODE.search(n.text)]
    if len(messages) > MAX_MESSAGES:
        warnings.append(f"mensagens: mostrando {MAX_MESSAGES} de {len(messages)}")
    if len(author_notes) > MAX_NOTES:
        warnings.append(f"anotações do autor: mostrando {MAX_NOTES} de {len(author_notes)}")
    for label, items in (("tabelas gravadas", found.writes), ("tabelas lidas", found.reads)):
        if len(items) > MAX_TABLES:
            warnings.append(f"{label}: mostrando {MAX_TABLES} de {len(items)}")
    if found.stats.truncated:
        warnings.append(
            "o caminho da tela foi cortado por limite: aumente a profundidade com cuidado"
        )
    return ScreenCard(
        entry=found.entry,
        path=path,
        title=(main.path if main and main.path else path),
        modules=tuple(str(m.get("name") or m.get("module")) for m in found.modules),
        menus=paths,
        help=main.help if main else None,
        technical_description=main.desctec if main else None,
        actions=tuple(
            f"{a.file}: {', '.join(a.cases[:12])}" + (" …" if len(a.cases) > 12 else "")
            for a in found.actions
        ),
        writes=tuple(_table_line(u) for u in found.writes[:MAX_TABLES]),
        reads=tuple(_table_line(u) for u in found.reads[:MAX_TABLES]),
        functions=_functions(conn, found, warnings),
        triggers=tuple(
            (t.table.partition(":")[2], t.function.partition(":")[2]) for t in found.triggers
        ),
        messages=tuple(
            MessageLine(m.text, m.category or "mensagem", m.file, m.line)
            for m in messages[:MAX_MESSAGES]
        ),
        notes=tuple(
            MessageLine(n.text, "anotação", n.file, n.line) for n in author_notes[:MAX_NOTES]
        ),
        depth=found.depth,
        truncated=found.stats.truncated,
        warnings=tuple(warnings),
        scanned_at=_scanned_at(conn),
    )


def _scanned_at(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = 'scanned_at'").fetchone()
    return str(row[0]) if row else ""


# --------------------------------------------------------------------------- Markdown


def _tables_markdown(items: tuple[TableLine, ...], heading: str, empty: str) -> list[str]:
    lines = [heading, ""]
    if not items:
        return [*lines, empty, ""]
    lines += [
        "| Tabela (dicionário) | Tabela técnica | Operações | Campos |",
        "|---|---|---|---|",
    ]
    for t in items:
        columns = "; ".join(
            f"{label} (`{column}`)" if label else f"`{column}`"
            for column, label in t.columns[:MAX_COLUMNS]
        )
        if len(t.columns) > MAX_COLUMNS:
            columns += f"; … (+{len(t.columns) - MAX_COLUMNS})"
        lines.append(
            f"| {_cell(t.label or '(sem rótulo no dicionário)')} | `{t.technical}` "
            f"| {', '.join(t.operations)} | {_cell(columns) or '—'} |"
        )
    return [*lines, ""]


def render_screen_markdown(card: ScreenCard, level: int = 2) -> str:
    """O Markdown da ficha. `level` é o nível do título (os blocos entram sob `# Contexto`)."""
    h1, h2 = "#" * level, "#" * (level + 1)
    out: list[str] = [f"{h1} Tela: {card.title}", ""]
    out.append(f"- **Arquivo:** `{card.path}`")
    if card.modules:
        out.append(f"- **Módulo:** {', '.join(card.modules)}")
    if card.menus:
        out.append(f"- **Menu:** {card.menus[0]}")
        out += [f"- **Também em:** {m}" for m in card.menus[1:4]]
    if card.help:
        out.append(f"- **O que o menu diz:** {one_line(card.help, 400)}")
    if card.technical_description:
        out.append(f"- **Descrição técnica do menu:** {one_line(card.technical_description, 400)}")
    for action in card.actions:
        out.append(f"- **Ações despachadas:** {action}")
    out.append("")
    out += _tables_markdown(
        card.writes, f"{h2} O que a tela grava", "A tela não grava tabelas conhecidas."
    )
    out += _tables_markdown(
        card.reads, f"{h2} O que a tela lê", "A tela não lê tabelas conhecidas."
    )
    out += [f"{h2} Funções da base chamadas", ""]
    if card.functions:
        for f in card.functions:
            line = (
                f"- `{f.name}`"
                + ("" if f.resolved else " (não está no schema)")
                + f" — chamada em `{f.path}:{f.line}`"
            )
            if f.error_rules:
                line += "; valida: " + "; ".join(f"«{m}»" for m in f.error_rules)
            out.append(line)
    else:
        out.append("A tela não chama funções da base conhecidas.")
    out.append("")
    out += [f"{h2} Mensagens de validação e erro", ""]
    if card.messages:
        out += [
            f"- **{m.category}** «{one_line(m.text)}» — `{m.file}:{m.line}`" for m in card.messages
        ]
    else:
        out.append("Nenhuma mensagem de validação ou erro encontrada no código da tela.")
    out.append("")
    if card.notes:
        out += [f"{h2} Anotações do autor no código", ""]
        out += [f"- «{one_line(n.text)}» — `{n.file}:{n.line}`" for n in card.notes]
        out.append("")
    if card.triggers:
        out += [f"{h2} Gatilhos (triggers) das tabelas gravadas", ""]
        out += [f"- `{table}` dispara `{function}`" for table, function in card.triggers]
        out.append("")
    notes = [f"profundidade {card.depth}", f"mapa de {card.scanned_at}"] + list(card.warnings)
    out.append("_" + "; ".join(notes) + "._")
    return "\n".join(out).rstrip() + "\n"


def as_dict(card: ScreenCard) -> dict[str, Any]:
    """A ficha como dicionário (para a API e o MCP)."""
    from dataclasses import asdict

    return asdict(card)


# --------------------------------------------------------------------------- ficha da função


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    mode: str  # IN | OUT | INOUT | VARIADIC
    type_raw: str
    type_pt: str
    default: str | None


@dataclass(frozen=True, slots=True)
class RuleLine:
    kind: str  # validation | error | warning
    level: str | None
    condition: str | None
    message: str | None
    params: tuple[str, ...]
    line: int


@dataclass(frozen=True, slots=True)
class OriginEntry:
    kind: str  # menu | route | endpoint | file
    target: str
    label: str | None
    distance: int
    confidence: float


@dataclass(frozen=True, slots=True)
class Origin:
    status: str  # com_origem | so_arquivo | gerada | sem_chamador | cadeia_sem_origem
    reason: str | None
    module: str | None
    module_confidence: float | None
    entries: tuple[OriginEntry, ...]


@dataclass(frozen=True, slots=True)
class Callers:
    php: int
    functions: int
    triggers: int


@dataclass(frozen=True, slots=True)
class SourceExcerpt:
    lines: tuple[str, ...]  # "N: texto", já numeradas
    total_lines: int
    shown: int


@dataclass(frozen=True, slots=True)
class FunctionCard:
    id: str
    schema: str
    name: str
    signature: str  # schema.nome(tipo, tipo…)
    kind: str  # function | trigger | generated
    purpose: tuple[str, ...]  # comentários do autor, até 3
    origin: Origin | None  # None em grafo antigo, sem origem das funções
    params: tuple[Param, ...]
    returns: str
    returns_pt: str
    volatility: str
    tables: tuple[TableLine, ...]  # com rótulo e colunas por papel
    rules: tuple[RuleLine, ...]  # validação/erro/aviso
    calls: tuple[str, ...]  # funções da base que esta chama
    called_by: Callers
    source: SourceExcerpt
    read_at: str
    scanned_at: str
    warnings: tuple[str, ...]


def _callers(conn: sqlite3.Connection, node_id: str) -> Callers:
    counts = {"php": 0, "functions": 0, "triggers": 0}
    php_labels = ("File", "Function", "Method")
    for label, total in conn.execute(
        "SELECT substr(src, 1, instr(src, ':') - 1), count(DISTINCT src) FROM edges "
        "WHERE dst = ? AND type IN ('CALLS_RPC', 'CALLS', 'TRIGGERS') GROUP BY 1",
        (node_id,),
    ):
        if label in php_labels:
            counts["php"] += total
        elif label == "PgFunction":
            counts["functions"] += total
        elif label == "Table":
            counts["triggers"] += total
    return Callers(**counts)


def _calls(conn: sqlite3.Connection, node_id: str) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT DISTINCT dst FROM edges WHERE src = ? AND type = 'CALLS_RPC' ORDER BY dst",
        (node_id,),
    ).fetchall()
    return tuple(key_of(str(r[0])) for r in rows)


def key_of(node_id: str) -> str:
    return node_id.partition(":")[2]


def _origin(conn: sqlite3.Connection, node_id: str, warnings: list[str]) -> Origin | None:
    found = routine_origin.read_origin(conn, node_id)
    if found is None:
        return None
    entries = found["entries"]
    if len(entries) > MAX_ENTRIES:
        warnings.append(f"origem: mostrando {MAX_ENTRIES} de {len(entries)} entradas")
    return Origin(
        status=found["status"],
        reason=found["reason"],
        module=found["module"],
        module_confidence=found["module_confidence"],
        entries=tuple(
            OriginEntry(e["kind"], e["target"], e["label"], e["distance"], e["confidence"])
            for e in entries[:MAX_ENTRIES]
        ),
    )


def _rules(conn: sqlite3.Connection, node_id: str, warnings: list[str]) -> tuple[RuleLine, ...]:
    found = routine_store.read_rules(
        conn, node_id, kinds=("validation", "error", "warning"), limit=MAX_RULES
    )
    if found is None:
        return ()
    if found["total"] > MAX_RULES:
        warnings.append(f"regras: mostrando {MAX_RULES} de {found['total']}")
    return tuple(
        RuleLine(i["kind"], i["level"], i["condition"], i["message"], tuple(i["params"]), i["line"])
        for i in found["items"]
    )


def _purpose(conn: sqlite3.Connection, node_id: str) -> tuple[str, ...]:
    found = routine_store.read_rules(conn, node_id, kinds=("comment",), limit=3)
    if found is None:
        return ()
    return tuple(one_line(i["message"]) for i in found["items"] if i["message"])


def _function_tables(
    conn: sqlite3.Connection, node_id: str, warnings: list[str]
) -> tuple[TableLine, ...]:
    found = routine_store.read_queries(conn, node_id, limit=1000)
    if found is None:
        return ()
    if found["total"] > 1000:
        warnings.append(f"consultas: mostrando 1000 de {found['total']}")
    by_table: dict[str, dict[str, Any]] = {}
    for q in found["items"]:
        for f in q["fields"]:
            entry = by_table.setdefault(f["table"], {"roles": set(), "columns": set()})
            entry["roles"].add(f["role"])
            if f["column"] != "*":
                entry["columns"].add(f["column"])
    lines: list[TableLine] = []
    for table_id in sorted(by_table):
        info = by_table[table_id]
        node = repo.node(conn, table_id)
        attrs = node.attrs if node else {}
        catalog = attrs.get("catalog") or {}
        columns_catalog = attrs.get("columns_catalog") or {}
        columns = tuple(
            (col, (columns_catalog.get(col) or {}).get("label")) for col in sorted(info["columns"])
        )
        if len(columns) > MAX_COLUMNS:
            warnings.append(
                f"colunas de {key_of(table_id)}: mostrando {MAX_COLUMNS} de {len(columns)}"
            )
        lines.append(
            TableLine(
                table=table_id,
                technical=key_of(table_id),
                label=catalog.get("label") if isinstance(catalog.get("label"), str) else None,
                description=catalog.get("description")
                if isinstance(catalog.get("description"), str)
                else None,
                operations=tuple(sorted(info["roles"])),
                columns=columns[:MAX_COLUMNS],
            )
        )
    if len(lines) > MAX_TABLES:
        warnings.append(f"tabelas: mostrando {MAX_TABLES} de {len(lines)}")
    return tuple(lines[:MAX_TABLES])


def _params(attrs: dict[str, Any], profile: dict[str, Any] | None) -> tuple[Param, ...]:
    if profile is not None and profile.get("params"):
        return tuple(
            Param(
                name=str(p.get("name") or ""),
                mode=str(p.get("mode") or "IN"),
                type_raw=str(p.get("type") or ""),
                type_pt=type_pt(str(p.get("type") or "")),
                default=p.get("default"),
            )
            for p in profile["params"]
        )
    # Sem a leitura de consultas: só os tipos, sem nome nem modo (o que o nó `PgFunction` já tem).
    return tuple(Param("", "IN", str(t), type_pt(str(t)), None) for t in attrs.get("args") or ())


def function_card(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    source_lines: int = DEFAULT_SOURCE_LINES,
) -> FunctionCard:
    """A ficha da função `node_id` (`PgFunction:<schema>.<nome>(<tipos>)`). `QueryError` se não
    existe ou se continuar ambíguo (chame com o ID completo de uma delas). Nome com
    várias assinaturas que só repassam para uma (`CALLS_RPC` interna à família) resolve sozinho
    para o destino, com o aviso em `warnings`."""
    row = routine_store.read_routine(conn, node_id)
    warnings: list[str] = []
    if row is None:
        resolution = routine_store.resolve_family(conn, node_id)
        if resolution.chosen is not None:
            row = resolution.chosen
            node_id = row.node_id
            if resolution.resolved_by == "repasse":
                warnings.append(routine_store.describe_repasse(resolution))
        elif resolution.resolved_by == "nenhum":
            hints = routine_store.suggest_names(conn, node_id)
            more = f" Parecidas: {', '.join(hints)}." if hints else ""
            raise QueryError(f"função inexistente: {node_id}.{more}")
        else:
            ids = [c.node_id for c in resolution.candidates]
            raise QueryError(
                f"mais de uma função casa com {node_id!r}; use o id completo de uma delas: "
                f"{', '.join(ids[:10])}"
            )
    node = repo.node(conn, node_id)
    attrs = node.attrs if node else {}
    profile = routine_store.read_profile(conn, node_id)
    kind = (
        "generated"
        if row.generated
        else ("trigger" if attrs.get("returns") == "trigger" else "function")
    )
    returns = str(attrs.get("returns") or (profile or {}).get("returns") or "")
    lines = routine_store.split_lines(row.ddl)
    shown = max(0, source_lines)
    numbered = tuple(f"{i + 1}: {text}" for i, text in enumerate(lines[:shown])) if shown else ()
    if shown and len(lines) > shown:
        warnings.append(f"texto: mostrando {shown} de {len(lines)} linhas")
    return FunctionCard(
        id=node_id,
        schema=row.schema,
        name=row.name,
        signature=key_of(node_id),
        kind=kind,
        purpose=_purpose(conn, node_id),
        origin=_origin(conn, node_id, warnings),
        params=_params(attrs, profile),
        returns=returns,
        returns_pt=type_pt(returns) if returns else "",
        volatility=str((profile or {}).get("volatility") or ""),
        tables=_function_tables(conn, node_id, warnings),
        rules=_rules(conn, node_id, warnings),
        calls=_calls(conn, node_id),
        called_by=_callers(conn, node_id),
        source=SourceExcerpt(numbered, len(lines), len(numbered)),
        read_at=row.read_at,
        scanned_at=_scanned_at(conn),
        warnings=tuple(warnings),
    )


_ORIGIN_STATUS_PT = {
    "com_origem": "tem origem conhecida",
    "so_arquivo": "só chega a um arquivo, sem menu/rota/endpoint",
    "gerada": "gerada pela auditoria do banco",
    "sem_chamador": "ninguém a chama no código mapeado",
    "cadeia_sem_origem": "é chamada, mas a cadeia não chega a uma origem",
}
_ORIGIN_KIND_PT = {"menu": "Menu", "route": "Rota", "endpoint": "Endpoint", "file": "Arquivo"}


def _origin_markdown(origin: Origin | None) -> list[str]:
    if origin is None:
        return ["Sem a origem das funções da base no mapa (refaça o scan)."]
    out = [_ORIGIN_STATUS_PT.get(origin.status, origin.status) + "."]
    if origin.reason:
        out.append(f"Motivo: {one_line(origin.reason)}")
    if origin.module:
        out.append(f"Módulo: {origin.module} (confiança {origin.module_confidence or 0:.2f}).")
    for e in origin.entries:
        label = f" — {one_line(e.label)}" if e.label else ""
        out.append(
            f"- {_ORIGIN_KIND_PT.get(e.kind, e.kind)}: `{e.target}`{label} "
            f"(distância {e.distance}, confiança {e.confidence:.2f})"
        )
    return out


def _params_markdown(params: tuple[Param, ...], returns: str, returns_pt: str) -> list[str]:
    out: list[str] = []
    if params:
        out += ["| Parâmetro | Modo | Tipo | Padrão |", "|---|---|---|---|"]
        out += [
            f"| {_cell(p.name) or '—'} | {p.mode} | {p.type_pt} (`{p.type_raw}`) "
            f"| {_cell(p.default) if p.default else '—'} |"
            for p in params
        ]
    else:
        out.append("A função não recebe parâmetro.")
    out.append("")
    out.append(f"**Devolve:** {returns_pt} (`{returns}`)." if returns else "**Devolve:** nada.")
    return out


def _rules_markdown(rules_: tuple[RuleLine, ...]) -> list[str]:
    errors = [r for r in rules_ if r.kind in ("validation", "error")]
    warns = [r for r in rules_ if r.kind == "warning"]
    out: list[str] = []
    for heading, items in (("Erros (bloqueiam)", errors), ("Avisos", warns)):
        out.append(f"**{heading}:**")
        out.append("")
        if not items:
            out.append("Nenhum encontrado no texto desta função.")
            out.append("")
            continue
        for r in items:
            condition = f"Se `{r.condition}`, " if r.condition else ""
            message = (
                f"a mensagem é: «{one_line(r.message or '')}»" if r.message else "sem mensagem."
            )
            params = f" (`%` = {', '.join(r.params)})" if r.params else ""
            out.append(f"- {condition}{message}{params} — linha {r.line}")
        out.append("")
    return out


def render_function_markdown(card: FunctionCard) -> str:
    """O Markdown da ficha da função. Nenhuma seção some em silêncio: sem dado, mostra o motivo."""
    out = [f"# {card.signature} (função da base, schema {card.schema})", ""]
    kind_pt = {
        "trigger": "função de gatilho (trigger)",
        "generated": "gerada pela auditoria do banco",
    }
    if card.kind in kind_pt:
        out.append(f"_{kind_pt[card.kind]}._")
        out.append("")
    out.append(f"_Mapa de {card.scanned_at}; texto lido em {card.read_at}._")
    out.append("")
    out += ["## Para que serve (pelo que o autor escreveu)", ""]
    if card.purpose:
        out += [f"- {p}" for p in card.purpose]
    else:
        out.append("A função não tem comentário do autor.")
    out.append("")
    out += ["## Onde é usada", "", *_origin_markdown(card.origin), ""]
    out += [
        "## O que recebe e devolve",
        "",
        *_params_markdown(card.params, card.returns, card.returns_pt),
        "",
    ]
    out += _tables_markdown(card.tables, "## Dados que usa", "A função não usa tabela conhecida.")
    out += ["## O que ela valida", "", *_rules_markdown(card.rules)]
    out += ["## O que ela chama e quem a chama", ""]
    if card.calls:
        out += [f"- `{c}`" for c in card.calls]
    else:
        out.append("Não chama nenhuma outra função da base.")
    out.append(
        f"\nChamada por {card.called_by.php} arquivo(s)/método(s) PHP, "
        f"{card.called_by.functions} outra(s) função(ões) da base e "
        f"{card.called_by.triggers} tabela(s) por gatilho."
    )
    out.append("")
    out += ["## Texto original", ""]
    if card.source.lines:
        out += ["```sql", *card.source.lines, "```"]
        if card.source.shown < card.source.total_lines:
            out.append(
                f"\n_Mostrando {card.source.shown} de {card.source.total_lines} linhas; "
                "o texto completo está em `routine` no MCP._"
            )
    else:
        out.append(f"Texto com {card.source.total_lines} linha(s); veja em `routine` no MCP.")
    out.append("")
    if card.warnings:
        out.append("_" + "; ".join(card.warnings) + "._")
    return "\n".join(out).rstrip() + "\n"


def function_as_dict(card: FunctionCard) -> dict[str, Any]:
    """A ficha da função como dicionário (para a API e o MCP)."""
    from dataclasses import asdict

    return asdict(card)
