"""Blocos dos briefings: pedaços de Markdown que o sistema preenche **sem IA**.

Cada bloco tem nome, descrição, parâmetros tipados e uma função que devolve Markdown a partir do
grafo (somente leitura). O registro `BLOCKS` é a fonte da validação dos modelos **e** do bloco
`catalogo_de_blocos`, que lista para o assistente de IA os blocos reais (por isso ele nunca
inventa um).

Dado que o mapa não tem (grafo sem módulos, sem os textos da ficha) vira "não disponível neste
mapa" com o motivo; o modelo não quebra.
"""

import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from php_map.analysis.cross import project_label
from php_map.briefing import model, resolve
from php_map.graph import routines as routine_store
from php_map.graph import store
from php_map.mcp import tools
from php_map.mcp.catalog import ProjectCatalog
from php_map.report import cards
from php_map.search.query import search as search_query

MAX_LIST = 15
MAX_COLUMNS_TABLE = 40
_SPACE = re.compile(r"\s+")


# --------------------------------------------------------------------------- registro


@dataclass(frozen=True, slots=True)
class BlockParam:
    name: str
    type: str  # os tipos de `model.PARAM_TYPES`
    required: bool = True
    default: Any = None
    description: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BlockResult:
    markdown: str
    warnings: tuple[str, ...] = ()


Render = Callable[["BriefingContext", dict[str, Any]], BlockResult]
Check = Callable[["BriefingContext"], str | None]


@dataclass(frozen=True, slots=True)
class Block:
    name: str
    description: str
    params: tuple[BlockParam, ...]
    render: Render
    check: Check | None = None  # motivo de indisponibilidade neste mapa, ou None

    def param(self, name: str) -> BlockParam | None:
        return next((p for p in self.params if p.name == name), None)


BLOCKS: dict[str, Block] = {}


def block(
    name: str, description: str, params: Sequence[BlockParam] = (), check: Check | None = None
) -> Callable[[Render], Render]:
    def decorate(fn: Render) -> Render:
        BLOCKS[name] = Block(name, description, tuple(params), fn, check)
        return fn

    return decorate


# --------------------------------------------------------------------------- contexto


@dataclass
class BriefingContext:
    """O mapa em que o briefing é montado: uma conexão somente leitura e o que os blocos citam."""

    dbs: Sequence[Path]
    conn: sqlite3.Connection = field(init=False)
    scanned_at: str = field(init=False, default="")
    mentioned: dict[str, list[str]] = field(init=False, default_factory=dict)
    _catalog: ProjectCatalog | None = field(init=False, default=None)
    _project: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.conn = store.connect_readonly(Path(self.dbs[0]))
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'scanned_at'").fetchone()
        self.scanned_at = str(row[0]) if row else ""

    def __enter__(self) -> "BriefingContext":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    @property
    def catalog(self) -> ProjectCatalog:
        if self._catalog is None:
            self._catalog = ProjectCatalog.from_dbs([Path(p) for p in self.dbs])
        return self._catalog

    @property
    def project(self) -> str:
        if self._project is None:
            self._project = project_label(Path(self.dbs[0]), store.load_meta(Path(self.dbs[0])))[0]
        return self._project

    def has_table(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def has_modules(self) -> bool:
        if not self.has_table("analytics_modules"):
            return False
        row = self.conn.execute(
            "SELECT COUNT(*) FROM analytics_modules WHERE module != '(sem módulo)'"
        ).fetchone()
        return bool(row and row[0])

    def note_table(self, table_id: str, columns: Sequence[str] = ()) -> None:
        """Registra uma tabela e colunas citadas, para o bloco `glossario`."""
        known = self.mentioned.setdefault(table_id, [])
        known += [c for c in columns if c not in known]


# --------------------------------------------------------------------------- utilitários


def escape_markup(value: str) -> str:
    """Torna visível, e inofensivo, qualquer `{{` ou `}}` de um texto digitado pela pessoa."""
    return value.replace("{{", "{ {").replace("}}", "} }")


def short(node_id: str) -> str:
    return node_id.partition(":")[2] or node_id


def flat(text: str, limit: int = 160) -> str:
    return cards.one_line(text, limit)


def _unavailable(reason: str) -> BlockResult:
    return BlockResult(f"_Não disponível neste mapa: {reason}._", (reason,))


def _attrs(ctx: BriefingContext, node_id: str) -> dict[str, Any]:
    row = ctx.conn.execute("SELECT attrs FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return dict(json.loads(row[0])) if row else {}


def _table_label(ctx: BriefingContext, table_id: str) -> str | None:
    label = (_attrs(ctx, table_id).get("catalog") or {}).get("label")
    return label if isinstance(label, str) else None


def _need_texts(ctx: BriefingContext) -> str | None:
    if ctx.has_table("search_texts"):
        return None
    return "o banco não tem os textos da ficha (tabela `search_texts`): rode o scan de novo"


def _need_modules(ctx: BriefingContext) -> str | None:
    return (
        None
        if ctx.has_modules()
        else "o grafo não tem módulos (scan feito sem o catálogo do banco)"
    )


def _need_routine_texts(ctx: BriefingContext) -> str | None:
    if routine_store.has_routines(ctx.conn):
        return None
    return "o banco não tem o texto das funções (tabela `routine_sources`): refaça o scan"


def _need_routine_rules(ctx: BriefingContext) -> str | None:
    if routine_store.has_rules(ctx.conn):
        return None
    return "o banco não tem as regras das funções (tabela `routine_rules`): refaça o scan"


def _need_routine_fields(ctx: BriefingContext) -> str | None:
    if routine_store.has_fields(ctx.conn):
        return None
    return "o banco não tem os campos das funções (tabela `routine_fields`): refaça o scan"


def _first_screen(ctx: BriefingContext, value: str, warnings: list[str]) -> str:
    files = resolve.resolve_screens(ctx.conn, value)
    if len(files) > 1:
        warnings.append(
            f"o item de menu abre {len(files)} arquivos; usando o primeiro ({short(files[0])})"
        )
    return files[0]


def _bullets(items: Sequence[str], limit: int, noun: str, warnings: list[str]) -> list[str]:
    if len(items) > limit:
        warnings.append(f"{noun}: mostrando {limit} de {len(items)}")
    return [f"- {item}" for item in items[:limit]]


def _inline(value: Any, limit: int = 6) -> str:
    """Valor em uma linha: `chave: valor; chave: valor`, listas separadas por vírgula."""
    if isinstance(value, dict):
        return "; ".join(
            f"{key}: {_inline(item)}"
            for key, item in value.items()
            if item not in (None, "", [], {})
        )
    if isinstance(value, list):
        more = f" … (+{len(value) - limit})" if len(value) > limit else ""
        return ", ".join(_inline(item) for item in value[:limit]) + more
    return flat(str(value), 120)


def generic(value: Any, indent: int = 0, limit: int = 12) -> list[str]:
    """Dicionário ou lista como lista de marcadores, com corte e sem JSON cru."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if item in (None, "", [], {}):
                continue
            plain_list = isinstance(item, list) and not any(
                isinstance(i, (dict, list)) for i in item
            )
            if plain_list:
                lines.append(f"{pad}- **{key}:** {_inline(item, limit)}")
            elif isinstance(item, (dict, list)):
                lines.append(f"{pad}- **{key}:**")
                lines += generic(item, indent + 1, limit)
            else:
                lines.append(f"{pad}- **{key}:** {flat(str(item), 300)}")
    elif isinstance(value, list):
        for item in value[:limit]:
            if isinstance(item, dict):
                lines.append(f"{pad}- {_inline(item)}")
            elif isinstance(item, list):
                lines += generic(item, indent, limit)
            else:
                lines.append(f"{pad}- {flat(str(item), 300)}")
        if len(value) > limit:
            lines.append(f"{pad}- … (+{len(value) - limit})")
    return lines


# --------------------------------------------------------------------------- blocos de tela


@block(
    "ficha_tela",
    "Ficha da tela em português: menu e módulo, dados gravados e lidos (com os rótulos do "
    "dicionário), funções da base chamadas, mensagens de validação e erro e anotações do autor.",
    [
        BlockParam(
            "tela", "tela", description="Arquivo da tela, trecho do caminho do menu ou item de menu"
        ),
        BlockParam("profundidade", "numero", False, 4, "Profundidade do caminho (1 a 6)"),
    ],
    check=_need_texts,
)
def _ficha_tela(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    warnings: list[str] = []
    entry = _first_screen(ctx, a["tela"], warnings)
    card = cards.screen_card(ctx.conn, entry, depth=int(a["profundidade"]))
    for use in (*card.writes, *card.reads):
        ctx.note_table(use.table, [column for column, _label in use.columns])
    return BlockResult(cards.render_screen_markdown(card, 2), tuple(warnings))


@block(
    "funcoes_da_tela",
    "Os nomes das funções da base de dados que a tela chama, com as regras de erro de cada uma"
    ", e as triggers das tabelas que ela grava. Não traz o texto das funções.",
    [
        BlockParam("tela", "tela"),
        BlockParam("limite", "numero", False, 12, "Quantas funções listar"),
    ],
    check=_need_texts,
)
def _funcoes_da_tela(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    warnings: list[str] = []
    entry = _first_screen(ctx, a["tela"], warnings)
    card = cards.screen_card(ctx.conn, entry)
    limit = int(a["limite"])
    lines = ["## Funções da base usadas pela tela", ""]
    if card.functions:
        calls = []
        for f in card.functions:
            line_no = f.line if f.line is not None else "?"
            call = f"`{f.name}`" + ("" if f.resolved else " (não está no schema)")
            call += f" — chamada em `{f.path}:{line_no}`"
            if f.error_rules:
                call += "; valida: " + "; ".join(f"«{m}»" for m in f.error_rules)
            calls.append(call)
        lines += _bullets(calls, limit, "funções", warnings)
    else:
        lines.append("A tela não chama funções da base conhecidas.")
    if card.triggers:
        lines += ["", "Triggers das tabelas que a tela grava:", ""]
        lines += _bullets(
            [f"`{table}` dispara `{function}`" for table, function in card.triggers],
            limit,
            "triggers",
            warnings,
        )
    return BlockResult("\n".join(lines) + "\n", tuple(warnings))


@block(
    "trace_tela",
    "O caminho da tela até o banco: quantos arquivos e funções participam e quais tabelas alcança.",
    [
        BlockParam("tela", "tela"),
        BlockParam("profundidade", "numero", False, 4, "Profundidade (1 a 8)"),
    ],
)
def _trace_tela(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    warnings: list[str] = []
    entry = _first_screen(ctx, a["tela"], warnings)
    out = tools.trace(ctx.catalog, short(entry), ctx.project, depth=int(a["profundidade"]))
    summary = out.get("summary") or {}
    tables = list(out.get("tables") or [])
    lines = [
        "## Caminho da tela até o banco",
        "",
        f"- **Nós no caminho:** {summary.get('nodes', len(out.get('nodes') or []))}"
        f" (arquivos: {summary.get('files', '—')},"
        f" telas alcançadas: {summary.get('screens', '—')})",
        f"- **Tabelas alcançadas:** {len(tables)}",
        "",
    ]
    named = [
        f"`{short(t)}`" + (f" — {label}" if (label := _table_label(ctx, t)) else "") for t in tables
    ]
    lines += _bullets(named, 30, "tabelas", warnings)
    if out.get("truncated"):
        warnings.append("o caminho foi cortado por limite de nós")
    return BlockResult("\n".join(lines) + "\n", tuple(warnings))


# ------------------------------------------------------------------------ blocos de função


@block(
    "ficha_funcao",
    "A ficha da função da base em português: propósito pelo comentário do autor, "
    "origem (menu, rota, endpoint, arquivo), parâmetros e retorno, tabelas e campos com rótulo, "
    "validações e o texto original.",
    [
        BlockParam(
            "funcao", "funcao", description="Função da base: nome, schema.nome ou o ID completo"
        )
    ],
    check=_need_routine_texts,
)
def _ficha_funcao(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    node_id = a.get("funcao")
    if not node_id:
        return BlockResult("")
    card = cards.function_card(ctx.conn, str(node_id))
    for t in card.tables:
        ctx.note_table(t.table, [column for column, label in t.columns if label])
    return BlockResult(cards.render_function_markdown(card), card.warnings)


_RULE_KIND_PT = {
    "error": "Erros (bloqueiam)",
    "validation": "Validações",
    "warning": "Avisos",
    "decision": "Decisões",
    "handler": "Tratamento de exceção",
    "comment": "Comentários do autor",
}
_RULE_KIND_ORDER = ("error", "validation", "warning", "decision", "handler", "comment")


def _rule_bullet(item: dict[str, Any]) -> str:
    if item["kind"] == "comment":
        text = f"«{cards.one_line(item['message'] or '')}»" if item["message"] else "(sem texto)"
        return f"- {text} — linha {item['line']}"
    condition = f"Se `{item['condition']}`, " if item["condition"] else ""
    if item["message"]:
        text = f"{condition}a mensagem é: «{cards.one_line(item['message'])}»"
    elif item["condition"]:
        text = f"Condição: `{item['condition']}`"
    else:
        text = "sem condição nem mensagem"
    params = f" (`%` = {', '.join(item['params'])})" if item["params"] else ""
    return f"- {text}{params} — linha {item['line']}"


@block(
    "regras_funcao",
    "As regras de negócio da função: condição e mensagem, lado a lado, com a linha.",
    [
        BlockParam(
            "funcao", "funcao", description="Função da base: nome, schema.nome ou o ID completo"
        ),
        BlockParam(
            "niveis",
            "escolha",
            False,
            "erro_e_aviso",
            "Quais regras mostrar",
            ("erro_e_aviso", "tudo"),
        ),
    ],
    check=_need_routine_rules,
)
def _regras_funcao(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    node_id = a.get("funcao")
    if not node_id:
        return BlockResult("")
    node_id = str(node_id)
    everything = a.get("niveis") == "tudo"
    kinds = None if everything else ("validation", "error", "warning")
    found = routine_store.read_rules(ctx.conn, node_id, kinds=kinds, limit=cards.MAX_RULES)
    lines = [f"## Regras de negócio de `{short(node_id)}`", ""]
    if found is None or not found["items"]:
        lines.append("Nenhuma regra encontrada no texto desta função.")
        return BlockResult("\n".join(lines) + "\n")
    warnings: list[str] = []
    if found["total"] > cards.MAX_RULES:
        warnings.append(f"regras: mostrando {cards.MAX_RULES} de {found['total']}")
    if not everything:
        rules_ = tuple(
            cards.RuleLine(
                i["kind"], i["level"], i["condition"], i["message"], tuple(i["params"]), i["line"]
            )
            for i in found["items"]
        )
        lines += cards._rules_markdown(rules_)
    else:
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for item in found["items"]:
            by_kind.setdefault(item["kind"], []).append(item)
        for kind in _RULE_KIND_ORDER:
            items = by_kind.get(kind)
            if not items:
                continue
            lines += [f"**{_RULE_KIND_PT[kind]}:**", "", *(_rule_bullet(i) for i in items), ""]
    return BlockResult("\n".join(lines) + "\n", tuple(warnings))


@block(
    "campos_funcao",
    "Os campos que a função usa, por tabela e papel (ler, gravar, filtrar, unir, ordenar), com "
    "os rótulos do dicionário de dados.",
    [
        BlockParam(
            "funcao", "funcao", description="Função da base: nome, schema.nome ou o ID completo"
        )
    ],
    check=_need_routine_fields,
)
def _campos_funcao(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    node_id = a.get("funcao")
    if not node_id:
        return BlockResult("")
    node_id = str(node_id)
    warnings: list[str] = []
    tables = cards._function_tables(ctx.conn, node_id, warnings)
    for t in tables:
        ctx.note_table(t.table, [column for column, label in t.columns if label])
    lines = cards._tables_markdown(
        tables, f"## Campos de `{short(node_id)}`", "A função não usa tabela conhecida."
    )
    return BlockResult("\n".join(lines) + "\n", tuple(warnings))


@block(
    "consultas_funcao",
    "O texto normalizado de cada instrução SQL da função, com a linha e as tabelas que toca.",
    [
        BlockParam(
            "funcao", "funcao", description="Função da base: nome, schema.nome ou o ID completo"
        ),
        BlockParam("limite", "numero", False, 20, "Quantas consultas listar"),
    ],
    check=_need_routine_fields,
)
def _consultas_funcao(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    node_id = a.get("funcao")
    if not node_id:
        return BlockResult("")
    node_id = str(node_id)
    limit = int(a["limite"])
    found = routine_store.read_queries(ctx.conn, node_id, limit=limit)
    lines = [f"## Consultas de `{short(node_id)}`", ""]
    if found is None or not found["items"]:
        lines.append("Nenhuma consulta encontrada no texto desta função.")
        return BlockResult("\n".join(lines) + "\n")
    warnings: list[str] = []
    if found["total"] > limit:
        warnings.append(f"consultas: mostrando {limit} de {found['total']}")
    for q in found["items"]:
        tables = ", ".join(f"`{short(t)}`" for t in q["tables"]) or "nenhuma tabela reconhecida"
        text = escape_markup(flat(str(q["text"]), 300))
        lines.append(f"- **{q['kind']}** (linha {q['line']}): `{text}` — tabelas: {tables}")
    return BlockResult("\n".join(lines) + "\n", tuple(warnings))


@block(
    "origem_funcao",
    "De onde a função é chamada: menus, rotas, endpoints e arquivos, com a distância e a "
    "confiança; ou o motivo de não haver origem conhecida.",
    [
        BlockParam(
            "funcao", "funcao", description="Função da base: nome, schema.nome ou o ID completo"
        )
    ],
)
def _origem_funcao(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    node_id = a.get("funcao")
    if not node_id:
        return BlockResult("")
    node_id = str(node_id)
    warnings: list[str] = []
    origin = cards._origin(ctx.conn, node_id, warnings)
    lines = [f"## Origem de `{short(node_id)}`", "", *cards._origin_markdown(origin)]
    return BlockResult("\n".join(lines) + "\n", tuple(warnings))


_COLUMN_ROLES = ("read", "write", "filter", "join", "sort")
_ROLE_PT = {
    "read": "lê",
    "write": "grava",
    "filter": "filtra por",
    "join": "une por",
    "sort": "ordena por",
}


@block(
    "uso_coluna",
    "Quem lê, grava, filtra, une ou ordena por uma coluna de uma tabela, com a linha e o texto "
    "da consulta.",
    [
        BlockParam("tabela", "tabela"),
        BlockParam(
            "coluna",
            "texto",
            False,
            description="Nome da coluna (sem o nome da tabela); vazio pula este bloco",
        ),
        BlockParam("papel", "escolha", False, None, "Só este papel", _COLUMN_ROLES),
        BlockParam("limite", "numero", False, 30, "Quantas funções listar"),
    ],
    check=_need_routine_fields,
)
def _uso_coluna(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    column = str(a.get("coluna") or "").strip().lower()
    if not column:
        return BlockResult("")
    table_id = a["tabela"]
    role = a.get("papel") or None
    limit = int(a["limite"])
    found = routine_store.column_usage(ctx.conn, table_id, column, role=role, limit=limit)
    ctx.note_table(table_id, [column])
    label = _table_label(ctx, table_id)
    heading = f"`{short(table_id)}.{column}`" + (f" ({label})" if label else "")
    lines = [f"## Uso da coluna {heading}", ""]
    if found is None or not found["items"]:
        lines.append("Nenhum uso conhecido desta coluna nas funções da base.")
        return BlockResult("\n".join(lines) + "\n")
    warnings: list[str] = []
    if found["total"] > limit:
        warnings.append(f"usos: mostrando {limit} de {found['total']}")
    for item in found["items"]:
        role_pt = _ROLE_PT.get(item["role"], item["role"])
        text = escape_markup(flat(str(item["text"]), 200))
        lines.append(f"- `{short(item['routine_id'])}` {role_pt} — linha {item['line']}: `{text}`")
    return BlockResult("\n".join(lines) + "\n", tuple(warnings))


# --------------------------------------------------------------------------- blocos de tabela


@block(
    "tabela",
    "A tabela como o dicionário de dados a descreve: rótulo, descrição, colunas com rótulo e "
    "quantos arquivos a gravam e a leem.",
    [BlockParam("tabela", "tabela")],
)
def _tabela(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    table_id = a["tabela"]
    attrs = _attrs(ctx, table_id)
    catalog = attrs.get("catalog") or {}
    labels = attrs.get("columns_catalog") or {}
    columns = [str(c) for c in attrs.get("columns") or []]
    warnings: list[str] = []
    lines = [f"## Tabela: {catalog.get('label') or short(table_id)} (`{short(table_id)}`)", ""]
    if catalog.get("description"):
        lines += [f"- **Descrição do dicionário:** {flat(str(catalog['description']), 400)}"]
    lines += [f"- **Colunas:** {len(columns)}", ""]
    if columns:
        lines += ["| Coluna | Rótulo | Descrição |", "|---|---|---|"]
        for column in columns[:MAX_COLUMNS_TABLE]:
            info = labels.get(column) or {}
            lines.append(
                f"| `{column}` | {flat(str(info.get('label') or '—'), 80)} "
                f"| {flat(str(info.get('description') or '—'), 120).replace('|', chr(92) + '|')} |"
            )
        if len(columns) > MAX_COLUMNS_TABLE:
            warnings.append(f"colunas: mostrando {MAX_COLUMNS_TABLE} de {len(columns)}")
    ctx.note_table(
        table_id, [c for c in columns if (labels.get(c) or {}).get("label")][:MAX_COLUMNS_TABLE]
    )
    for title, kinds in (
        ("Quem grava", ("INSERTS", "UPDATES", "DELETES")),
        ("Quem lê", ("QUERIES",)),
    ):
        marks = ",".join("?" for _ in kinds)
        rows = ctx.conn.execute(
            f"SELECT DISTINCT src FROM edges WHERE dst = ? AND type IN ({marks}) ORDER BY src",  # noqa: S608
            (table_id, *kinds),
        ).fetchall()
        lines += ["", f"**{title}:** {len(rows)} nó(s)"]
        lines += _bullets([f"`{short(r[0])}`" for r in rows], 10, title.lower(), warnings)
    return BlockResult("\n".join(lines) + "\n", tuple(warnings))


@block(
    "impacto_tabela",
    "O impacto de mexer numa tabela: quem a grava, quem a lê e quantas telas dependem dela.",
    [
        BlockParam("tabela", "tabela"),
        BlockParam("profundidade", "numero", False, 6, "Profundidade (1 a 10)"),
    ],
)
def _impacto_tabela(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    out = tools.impact(ctx.catalog, a["tabela"], ctx.project, depth=int(a["profundidade"]))
    summary = out.get("summary") or {}
    warnings: list[str] = []
    lines = [
        f"## Impacto da tabela `{short(a['tabela'])}`",
        "",
        f"- **Quem grava:** {out.get('writers_total', 0)} nó(s)",
        f"- **Quem lê:** {out.get('readers_total', 0)} nó(s)",
        f"- **Telas alcançadas (até a profundidade {a['profundidade']}):**"
        f" {summary.get('screens', '—')}",
        f"- **Arquivos alcançados:** {summary.get('files', '—')}",
        "",
        "**Quem grava:**",
    ]
    lines += _bullets(
        [f"`{short(n)}`" for n in out.get("writers") or []], MAX_LIST, "escritores", warnings
    )
    lines += ["", "**Quem lê (amostra):**"]
    lines += _bullets(
        [f"`{short(n)}`" for n in out.get("readers") or []], MAX_LIST, "leitores", warnings
    )
    if out.get("truncated") or summary.get("truncated"):
        warnings.append("o impacto foi cortado por limite de nós")
    return BlockResult("\n".join(lines) + "\n", tuple(warnings))


# ------------------------------------------------------------------------ módulo e fronteira


@block(
    "modulo",
    "Resumo do módulo: tamanho, acoplamento, telas do menu e tabelas próprias.",
    [BlockParam("modulo", "modulo")],
    check=_need_modules,
)
def _modulo(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    module = a["modulo"]
    row = ctx.conn.execute(
        "SELECT name, files, screens, tables, ca, ce, instability, shared_tables"
        " FROM analytics_modules WHERE module = ?",
        (module,),
    ).fetchone()
    if row is None:
        return _unavailable(f"o módulo {short(module)} não está nos agregados do mapa")
    name, files, screens, tables, ca, ce, instability, shared = row
    warnings: list[str] = []
    lines = [
        f"## Módulo: {name or short(module)}",
        "",
        f"- **Arquivos:** {files} · **telas:** {screens} · **tabelas:** {tables}"
        f" (compartilhadas: {shared})",
        f"- **Acoplamento:** entra {ca}, sai {ce}, instabilidade {instability}",
        "",
        "**Telas do menu com mais dados gravados:**",
    ]
    screens_rows = ctx.conn.execute(
        "SELECT file_id, tables_written, tables_read FROM analytics_screens"
        " WHERE module = ? AND in_menu = 1"
        " ORDER BY tables_written DESC, reached_by DESC, file_id LIMIT ?",
        (module, MAX_LIST),
    ).fetchall()
    items = []
    for file_id, written, read in screens_rows:
        menu = ctx.conn.execute(
            "SELECT json_extract(n.attrs, '$.menu_path') FROM edges e JOIN nodes n ON n.id = e.dst"
            " WHERE e.src = ? AND e.type = 'IMPLEMENTS_ACTION' ORDER BY n.id LIMIT 1",
            (file_id,),
        ).fetchone()
        where = (
            f"{menu[0]} (`{short(file_id)}`"
            if menu and menu[0]
            else f"`{short(file_id)}` (sem menu"
        )
        items.append(f"{where}, grava {written}, lê {read})")
    lines += _bullets(items, MAX_LIST, "telas", warnings) or ["- nenhuma tela do menu neste módulo"]
    lines += ["", "**Tabelas próprias (mais usadas):**"]
    tables_rows = ctx.conn.execute(
        "SELECT table_id, writers, readers FROM analytics_tables WHERE owner_modules LIKE ?"
        " ORDER BY writers + readers DESC, table_id LIMIT ?",
        (f'%"{module}"%', MAX_LIST),
    ).fetchall()
    for table_id, writers, readers in tables_rows:
        label = _table_label(ctx, table_id)
        ctx.note_table(table_id)
        lines.append(
            f"- `{short(table_id)}`"
            + (f" — {label}" if label else "")
            + f" (grava {writers}, lê {readers})"
        )
    return BlockResult("\n".join(lines) + "\n", tuple(warnings))


@block(
    "fronteira_modulo",
    "A fronteira do módulo para o desacoplamento: tabelas próprias que outros escrevem, tabelas "
    "de outros que ele escreve, acoplamento e custo de corte.",
    [BlockParam("modulo", "modulo")],
    check=_need_modules,
)
def _fronteira_modulo(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    out = tools.module_boundary(ctx.catalog, ctx.project, short(a["modulo"]))
    name = out.get("name") or short(a["modulo"])
    shared = len(out.get("shared_tables") or [])
    lines = [
        f"## Fronteira do módulo {name}",
        "",
        f"- **Arquivos:** {out.get('files')} · **telas:** {out.get('screens')}"
        f" · **tabelas próprias:** {out.get('tables_owned')}",
        f"- **Acoplamento:** entra {out.get('ca')}, sai {out.get('ce')},"
        f" instabilidade {out.get('instability')}",
        f"- **Custo de corte:** {out.get('cost')} ({out.get('cost_formula')})",
        f"- **Tabelas compartilhadas com outros módulos:** {shared}",
        "",
    ]
    sections = (
        ("Tabelas deste módulo que outros módulos escrevem", "owned_written_by_others"),
        ("Tabelas de outros módulos que este escreve", "writes_into_others"),
        ("Quem este módulo usa (maiores acoplamentos de saída)", "edges_out"),
        ("Quem usa este módulo (maiores acoplamentos de entrada)", "edges_in"),
        ("Tabelas mais disputadas (pontos quentes)", "hotspots"),
        ("Endpoints que o módulo serve", "endpoints_served"),
    )
    for title, key in sections:
        lines.append(f"**{title}:**")
        lines += generic(out.get(key) or [], limit=10) or ["- nenhum"]
        lines.append("")
    return BlockResult("\n".join(lines) + "\n")


@block(
    "plano_de_corte",
    "O plano de corte por endpoint: cada endpoint como leitura (candidato a réplica), comando "
    "(candidato a fila ou outbox) ou indeterminado, com as tabelas que lê e escreve.",
    [
        BlockParam(
            "modulo", "modulo", False, None, "Só os endpoints servidos por arquivos deste módulo"
        ),
        BlockParam(
            "natureza",
            "escolha",
            False,
            None,
            "leitura, comando ou indeterminado",
            ("leitura", "comando", "indeterminado"),
        ),
        BlockParam("limite", "numero", False, 15, "Quantos endpoints listar"),
    ],
)
def _plano_de_corte(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    selection = "matched" if len(ctx.dbs) > 1 else "served"
    out = tools.cut_plan(
        ctx.catalog,
        ctx.project,
        natureza=a.get("natureza") or None,
        endpoints=selection,
        limit=200,
    )
    rows = list(out.get("endpoints") or [])
    module = a.get("modulo")
    if module:
        marks = ",".join("?" for _ in rows) or "NULL"
        served = (
            {
                r[0]
                for r in ctx.conn.execute(
                    "SELECT src FROM edges WHERE dst = ? AND type = 'BELONGS_TO'"
                    f" AND src IN ({marks})",  # noqa: S608
                    (module, *[f for row in rows for f in (row.get("served_by_files") or [])]),
                )
            }
            if rows
            else set()
        )
        rows = [
            r
            for r in rows
            if module in (r.get("caller_modules") or [])
            or set(r.get("served_by_files") or []) & served
        ]
    warnings: list[str] = []
    lines = [
        "## Plano de corte por endpoint",
        "",
        f"- **Contagem por natureza:** {out.get('natureza_counts')}",
        "",
    ]
    if not rows:
        lines.append("Nenhum endpoint para os filtros pedidos.")
    for row in rows[: int(a["limite"])]:
        lines.append(
            f"- `{short(str(row.get('endpoint_id')))}` ({row.get('protocol')}):"
            f" **{row.get('natureza')}**"
            + (f" — {row['motivo']}" if row.get("motivo") else "")
            + f"; lê {len(row.get('reads') or [])} tabela(s),"
            f" escreve {len(row.get('writes') or [])};"
            f" chamado {row.get('callers', 0)}x"
        )
    if len(rows) > int(a["limite"]):
        warnings.append(f"endpoints: mostrando {a['limite']} de {len(rows)}")
    warnings += [str(w) for w in out.get("warnings") or []]
    return BlockResult("\n".join(lines) + "\n", tuple(warnings))


@block(
    "endpoint",
    "Um endpoint (SOAP, RPC ou rota): parâmetros, quem o implementa, quem o chama e o que alcança.",
    [BlockParam("endpoint", "endpoint")],
)
def _endpoint(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    out = tools.endpoint(ctx.catalog, a["endpoint"], ctx.project)
    lines = [f"## Endpoint {short(a['endpoint'])}", ""] + generic(out, limit=10)
    return BlockResult("\n".join(lines) + "\n")


# ---------------------------------------------------------------------- busca, glossário, texto


@block(
    "busca",
    "Os melhores resultados da busca por texto no mapa (telas, tabelas, menus, funções), com o "
    "caminho de menu quando for um item de menu.",
    [
        BlockParam("consulta", "texto", description="Termos de negócio ou nomes técnicos"),
        BlockParam("limite", "numero", False, 8, "Quantos resultados"),
    ],
)
def _busca(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    hits = search_query(Path(ctx.dbs[0]), str(a["consulta"]), limit=int(a["limite"]), any_term=True)
    lines = [f"## O que o mapa sabe sobre «{escape_markup(flat(str(a['consulta']), 120))}»", ""]
    if not hits:
        lines.append("A busca não achou nada no mapa para estes termos.")
    for hit in hits:
        extra = ""
        if hit.label == "BusinessAction":
            menu = _attrs(ctx, hit.node_id).get("menu_path")
            extra = f" — menu: {menu}" if menu else ""
        elif hit.label == "Table":
            label = _table_label(ctx, hit.node_id)
            extra = f" — {label}" if label else ""
            ctx.note_table(hit.node_id)
        lines.append(f"- **{hit.label}** `{short(hit.node_id)}`{extra} — {flat(hit.snippet, 160)}")
    return BlockResult("\n".join(lines) + "\n")


@block(
    "glossario",
    "Os termos do dicionário de dados citados nos blocos anteriores: rótulo → tabela e coluna "
    "técnicas. Coloque depois dos blocos de contexto.",
    [BlockParam("de", "texto", False, "contexto", "Sempre `contexto`")],
)
def _glossario(ctx: BriefingContext, _a: dict[str, Any]) -> BlockResult:
    lines = ["## Glossário (termos do dicionário de dados citados acima)", ""]
    if not ctx.mentioned:
        return BlockResult(
            "\n".join(lines + ["Nenhum termo do dicionário foi citado neste contexto."]) + "\n"
        )
    lines += ["| Termo | Tabela e coluna técnicas |", "|---|---|"]
    count = 0
    warnings: list[str] = []
    for table_id in sorted(ctx.mentioned):
        attrs = _attrs(ctx, table_id)
        label = (attrs.get("catalog") or {}).get("label")
        if label:
            lines.append(f"| {label} | `{short(table_id)}` |")
        labels = attrs.get("columns_catalog") or {}
        for column in ctx.mentioned[table_id]:
            info = labels.get(column) or {}
            if info.get("label"):
                lines.append(f"| {flat(str(info['label']), 80)} | `{short(table_id)}.{column}` |")
                count += 1
        if count >= 80:
            warnings.append("glossário cortado em 80 colunas")
            break
    return BlockResult("\n".join(lines) + "\n", tuple(warnings))


@block(
    "origem_valor",
    "'De onde vem este valor?': interpreta a pergunta dentro do alcance da função "
    "e monta a cadeia até a coluna base e a tela que a cadastra (ou a hipótese pelo nome). Sem "
    "`alvo`, usa o candidato de maior placar da interpretação, com aviso.",
    [
        BlockParam("funcao", "funcao", description="Função da base para limitar o alcance"),
        BlockParam("pergunta", "texto", description="A pergunta do analista, em português"),
        BlockParam(
            "alvo",
            "texto",
            False,
            None,
            "Um ou mais `schema.tabela.coluna`, separados por vírgula (vazio = automático)",
        ),
        BlockParam("limite_palavras", "numero", False, 2000, "Teto do dossiê (500 a 6000)"),
    ],
    check=_need_routine_fields,
)
def _origem_valor(ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    from php_map.analysis import value_origin

    warnings: list[str] = []
    try:
        interpretation = value_origin.interpret(ctx.conn, str(a["pergunta"]), str(a["funcao"]))
    except value_origin.ValueOriginError as exc:
        raise model.BriefingError(str(exc)) from exc
    raw_alvo = str(a.get("alvo") or "").strip()
    targets = [t.strip() for t in raw_alvo.split(",") if t.strip()] if raw_alvo else []
    if not targets:
        if not interpretation.candidates:
            return BlockResult(
                f"## Origem do valor: {flat(interpretation.question, 160)}\n\n"
                "Nenhum candidato achado para esta pergunta dentro do alcance da função."
            )
        top = interpretation.candidates[0]
        target = f"{short(top.table)}.{top.column}" if top.column else short(top.table)
        targets = [target]
        warnings.append(f"alvo escolhido automaticamente: {target} (placar {top.score:.2f})")
    try:
        origin = value_origin.trace_value(ctx.conn, interpretation, targets)
        dossier = value_origin.render_dossier(origin, max_words=int(a["limite_palavras"]))
    except value_origin.ValueOriginError as exc:
        raise model.BriefingError(str(exc)) from exc
    for chain in origin.chains:
        for step in chain.steps:
            if step.table:
                ctx.note_table(step.table, [step.column] if step.column else [])
    return BlockResult(dossier, tuple(warnings) + origin.warnings)


@block(
    "texto_livre",
    "O texto digitado pela pessoa (a dúvida, a necessidade), sem alteração.",
    [BlockParam("texto", "texto")],
)
def _texto_livre(_ctx: BriefingContext, a: dict[str, Any]) -> BlockResult:
    return BlockResult(escape_markup(str(a["texto"]).strip()) + "\n")


FORMAT_HELP = """\
Um modelo é um arquivo Markdown com um **cabeçalho** entre `---` e um **corpo**.

Cabeçalho (só estas chaves): `id` (minúsculas, números e `-`), `titulo`, `descricao`, `versao`
(inteiro), `status` (`rascunho` para modelos novos), `autor`, `parametros`, `resposta`
(`{formato: livre}` ou `{formato: json}`) e `limite_palavras`. Cada parâmetro é uma linha
`  - {nome: tela, tipo: tela, rotulo: "Tela ou item de menu", obrigatorio: true}`; opcionais levam
`padrao`, `minimo`, `maximo`; o tipo `escolha` leva `opcoes: [a, b]`.

Tipos de parâmetro: %(types)s. Os tipos `tela`, `modulo`, `tabela`, `endpoint`, `funcao` e
`coluna` são conferidos no mapa; o formulário oferece a busca.

Corpo: instruções para a IA e **marcadores**. Só existem dois:
- `{{nome}}` — troca pelo valor do parâmetro `nome`;
- `{{bloco:nome chave=valor}}` — chama um bloco; o valor pode ser `{{parametro}}`, um número, uma
  palavra ou um texto entre aspas.
Não há lógica, laço nem código. Todo parâmetro declarado precisa ser usado, e todo marcador
precisa apontar para um parâmetro declarado ou para um bloco da lista abaixo.

Boas práticas: um modelo tem as seções `# Papel`, `# Tarefa`, `# Regras` (use SOMENTE o contexto;
se faltar, "não consta no mapa"; cite os nomes como aparecem), `# Contexto` (os blocos) e
`# Formato da resposta`. Coloque o bloco `glossario` depois dos blocos de contexto.
"""


@block(
    "catalogo_de_blocos",
    "A lista atual de blocos e parâmetros e a descrição do formato de modelo (gerada do código). "
    "Usada pelo modelo `criar-novo-modelo`.",
)
def _catalogo_de_blocos(ctx: BriefingContext, _a: dict[str, Any]) -> BlockResult:
    from php_map.briefing import engine

    lines = [
        "## Formato de um modelo",
        "",
        FORMAT_HELP % {"types": ", ".join(f"`{t}`" for t in model.PARAM_TYPES)},
        "",
    ]
    lines += ["## Blocos disponíveis", ""]
    for name in sorted(BLOCKS):
        item = BLOCKS[name]
        reason = item.check(ctx) if item.check else None
        status = (
            "disponível neste mapa"
            if reason is None
            else f"**não disponível neste mapa** ({reason})"
        )
        lines.append(f"### `{name}` — {status}")
        lines.append(item.description)
        for param in item.params:
            extra = []
            if not param.required:
                extra.append(
                    f"opcional, padrão {param.default!r}"
                    if param.default is not None
                    else "opcional"
                )
            if param.options:
                extra.append("opções: " + ", ".join(param.options))
            lines.append(
                f"- `{param.name}` ({param.type}{'; ' + '; '.join(extra) if extra else ''})"
                + (f": {param.description}" if param.description else "")
            )
        lines.append("")
    lines += ["## Exemplos de modelos oficiais", ""]
    for example in ("como-se-faz", "duvida-com-contexto"):
        lines += [
            f"### `{example}`",
            "",
            "```markdown",
            engine.official_text(example).rstrip(),
            "```",
            "",
        ]
    return BlockResult("\n".join(lines) + "\n")


def catalog(ctx: BriefingContext | None = None) -> list[dict[str, Any]]:
    """Os blocos como dados (API e MCP); com `ctx`, marca a disponibilidade neste mapa."""
    items = []
    for name in sorted(BLOCKS):
        item = BLOCKS[name]
        reason = item.check(ctx) if ctx is not None and item.check else None
        items.append(
            {
                "name": name,
                "description": item.description,
                "available": reason is None,
                "reason": reason,
                "params": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "required": p.required,
                        "default": p.default,
                        "description": p.description,
                        "options": list(p.options),
                    }
                    for p in item.params
                ],
            }
        )
    return items


__all__ = ["BLOCKS", "Block", "BlockParam", "BlockResult", "BriefingContext", "catalog"]
