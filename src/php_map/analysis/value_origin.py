"""De onde vem um valor: pergunta guiada, sem IA.

O assistente de IA **não consulta o mapa**: ela só recebe o texto que este módulo monta
(`render_dossier`) e analisa. Por isso a interpretação da pergunta e a subida pelo código são
determinísticas aqui, e o dossiê cita sempre a linha do texto que sustenta cada afirmação.

**Alcance:** a função escolhida e as que ela chama (`CALLS_RPC`), até `--hops` níveis — nunca o
banco inteiro. **Interpretação:** termos da pergunta × rótulos do dicionário de dados, só dentro
do alcance. **Linhagem:** quando a coluna é gravada por uma instrução SQL do alcance, uma fatia
**léxica** (não a árvore do `parse_plpgsql`, que falha em funções reais como
`fc_iptu_calculavvt_mar_2011`, com campo de tipo composto) segue a variável até a instrução que a
preenche; sem variável, é a **coluna base**, e dali sobe por quem grava a tabela (PHP) até a
tela — com hipótese pelo nome do arquivo quando não há chamador conhecido.
"""

import json
import re
import sqlite3
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pglast
import pglast.visitors
from pglast import ast

from php_map.errors import QueryError
from php_map.graph import routines as routine_store
from php_map.ingest.plpgsql_rules import _function_body, _mask, _scan_spans
from php_map.secret_scan import mask, scan_text

MAX_REACH_HOPS = 4
MAX_REACH_NODES = 400
MAX_CANDIDATES = 8
MAX_HOPS_DEFAULT = 8
MAX_EXCERPT_LINES = 6
DEFAULT_MAX_WORDS = 2000
MAX_MAX_WORDS = 6000
MAX_HINT_LABELS = 40
# ponto de partida da pergunta, além de "function" (o padrão/mais preciso)
ANCHOR_KINDS = ("function", "menu", "file", "table", "module")
WIDE_REACH_WARNING = 20

_STOPWORDS = frozenset(
    [
        "de",
        "do",
        "da",
        "das",
        "dos",
        "o",
        "a",
        "os",
        "as",
        "que",
        "qual",
        "quais",
        "onde",
        "quando",
        "como",
        "vem",
        "é",
        "foi",
        "vindo",
        "vindos",
        "vem",
        "pra",
        "para",
        "por",
        "num",
        "numa",
        "nos",
        "nas",
        "e",
        "ou",
        "um",
        "uma",
        "esse",
        "essa",
        "esta",
        "este",
        "isso",
        "no",
        "na",
        "nesse",
        "nessa",
        "neste",
        "nesta",
        "em",
        "ao",
        "aos",
        "se",
        "seu",
        "sua",
        "dele",
        "dela",
        "ele",
        "ela",
    ]
)
# Equivalências fixas, sem IA: só o que a tabela lista. Acento e "²"
# já normalizam para a forma sem acento (`construção` -> `construcao`, `m²` -> `m2`): só entram
# aqui sinônimos de fato diferentes.
_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "m2": ("metro quadrado", "metros quadrados"),
    "terreno": ("lote", "territorial"),
    "construcao": ("edificacao", "predial"),
}


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _normalize(text: str) -> str:
    return _strip_accents(text.lower())


def _plain_words(text: str) -> list[str]:
    return [_normalize(w) for w in re.findall(r"[a-zA-Z0-9À-ÿ²]+", text)]


def _canonical_of(word: str) -> str:
    for canonical, synonyms in _EQUIVALENTS.items():
        if word == canonical or word in synonyms:
            return canonical
    return word


def _terms(text: str) -> frozenset[str]:
    """As **classes de sentido** de `text`: cada palavra é sua própria classe (ou a canônica de
    um sinônimo), e um sinônimo de duas palavras ("metro quadrado") só conta junto, para não
    deixar "metros" sozinho (como em "metros lineares") contar como "m2". Evita que uma palavra
    solta e comum (ex.: "valor") empate com um rótulo que casa vários conceitos da pergunta."""
    words = [w for w in _plain_words(text) if w not in _STOPWORDS and len(w) > 1]
    normalized = " " + " ".join(words) + " "
    classes = {_canonical_of(w) for w in words}
    for canonical, synonyms in _EQUIVALENTS.items():
        for phrase in synonyms:
            normalized_phrase = " ".join(_plain_words(phrase))
            if " " in normalized_phrase and f" {normalized_phrase} " in normalized:
                classes.add(canonical)
    return frozenset(classes)


terms_of = _terms  # nome público, para a busca por termo
normalize = _normalize


# --------------------------------------------------------------------------- tipos públicos


@dataclass(frozen=True, slots=True)
class Candidate:
    table: str  # Table:schema.nome
    column: str | None  # None = casou a tabela inteira (sem coluna escolhida)
    label: str | None
    matched: tuple[str, ...]
    routines: tuple[str, ...]  # funções do alcance que leem/gravam, até 3
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class Interpretation:
    question: str
    function: str | None
    suggested_functions: tuple[str, ...]
    reach: tuple[str, ...]
    candidates: tuple[Candidate, ...]
    warnings: tuple[str, ...]
    anchor_kind: str = "function"


@dataclass(frozen=True, slots=True)
class Step:
    # select_into | assign | write | base_column | writer_dao | screen | menu | hypothesis | stop
    kind: str
    text: str
    node: str | None
    table: str | None
    column: str | None
    routine: str | None
    line: int | None
    excerpt: tuple[str, ...]
    confidence: float
    # routine_fields | lexical_slice | statement_tables | graph_edge | name_hypothesis
    source: str


@dataclass(frozen=True, slots=True)
class Chain:
    target: str  # "schema.tabela.coluna" (técnico)
    label: str | None
    steps: tuple[Step, ...]
    stop: str


@dataclass(frozen=True, slots=True)
class ValueOrigin:
    interpretation: Interpretation
    chains: tuple[Chain, ...]
    warnings: tuple[str, ...]


class ValueOriginError(QueryError):
    """Erro de uso (função ambígua, alvo inválido), mensagem em português."""


# --------------------------------------------------------------------------- utilidades do grafo


def node_attrs(conn: sqlite3.Connection, node_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT attrs FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return dict(json.loads(row[0])) if row else {}


def short(node_id: str) -> str:
    return node_id.partition(":")[2] or node_id


def _resolve_function(conn: sqlite3.Connection, value: str) -> str:
    """`PgFunction:...`, `schema.nome` ou só o nome; levanta se ambíguo ou não achar.

    Nome com várias assinaturas que só repassam para uma (`CALLS_RPC` interna à
    família) resolve sozinho — o alcance usa o destino, que faz o trabalho de verdade."""
    text = value.strip()
    if text.startswith("PgFunction:"):
        row = conn.execute(
            "SELECT 1 FROM nodes WHERE id = ? AND label = 'PgFunction'", (text,)
        ).fetchone()
        if row:
            return text
        raise ValueOriginError(f"função não encontrada: {text}")
    resolution = routine_store.resolve_family(conn, text)
    if resolution.chosen is not None:
        return resolution.chosen.node_id
    if resolution.resolved_by == "nenhum":
        hints = routine_store.suggest_names(conn, text)
        more = f" Parecidas: {', '.join(hints)}." if hints else ""
        raise ValueOriginError(f"função não encontrada: {text}.{more}")
    names = ", ".join(c.node_id for c in resolution.candidates[:10])
    raise ValueOriginError(
        f"mais de uma função casa com {text!r}; use o id completo de uma delas: {names}"
    )


def _reach(conn: sqlite3.Connection, root: str) -> list[str]:
    """`root` e as funções que ela alcança por `CALLS_RPC`, até `MAX_REACH_HOPS` níveis."""
    seen = [root]
    seen_set = {root}
    frontier = [root]
    for _level in range(MAX_REACH_HOPS):
        if not frontier or len(seen) >= MAX_REACH_NODES:
            break
        marks = ",".join("?" for _ in frontier)
        rows = conn.execute(
            f"SELECT DISTINCT dst FROM edges WHERE src IN ({marks}) "  # noqa: S608
            "AND type = 'CALLS_RPC' AND dst LIKE 'PgFunction:%'",
            frontier,
        ).fetchall()
        frontier = []
        for (dst,) in rows:
            if dst not in seen_set and len(seen) < MAX_REACH_NODES:
                seen_set.add(dst)
                seen.append(dst)
                frontier.append(dst)
    return seen


_ANCHOR_LABEL = {"menu": "BusinessAction", "file": "File", "table": "Table", "module": "Module"}


def _resolve_node(conn: sqlite3.Connection, value: str, anchor_kind: str) -> str:
    """Valida um id de nó (menu, fonte, tabela ou módulo): existe e tem o rótulo certo."""
    label = _ANCHOR_LABEL[anchor_kind]
    text = value.strip()
    if not text.startswith(f"{label}:"):
        raise ValueOriginError(f"id inválido para {anchor_kind} (esperado {label}:...): {text}")
    row = conn.execute("SELECT 1 FROM nodes WHERE id = ? AND label = ?", (text, label)).fetchone()
    if not row:
        raise ValueOriginError(f"{anchor_kind} não encontrado: {text}")
    return text


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _reach_table(conn: sqlite3.Connection, table_id: str) -> list[str]:
    """As `PgFunction` que leem, gravam, filtram, ordenam ou cruzam a tabela: por coluna
    (`routine_fields`) ou, quando a instrução não foi lida por coluna, pela tabela
    inteira da instrução (`routine_queries.kind = 'parse_fallback'`, mesma fonte que
    `_column_candidates` já usa como segunda pista)."""
    if not routine_store.has_fields(conn):
        raise ValueOriginError("este mapa não tem campos e consultas para buscar por tabela")
    rows = conn.execute(
        f"SELECT routine_id FROM {routine_store.FIELDS_TABLE} WHERE table_id = ? "  # noqa: S608
        f"UNION SELECT routine_id FROM {routine_store.QUERIES_TABLE}, json_each(tables) "
        "WHERE kind = 'parse_fallback' AND json_each.value = ? "
        "ORDER BY routine_id",
        (table_id, table_id),
    ).fetchall()
    return [r[0] for r in rows]


def _reach_module(conn: sqlite3.Connection, module_id: str) -> list[str]:
    """As `PgFunction` classificadas nesse módulo (`analytics_routines`). A coluna
    `module` grava o id completo do nó (`Module:<nome>`), não só o nome."""
    if not _has_table(conn, "analytics_routines"):
        raise ValueOriginError("este mapa não tem classificação de módulo para buscar por módulo")
    rows = conn.execute(
        "SELECT routine_id FROM analytics_routines WHERE module = ? ORDER BY routine_id",
        (module_id,),
    ).fetchall()
    return [r[0] for r in rows]


def _reach_from_php(conn: sqlite3.Connection, roots: Sequence[str]) -> list[str]:
    """A partir de arquivos/métodos PHP, as `PgFunction` que eles alcançam — direto ou por outra
    função/método que chamam (`CALLS`, `INSTANTIATES`) —, até `MAX_REACH_HOPS` níveis."""
    seen_php: set[str] = set(roots)
    frontier = list(roots)
    reach: list[str] = []
    reach_set: set[str] = set()
    for _level in range(MAX_REACH_HOPS):
        if not frontier or len(reach) >= MAX_REACH_NODES:
            break
        marks = ",".join("?" for _ in frontier)
        rows = conn.execute(
            f"SELECT DISTINCT dst FROM edges WHERE src IN ({marks}) "  # noqa: S608
            "AND type IN ('CALLS', 'INSTANTIATES', 'CALLS_RPC')",
            frontier,
        ).fetchall()
        frontier = []
        for (dst,) in rows:
            if dst.startswith("PgFunction:"):
                if dst not in reach_set and len(reach) < MAX_REACH_NODES:
                    reach_set.add(dst)
                    reach.append(dst)
            elif dst not in seen_php:
                seen_php.add(dst)
                frontier.append(dst)
    return reach


def _reach_menu(conn: sqlite3.Connection, menu_id: str) -> list[str]:
    """As `PgFunction` alcançadas pelos arquivos/métodos que implementam este item de menu
    (`IMPLEMENTS_ACTION`, no sentido contrário de `value_origin`'s detecção de tela)."""
    rows = conn.execute(
        "SELECT DISTINCT src FROM edges WHERE dst = ? AND type = 'IMPLEMENTS_ACTION'", (menu_id,)
    ).fetchall()
    roots = [r[0] for r in rows]
    if not roots:
        raise ValueOriginError(f"nenhum arquivo implementa este menu: {short(menu_id)}")
    return _reach_from_php(conn, roots)


def _reach_for_anchor(conn: sqlite3.Connection, anchor_kind: str, anchor_id: str) -> list[str]:
    if anchor_kind == "function":
        return _reach(conn, anchor_id)
    if anchor_kind == "table":
        return _reach_table(conn, anchor_id)
    if anchor_kind == "module":
        return _reach_module(conn, anchor_id)
    if anchor_kind == "menu":
        return _reach_menu(conn, anchor_id)
    if anchor_kind == "file":
        return _reach_from_php(conn, [anchor_id])
    raise ValueOriginError(f"tipo de alcance desconhecido: {anchor_kind}")


# --------------------------------------------------------------------------- interpretação


def _label_score(
    label: str | None, description: str | None, terms: frozenset[str]
) -> tuple[float, tuple[str, ...]]:
    """Quanto mais termos da pergunta o **rótulo** cobre, maior o placar. A descrição (texto
    corrido, ex.: "Valor em metros lineares da testada do lote") casa palavra demais por acaso —
    só desempata, nunca sozinha, e com peso bem menor."""
    if not terms:
        return 0.0, ()
    return label_terms_score(_terms(label or ""), terms, description)


def label_terms_score(
    label_terms: frozenset[str], terms: frozenset[str], description: str | None = None
) -> tuple[float, tuple[str, ...]]:
    """A mesma regra de `_label_score` com o rótulo já reduzido às classes de sentido (a busca por
    termo guarda isso em cache e não repete `_terms` nas 23 mil colunas)."""
    if not terms:
        return 0.0, ()
    hit = terms & label_terms
    if hit:
        coverage = len(hit) / len(terms)
        return 0.3 + 0.7 * coverage, tuple(sorted(hit))
    if not description:
        return 0.0, ()
    desc_hit = terms & _terms(description)
    if len(desc_hit) >= 2:  # uma palavra solta na descrição não basta (ex.: só "valor")
        coverage = len(desc_hit) / len(terms)
        return 0.15 * coverage, tuple(sorted(desc_hit))
    return 0.0, ()


def _column_candidates(
    conn: sqlite3.Connection, reach: Sequence[str], terms: frozenset[str]
) -> list[Candidate]:
    by_key: dict[tuple[str, str | None], Candidate] = {}
    table_attrs_cache: dict[str, dict[str, Any]] = {}

    def attrs_of(table_id: str) -> dict[str, Any]:
        if table_id not in table_attrs_cache:
            table_attrs_cache[table_id] = node_attrs(conn, table_id)
        return table_attrs_cache[table_id]

    if not reach:
        return []
    marks = ",".join("?" for _ in reach)
    # colunas lidas/gravadas no alcance, com AST (routine_fields)
    field_rows = conn.execute(
        f"SELECT DISTINCT table_id, column_name, routine_id, role FROM routine_fields "  # noqa: S608
        f"WHERE routine_id IN ({marks}) AND column_name != '*'",
        reach,
    ).fetchall()
    for table_id, column, routine_id, role in field_rows:
        table = attrs_of(table_id)
        catalog = table.get("catalog") or {}
        columns_catalog = table.get("columns_catalog") or {}
        col_info = columns_catalog.get(column) or {}
        score, matched = _label_score(col_info.get("label"), col_info.get("description"), terms)
        if score <= 0:
            continue
        if role == "read":
            score += 0.2
        key = (table_id, column)
        existing = by_key.get(key)
        routines = {*(existing.routines if existing else ()), routine_id}
        label = col_info.get("label") if isinstance(col_info.get("label"), str) else None
        table_label = catalog.get("label") if isinstance(catalog.get("label"), str) else None
        reason = (
            f"casou {', '.join(repr(m) for m in matched)} com o rótulo "
            f"{label or table_label or short(table_id)!r}; usada em {short(routine_id)}"
        )
        by_key[key] = Candidate(
            table_id, column, label, matched, tuple(sorted(routines))[:3], score, reason
        )
    # tabelas inteiras de instruções sem coluna (parse_fallback), score menor
    query_rows = conn.execute(
        f"SELECT DISTINCT routine_id, tables FROM routine_queries "  # noqa: S608
        f"WHERE routine_id IN ({marks}) AND kind = 'parse_fallback'",
        reach,
    ).fetchall()
    for routine_id, tables_json in query_rows:
        for table_id in json.loads(tables_json):
            table = attrs_of(table_id)
            catalog = table.get("catalog") or {}
            columns_catalog = table.get("columns_catalog") or {}
            # a instrução não foi lida por coluna: tenta cada coluna que o
            # dicionário conhece desta tabela — é a melhor pista que há, mas não é certeza.
            for column, info in columns_catalog.items():
                key = (table_id, column)
                if key in by_key:
                    continue
                score, matched = _label_score(info.get("label"), info.get("description"), terms)
                if score <= 0:
                    continue
                label = info.get("label") if isinstance(info.get("label"), str) else None
                by_key[key] = Candidate(
                    table_id,
                    column,
                    label,
                    matched,
                    (routine_id,),
                    score,
                    f"casou {', '.join(repr(m) for m in matched)} com o rótulo da coluna "
                    f"{label!r} (a leitura da instrução não achou a coluna certa: confirmar)",
                )
            table_key = (table_id, None)
            if table_key in by_key:
                continue
            score, matched = _label_score(catalog.get("label"), catalog.get("description"), terms)
            if score <= 0:
                continue
            label = catalog.get("label") if isinstance(catalog.get("label"), str) else None
            by_key[table_key] = Candidate(
                table_id,
                None,
                label,
                matched,
                (routine_id,),
                score * 0.6,
                f"casou {', '.join(repr(m) for m in matched)} com o rótulo da tabela "
                f"{label or short(table_id)!r} (sem leitura de coluna certa nesta instrução)",
            )
    ordered = sorted(by_key.values(), key=lambda c: (-c.score, c.table, c.column or ""))
    return ordered[:MAX_CANDIDATES]


def interpret(
    conn: sqlite3.Connection,
    question: str,
    function: str | None = None,
    *,
    anchor_kind: str = "function",
    limit: int = MAX_CANDIDATES,
) -> Interpretation:
    """Interpreta a pergunta: casa os termos com os rótulos do dicionário dentro do alcance do
    ponto de partida (`anchor_kind`: função, menu, fonte, tabela ou módulo). Sem
    ponto de partida, sugere funções pela busca textual e devolve candidatos com score menor."""
    if anchor_kind not in ANCHOR_KINDS:
        raise ValueOriginError(f"tipo de alcance desconhecido: {anchor_kind}")
    warnings: list[str] = []
    resolved_anchor: str | None = None
    reach: list[str] = []
    suggested: tuple[str, ...] = ()
    terms = _terms(question)
    if function:
        if anchor_kind == "function":
            resolved_anchor = _resolve_function(conn, function)
        else:
            resolved_anchor = _resolve_node(conn, function, anchor_kind)
        reach = _reach_for_anchor(conn, anchor_kind, resolved_anchor)
        if len(reach) > WIDE_REACH_WARNING:
            warnings.append(
                f"alcance amplo ({len(reach)} funções): considere restringir à função, se souber"
            )
    else:
        warnings.append("escolha a função para precisão: a busca abaixo é sem alcance")
        # a busca por texto não tem acesso ao caminho do db aqui: usa o grafo via rótulo direto
        rows = conn.execute(
            "SELECT id FROM nodes WHERE label = 'PgFunction' "
            "AND (lower(json_extract(attrs, '$.name')) LIKE ?) LIMIT 5",
            (f"%{_normalize(next(iter(terms), ''))[:30]}%",),
        ).fetchall()
        suggested = tuple(r[0] for r in rows)
        reach = [r for r in suggested]
    candidates = _column_candidates(conn, reach, terms) if reach else []
    return Interpretation(
        question,
        resolved_anchor,
        suggested,
        tuple(reach),
        tuple(candidates),
        tuple(warnings),
        anchor_kind,
    )


# --------------------------------------------------------------------------- fatia léxica


@dataclass(frozen=True, slots=True)
class Write:
    """Um ponto do texto que atribui a `var`: `select ... into` (uma por posição da lista) ou
    uma atribuição simples (`var := expr` / `var = expr`, em início de instrução)."""

    var: str
    line: int
    end_line: int
    kind: str  # select_into | assign
    rhs: str | None  # texto da expressão (assign)
    select_sql: str | None  # SQL sem o "into ..." (select_into)
    position: int  # posição de `var` na lista do INTO


SELECT_INTO = re.compile(
    r"\bSELECT\b.*?\bINTO\b(?P<vars>[\w\s,.\"\[\]]+?)\bFROM\b", re.IGNORECASE | re.DOTALL
)
ASSIGN_START = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(:=|=)\s*(.+)$", re.DOTALL)
_STMT_BOUNDARY = re.compile(r";|\bBEGIN\b|\bTHEN\b|\bELSE\b|\bLOOP\b", re.IGNORECASE)
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def statement_chunks(masked: str) -> list[tuple[int, int]]:
    chunks: list[tuple[int, int]] = []
    start = 0
    for m in _STMT_BOUNDARY.finditer(masked):
        chunks.append((start, m.end()))
        start = m.end()
    chunks.append((start, len(masked)))
    return chunks


def strip_into(whole: str) -> str:
    """`whole` casado por `SELECT_INTO` (de `SELECT` até `FROM`), sem o trecho `INTO ...`."""
    into_kw = re.search(r"\binto\b", whole, re.IGNORECASE)
    from_kw = re.search(r"\bfrom\b", whole, re.IGNORECASE)
    if into_kw is None or from_kw is None:
        return whole
    return whole[: into_kw.start()] + whole[from_kw.start() :]


def line_of_factory(ddl: str, body_start: int) -> Any:
    newlines = [m.start() for m in re.finditer("\n", ddl)]

    def line_of(offset_in_body: int) -> int:
        absolute = body_start + offset_in_body
        lo, hi = 0, len(newlines)
        while lo < hi:
            mid = (lo + hi) // 2
            if newlines[mid] < absolute:
                lo = mid + 1
            else:
                hi = mid
        return 1 + lo

    return line_of


def scan_writes(ddl: str) -> list[Write]:
    """Toda atribuição e todo `select ... into` do corpo da função (fatia léxica; não depende
    da árvore do `parse_plpgsql`, que falha em funções reais com parâmetro de saída composto)."""
    body_range = _function_body(ddl)
    if body_range is None:
        return []
    body_start, body_end = body_range
    body = ddl[body_start:body_end]
    masked = _mask(body, _scan_spans(body))
    line_of = line_of_factory(ddl, body_start)
    writes: list[Write] = []
    for start, end in statement_chunks(masked):
        chunk = masked[start:end]
        stripped = chunk.strip()
        if not stripped:
            continue
        lead = len(chunk) - len(chunk.lstrip())
        stmt_start = start + lead

        match = SELECT_INTO.search(chunk)
        if match is not None:
            var_names = [
                v.strip().lower()
                for v in re.split(r"[,\s]+", match.group("vars").strip())
                if v.strip()
            ]
            real_whole = body[
                stmt_start + (match.start() - lead) : stmt_start + (match.end() - lead)
            ]
            tail = body[stmt_start + (match.end() - lead) : end]
            select_sql = strip_into(real_whole) + tail
            line = line_of(stmt_start + (match.start() - lead))
            end_line = line_of(end)
            for position, var in enumerate(var_names):
                writes.append(Write(var, line, end_line, "select_into", None, select_sql, position))
            continue

        assign = ASSIGN_START.match(stripped)
        if assign is not None:
            var = assign.group(1).lower()
            rhs = assign.group(3).rstrip().rstrip(";").strip()
            writes.append(Write(var, line_of(stmt_start), line_of(end), "assign", rhs, None, 0))
    return writes


def known_variables(conn: sqlite3.Connection, routine_id: str) -> frozenset[str]:
    profile = routine_store.read_profile(conn, routine_id)
    if profile is None:
        return frozenset()
    names = {str(p["name"]).lower() for p in profile.get("params") or () if p.get("name")}
    names |= {str(d).lower() for d in profile.get("declared") or ()}
    return frozenset(names)


def rhs_variables(rhs: str, known: frozenset[str]) -> list[str]:
    found: list[str] = []
    for token in IDENT.findall(rhs):
        lowered = token.lower()
        if lowered in known and lowered not in found:
            found.append(lowered)
    return found[:2]


class _ColumnCollector(pglast.visitors.Visitor):
    def __init__(self) -> None:
        self.refs: list[ast.ColumnRef] = []

    def visit_ColumnRef(self, _ancestors: object, node: ast.ColumnRef) -> None:  # noqa: N802
        self.refs.append(node)


def columns_in(node: ast.Node | tuple[ast.Node, ...] | None) -> list[ast.ColumnRef]:
    if node is None:
        return []
    collector = _ColumnCollector()
    collector(node)
    return collector.refs


def column_name_of(ref: ast.ColumnRef) -> tuple[str | None, str]:
    parts = list(ref.fields or ())
    if not parts:
        return None, ""
    last = parts[-1]
    name = "" if isinstance(last, ast.A_Star) else str(getattr(last, "sval", "") or "")
    qualifier = parts[-2].sval if len(parts) > 1 and isinstance(parts[-2], ast.String) else None
    return qualifier, name


def alias_map_of(select_stmt: ast.SelectStmt) -> dict[str, str]:
    """alias/nome em minúsculas → nome da tabela (minúsculas), só o que o `FROM` desta consulta
    declara (consulta isolada, sem CTE: suficiente para a fatia léxica)."""
    aliases: dict[str, str] = {}

    def walk(node: object) -> None:
        if isinstance(node, ast.RangeVar) and node.relname:
            name = node.relname.lower()
            alias = (
                node.alias.aliasname.lower()
                if node.alias is not None and node.alias.aliasname
                else name
            )
            aliases[alias] = name
            aliases.setdefault(name, name)
        elif isinstance(node, ast.JoinExpr):
            walk(node.larg)
            walk(node.rarg)

    for item in select_stmt.fromClause or ():
        walk(item)
    return aliases


def resolve_into_column(
    conn: sqlite3.Connection, write: Write, routine_id: str
) -> tuple[list[str], str | None, float, str] | None:
    """`(tabelas candidatas, coluna, confiança, motivo)` da posição `write.position` do
    `select ... into`, usando as tabelas já resolvidas para a mesma instrução
    (`routine_queries`, qualquer `kind`) e as colunas de cada `Table` do grafo."""
    assert write.select_sql is not None
    try:
        parsed = pglast.parse_sql(write.select_sql)
    except pglast.parser.ParseError:
        return None
    select = next((s.stmt for s in parsed if isinstance(s.stmt, ast.SelectStmt)), None)
    target_list = select.targetList or () if select is not None else ()
    if select is None or write.position >= len(target_list):
        return None
    target = target_list[write.position]
    if not isinstance(target, ast.ResTarget):
        return None
    refs = columns_in(target.val)
    if not refs:
        return None
    qualifier, column = column_name_of(refs[0])
    if not column:
        return None
    row = conn.execute(
        "SELECT tables FROM routine_queries WHERE routine_id = ? AND line <= ? AND end_line >= ?"
        " ORDER BY seq LIMIT 1",
        (routine_id, write.line, write.line),
    ).fetchone()
    if row is None:
        return [], column, 0.0, "não achei a consulta correspondente no mapa"
    table_ids = json.loads(row[0])
    aliases = alias_map_of(select)
    if qualifier:
        bare = aliases.get(qualifier.lower(), qualifier.lower())
        candidates = [t for t in table_ids if t.rpartition(".")[2].lower() == bare]
    else:
        candidates = table_ids
    with_column = [
        t
        for t in candidates
        if column.lower() in {c.lower() for c in node_attrs(conn, t).get("columns") or ()}
    ]
    if not with_column:
        return [], column, 0.0, f"não achei a coluna `{column}` nas tabelas desta consulta"
    confidence = 1.0 if len(with_column) == 1 else round(1 / len(with_column), 4)
    return with_column, column, confidence, "lido na instrução de linha " + str(write.line)


def excerpt(ddl: str, line: int, end_line: int) -> tuple[str, ...]:
    lines = ddl.split("\n")
    start = max(1, line)
    stop = min(len(lines), max(end_line, line))
    if stop - start + 1 > MAX_EXCERPT_LINES:
        head = [f"{start}: {lines[start - 1]}", "…"]
        tail = [f"{i}: {lines[i - 1]}" for i in range(stop - 1, stop + 1) if 1 <= i <= len(lines)]
        return tuple(head + tail)
    return tuple(f"{i}: {lines[i - 1]}" for i in range(start, stop + 1))


# --------------------------------------------------------------------------- linhagem


def _find_writer(
    conn: sqlite3.Connection, reach: Sequence[str], table_id: str, column: str
) -> tuple[str, int] | None:
    """A função do alcance (mais perto da raiz primeiro) que grava `column`, e a linha."""
    for routine_id in reach:
        row = conn.execute(
            "SELECT line FROM routine_fields WHERE routine_id = ? AND table_id = ? "
            "AND column_name = ? AND role = 'write' ORDER BY line LIMIT 1",
            (routine_id, table_id, column),
        ).fetchone()
        if row is not None:
            return routine_id, int(row[0])
    return None


def write_expression(
    conn: sqlite3.Connection, routine_id: str, table_id: str, column: str, line: int
) -> tuple[str | None, int, int]:
    """O texto do lado direito (`SET col = expr`/`(cols) VALUES (exprs)`) para `column`, lido de
    novo da instrução (não fica salvo no banco): `(expr, line, end_line)`. `expr=None` quando
    não dá para achar (instrução não lida pelo AST)."""
    row = conn.execute(
        "SELECT seq, kind, line, end_line FROM routine_queries WHERE routine_id = ? "
        "AND line <= ? AND end_line >= ? ORDER BY seq LIMIT 1",
        (routine_id, line, line),
    ).fetchone()
    if row is None:
        return None, line, line
    _seq, kind, stmt_line, end_line = row
    source = routine_store.read_routine(conn, routine_id)
    if source is None:
        return None, stmt_line, end_line
    text = "\n".join(routine_store.split_lines(source.ddl)[stmt_line - 1 : end_line])
    try:
        parsed = pglast.parse_sql(text)
    except pglast.parser.ParseError:
        return None, stmt_line, end_line
    stmt = next((s.stmt for s in parsed if type(s.stmt) in (ast.UpdateStmt, ast.InsertStmt)), None)
    if isinstance(stmt, ast.UpdateStmt):
        for rt in stmt.targetList or ():
            if (
                isinstance(rt, ast.ResTarget)
                and rt.name
                and rt.name.lower() == column.lower()
                and rt.val is not None
            ):
                from pglast.stream import RawStream

                return RawStream()(rt.val), stmt_line, end_line
    elif isinstance(stmt, ast.InsertStmt):
        cols = [c.name for c in stmt.cols or () if isinstance(c, ast.ResTarget) and c.name]
        if column.lower() in [c.lower() for c in cols] and isinstance(
            stmt.selectStmt, ast.SelectStmt
        ):
            index = [c.lower() for c in cols].index(column.lower())
            targets = stmt.selectStmt.targetList or ()
            if index < len(targets):
                target = targets[index]
                if isinstance(target, ast.ResTarget) and target.val is not None:
                    from pglast.stream import RawStream

                    return RawStream()(target.val), stmt_line, end_line
    return None, stmt_line, end_line


def trace_value(
    conn: sqlite3.Connection,
    interpretation: Interpretation,
    targets: Sequence[str],
    *,
    max_hops: int = MAX_HOPS_DEFAULT,
) -> ValueOrigin:
    """A cadeia de cada alvo (`Table:schema.tabela#coluna` ou `schema.tabela.coluna`), da função
    até a coluna base e até a tela que a cadastra (ou a hipótese pelo nome, sem chamador
    conhecido)."""
    warnings: list[str] = []
    chains: list[Chain] = []
    reach = list(interpretation.reach)
    for raw_target in targets:
        table_id, column = _parse_target(conn, raw_target)
        if table_id is None or column is None:
            warnings.append(f"alvo inválido: {raw_target}")
            continue
        label = (
            (node_attrs(conn, table_id).get("columns_catalog") or {}).get(column, {}).get("label")
        )
        steps, stop = _trace_one(conn, reach, table_id, column, max_hops)
        chains.append(Chain(f"{short(table_id)}.{column}", label, tuple(steps), stop))
    return ValueOrigin(interpretation, tuple(chains), tuple(warnings))


def _parse_target(conn: sqlite3.Connection, raw: str) -> tuple[str | None, str | None]:
    text = raw.strip()
    if text.startswith("Table:") and "#" in text:
        table_id, _sep, column = text.partition("#")
        return table_id, column.lower()
    parts = text.split(".")
    if len(parts) == 3:
        schema, table, column = parts
        table_id = f"Table:{schema}.{table}"
        row = conn.execute("SELECT 1 FROM nodes WHERE id = ?", (table_id,)).fetchone()
        return (table_id, column.lower()) if row else (None, None)
    if len(parts) == 2:
        table, column = parts
        rows = conn.execute(
            "SELECT id FROM nodes WHERE label = 'Table' AND id LIKE ?", (f"Table:%.{table}",)
        ).fetchall()
        return (rows[0][0], column.lower()) if len(rows) == 1 else (None, None)
    return None, None


def _trace_one(
    conn: sqlite3.Connection, reach: Sequence[str], table_id: str, column: str, max_hops: int
) -> tuple[list[Step], str]:
    steps: list[Step] = []
    visited: set[tuple[str, str, int, str]] = set()
    reason = _trace_column(conn, reach, table_id, column, steps, max_hops, visited)
    if reason is None:
        base = next((s for s in reversed(steps) if s.kind == "base_column"), None)
        base_table = base.table if base and base.table else table_id
        base_column = base.column if base and base.column else column
        return steps, climb_to_screen(conn, base_table, base_column, steps)
    return steps, reason


def _trace_column(
    conn: sqlite3.Connection,
    reach: Sequence[str],
    table_id: str,
    column: str,
    steps: list[Step],
    depth: int,
    visited: set[tuple[str, str, int, str]],
) -> str | None:
    """`None` quando a cadeia chega a uma coluna base (sucesso); senão, o motivo de parada."""
    if depth <= 0:
        return "limite de saltos"
    found = _find_writer(conn, reach, table_id, column)
    if found is None:
        label = (
            (node_attrs(conn, table_id).get("columns_catalog") or {}).get(column, {}).get("label")
        )
        steps.append(
            Step(
                "base_column",
                f"`{short(table_id)}.{column}`"
                + (f" ({label})" if label else "")
                + " não é gravada por nenhuma função do alcance: é a coluna base",
                None,
                table_id,
                column,
                None,
                None,
                (),
                1.0,
                "routine_fields",
            )
        )
        return None
    routine_id, write_line = found
    key = (routine_id, column, write_line, "column")
    if key in visited:
        return "laço detectado: parando aqui"
    visited.add(key)
    expr, stmt_line, stmt_end = write_expression(conn, routine_id, table_id, column, write_line)
    source = routine_store.read_routine(conn, routine_id)
    ddl = source.ddl if source else ""
    if expr is None:
        label = (
            (node_attrs(conn, table_id).get("columns_catalog") or {}).get(column, {}).get("label")
        )
        steps.append(
            Step(
                "base_column",
                f"`{short(table_id)}.{column}`"
                + (f" ({label})" if label else "")
                + f" é gravada em `{short(routine_id)}`, mas o texto da instrução"
                " não pôde ser lido de novo",
                routine_id,
                table_id,
                column,
                routine_id,
                stmt_line,
                excerpt(ddl, stmt_line, stmt_end),
                0.6,
                "routine_fields",
            )
        )
        return None
    steps.append(
        Step(
            "write",
            f"`{short(table_id)}.{column}` recebe `{expr}` em `{short(routine_id)}`",
            routine_id,
            table_id,
            column,
            routine_id,
            stmt_line,
            excerpt(ddl, stmt_line, stmt_end),
            1.0,
            "routine_fields",
        )
    )
    known = known_variables(conn, routine_id)
    rhs_vars = rhs_variables(expr, known)
    if not rhs_vars:
        steps.append(
            Step(
                "stop",
                f"`{expr}` é um valor calculado no código, sem variável rastreável",
                None,
                None,
                None,
                routine_id,
                stmt_line,
                (),
                0.5,
                "lexical_slice",
            )
        )
        return "valor calculado no código"
    reasons: list[str] = []
    for var in rhs_vars:
        reason = _trace_variable(
            conn, reach, routine_id, var, stmt_line, ddl, steps, depth - 1, visited
        )
        if reason is None:
            return None
        reasons.append(reason)
    return reasons[0] if reasons else "não achei de onde a variável vem"


def _stop_for_variable(conn: sqlite3.Connection, routine_id: str, var: str) -> str:
    profile = routine_store.read_profile(conn, routine_id)
    is_param = profile is not None and any(
        str(p.get("name") or "").lower() == var for p in profile.get("params") or ()
    )
    if is_param:
        callers = conn.execute(
            "SELECT DISTINCT src FROM edges WHERE dst = ? AND type = 'CALLS_RPC' LIMIT 3",
            (routine_id,),
        ).fetchall()
        names = ", ".join(short(c[0]) for c in callers) or "nenhum chamador conhecido"
        return f"`{var}` é parâmetro de `{short(routine_id)}`: vem de quem chama a função ({names})"
    return f"não achei atribuição anterior de `{var}` em `{short(routine_id)}`"


def _trace_variable(
    conn: sqlite3.Connection,
    reach: Sequence[str],
    routine_id: str,
    var: str,
    before_line: int,
    ddl: str,
    steps: list[Step],
    depth: int,
    visited: set[tuple[str, str, int, str]],
) -> str | None:
    if depth <= 0:
        return "limite de saltos"
    writes = sorted(
        (w for w in scan_writes(ddl) if w.var == var and w.line < before_line),
        key=lambda w: w.line,
        reverse=True,
    )[:2]
    if not writes:
        return _stop_for_variable(conn, routine_id, var)
    reasons: list[str] = []
    for write in writes:
        key = (routine_id, var, write.line, "var")
        if key in visited:
            continue
        visited.add(key)
        if write.kind == "assign":
            steps.append(
                Step(
                    "assign",
                    f"a variável `{var}` recebe `{write.rhs}`",
                    routine_id,
                    None,
                    None,
                    routine_id,
                    write.line,
                    excerpt(ddl, write.line, write.end_line),
                    0.9,
                    "lexical_slice",
                )
            )
            known = known_variables(conn, routine_id)
            inner_vars = rhs_variables(write.rhs or "", known)
            if not inner_vars:
                reasons.append(f"`{write.rhs}` é um valor calculado, sem variável rastreável")
                continue
            for inner in inner_vars:
                reason = _trace_variable(
                    conn, reach, routine_id, inner, write.line, ddl, steps, depth - 1, visited
                )
                if reason is None:
                    return None
                reasons.append(reason)
        else:  # select_into
            resolution = resolve_into_column(conn, write, routine_id)
            if resolution is None:
                reasons.append(f"não foi possível interpretar a consulta da linha {write.line}")
                continue
            candidates, src_column, confidence, reason_text = resolution
            if not candidates or src_column is None:
                steps.append(
                    Step(
                        "stop",
                        reason_text,
                        None,
                        None,
                        src_column,
                        routine_id,
                        write.line,
                        (),
                        0.0,
                        "statement_tables",
                    )
                )
                reasons.append(reason_text)
                continue
            chosen = candidates[0]
            steps.append(
                Step(
                    "select_into",
                    f"a variável `{var}` vem de `{short(chosen)}.{src_column}`"
                    f" (linha {write.line})",
                    routine_id,
                    chosen,
                    src_column,
                    routine_id,
                    write.line,
                    excerpt(ddl, write.line, write.end_line),
                    confidence,
                    "statement_tables",
                )
            )
            reason = _trace_column(conn, reach, chosen, src_column, steps, depth - 1, visited)
            if reason is None:
                return None
            reasons.append(reason)
    return reasons[0] if reasons else f"não achei de onde a variável `{var}` vem"


def climb_to_screen(conn: sqlite3.Connection, table_id: str, column: str, steps: list[Step]) -> str:
    """Sobe de `table_id.column` (já a coluna base) até a tela que a cadastra, via quem grava a
    tabela no PHP; hipótese pelo nome do arquivo quando não há chamador conhecido."""
    writers = conn.execute(
        "SELECT DISTINCT src FROM edges WHERE dst = ?"
        " AND type IN ('INSERTS', 'UPDATES', 'DELETES')",
        (table_id,),
    ).fetchall()
    if not writers:
        return _hypothesis(conn, table_id, steps)
    for (writer,) in writers[:3]:
        label_node = writer.partition(":")[0]
        if label_node == "File":
            steps.append(
                Step(
                    "screen",
                    f"o arquivo `{short(writer)}` grava a tabela",
                    writer,
                    table_id,
                    column,
                    None,
                    None,
                    (),
                    0.8,
                    "graph_edge",
                )
            )
            _add_menu_step(conn, writer, steps)
            continue
        steps.append(
            Step(
                "writer_dao",
                f"`{short(writer)}` grava a tabela",
                writer,
                table_id,
                column,
                None,
                None,
                (),
                0.8,
                "graph_edge",
            )
        )
        callers = conn.execute(
            "SELECT DISTINCT src FROM edges WHERE dst = ? AND type = 'CALLS'", (writer,)
        ).fetchall()
        if not callers:
            continue
        for (caller,) in callers[:2]:
            file_id = caller if caller.startswith("File:") else None
            if file_id is None:
                continue
            steps.append(
                Step(
                    "screen",
                    f"`{short(file_id)}` chama `{short(writer)}`",
                    file_id,
                    table_id,
                    column,
                    None,
                    None,
                    (),
                    0.8,
                    "graph_edge",
                )
            )
            _add_menu_step(conn, file_id, steps)
    if any(s.kind == "menu" for s in steps):
        return "cadastro encontrado"
    if any(s.kind == "screen" for s in steps):
        # achou quem chama o DAO, mas nenhum item de menu (scan sem --catalog-db):
        # é a tela de verdade — não é hipótese, só falta o menu no mapa.
        return "tela encontrada, sem o item de menu (scan sem --catalog-db)"
    return _hypothesis(conn, table_id, steps)


def _add_menu_step(conn: sqlite3.Connection, file_id: str, steps: list[Step]) -> None:
    rows = conn.execute(
        "SELECT dst FROM edges WHERE src = ? AND type = 'IMPLEMENTS_ACTION' LIMIT 1", (file_id,)
    ).fetchall()
    if not rows:
        return
    menu_path = node_attrs(conn, rows[0][0]).get("menu_path")
    if isinstance(menu_path, str) and menu_path:
        text = f"cadastrado em {menu_path}"
    else:
        # o item de menu existe (`IMPLEMENTS_ACTION`), mas sem o caminho legível: o mapa foi
        # feito sem o dicionário do e-cidade (--catalog-db) ou o item não tem texto.
        text = (
            f"é um item de menu (`{short(rows[0][0])}`), mas sem o caminho legível "
            "(mapa sem --catalog-db ou item sem texto no dicionário)"
        )
    steps.append(Step("menu", text, rows[0][0], None, None, None, None, (), 1.0, "graph_edge"))


_FILE_HYPOTHESIS_PATTERNS = ("cad%_{0}%.php", "func_{0}.php", "forms/db_frm{0}%.php")


def _hypothesis(conn: sqlite3.Connection, table_id: str, steps: list[Step]) -> str:
    bare = short(table_id).rpartition(".")[2]
    found: list[str] = []
    for pattern in _FILE_HYPOTHESIS_PATTERNS:
        like = pattern.format(bare)
        rows = conn.execute(
            "SELECT id FROM nodes WHERE label = 'File' AND id LIKE ? LIMIT 3", (f"File:{like}",)
        ).fetchall()
        found.extend(r[0] for r in rows)
    if not found:
        return "não consta no mapa quem grava esta tabela pela tela"
    for file_id in found[:3]:
        steps.append(
            Step(
                "hypothesis",
                f"hipótese pelo nome: `{short(file_id)}` pode ser a tela — confirmar",
                file_id,
                table_id,
                None,
                None,
                None,
                (),
                0.3,
                "name_hypothesis",
            )
        )
    return "não consta quem grava pela tela: hipótese pelo nome"


# --------------------------------------------------------------------------- dossiê e apoio


_INSTRUCTIONS = (
    "Explique, em português, de onde vem o valor, usando SÓ os elos abaixo. Cite a linha de cada "
    "elo. Um elo marcado «hipótese» não é fato: diga que precisa ser confirmado. Se "
    "faltar um elo (a cadeia parou antes do cadastro), diga «não consta no mapa»."
)

# convite para continuar a conversa com a IA por fora (nunca chamada daqui), por
# categoria do `Chain.stop` — texto fixo.
_NEXT_STEP_TEXTS: dict[str, str] = {
    "cadastro": (
        "Se quiser, peça ao seu assistente de IA para montar a consulta SQL que leria este valor "
        "na tela encontrada, ou para explicar em que situação ele muda."
    ),
    "hipotese": (
        "A última etapa é uma hipótese, não uma certeza do mapa. Peça ao seu assistente de IA para "
        "ajudar a confirmar olhando o trecho de código, ou para sugerir outro arquivo candidato."
    ),
    "outros": (
        "O rastro parou aqui. Peça ao seu assistente de IA para ler o trecho de código acima e "
        "sugerir o próximo passo manual."
    ),
}


def _next_step_category(stop: str) -> str:
    if "cadastro encontrado" in stop:
        return "cadastro"
    if "hipótese" in stop:
        return "hipotese"
    return "outros"


def render_dossier(origin: ValueOrigin, *, max_words: int = DEFAULT_MAX_WORDS) -> str:
    max_words = max(500, min(max_words, MAX_MAX_WORDS))
    parts = [
        f"> Dossiê gerado pelo php-map · pergunta: {origin.interpretation.question}",
        "",
        "# O que analisar",
        _INSTRUCTIONS,
        "",
    ]
    for chain in origin.chains:
        parts.append(f"# Cadeia do valor: {chain.label or chain.target} (`{chain.target}`)")
        parts.append("")
        for i, step in enumerate(chain.steps, start=1):
            badge = "hipótese — confirmar" if step.kind == "hypothesis" else "do mapa"
            where = f" · `{step.routine or step.node or ''}`" if (step.routine or step.node) else ""
            line = f" linha {step.line}" if step.line else ""
            parts.append(
                f"{i}. **{step.text}**{where}{line} · confiança {step.confidence:.2f} · {badge}"
            )
            if step.excerpt:
                parts += ["   ```sql", *(f"   {ln}" for ln in step.excerpt), "   ```"]
        parts.append(f"**Situação:** {chain.stop}")
        parts.append("")
    if origin.chains:
        categories = []
        for chain in origin.chains:
            category = _next_step_category(chain.stop)
            if category not in categories:
                categories.append(category)
        parts.append("# Próximos passos sugeridos")
        parts.append("")
        for category in categories:
            parts.append(_NEXT_STEP_TEXTS[category])
            parts.append("")
    text = "\n".join(parts).rstrip() + "\n"
    words = text.split()
    if len(words) > max_words:
        text = (
            " ".join(words[:max_words])
            + "\n\n_[dossiê cortado: passou do limite de "
            + str(max_words)
            + " palavras]_\n"
        )
    hits = list(scan_text(text))
    if hits:
        pattern, value = hits[0]
        raise ValueOriginError(
            f"o dossiê contém um segredo aparente (padrão {pattern}, valor {mask(value)});"
            " não foi liberado"
        )
    return text


def hint_prompt(
    interpretation: Interpretation, conn: sqlite3.Connection, *, max_labels: int = MAX_HINT_LABELS
) -> str:
    """Prompt curto (≤ 300 palavras) para pedir ao assistente de IA o `tabela.coluna` certo,
    quando a interpretação não achou nada."""
    reach = interpretation.reach[:30]
    labels: list[str] = []
    seen: set[tuple[str, str]] = set()

    def add(table_id: str, column: str) -> bool:
        if (table_id, column) in seen:
            return len(labels) >= max_labels
        seen.add((table_id, column))
        info = (node_attrs(conn, table_id).get("columns_catalog") or {}).get(column) or {}
        label = info.get("label")
        if isinstance(label, str) and label:
            labels.append(f"{label} ({short(table_id)}.{column})")
        return len(labels) >= max_labels

    if reach:
        marks = ",".join("?" for _ in reach)
        for table_id, column in conn.execute(
            f"SELECT DISTINCT table_id, column_name FROM routine_fields "  # noqa: S608
            f"WHERE routine_id IN ({marks}) AND column_name != '*'",
            reach,
        ):
            if add(table_id, column):
                break
        for (tables_json,) in conn.execute(
            f"SELECT tables FROM routine_queries "  # noqa: S608
            f"WHERE routine_id IN ({marks}) AND kind = 'parse_fallback'",
            reach,
        ):
            for table_id in json.loads(tables_json):
                columns_catalog = node_attrs(conn, table_id).get("columns_catalog") or {}
                done = False
                for column in columns_catalog:
                    if add(table_id, column):
                        done = True
                        break
                if done or len(labels) >= max_labels:
                    break
            if len(labels) >= max_labels:
                break
    lines = [
        f"Pergunta: {interpretation.question}",
        "",
        "Entre os campos abaixo, qual corresponde à pergunta?",
        "",
        *(f"- {label}" for label in labels),
        "",
        "Responda só com `tabela.coluna`, ou `nenhum`.",
    ]
    return "\n".join(lines)


__all__ = [
    "Candidate",
    "Chain",
    "Interpretation",
    "Step",
    "ValueOrigin",
    "ValueOriginError",
    "hint_prompt",
    "interpret",
    "render_dossier",
    "short",
    "trace_value",
]
