"""Fórmulas das funções da base e dossiê de cálculo: sem IA.

Cada cálculo de uma função PL/pgSQL (atribuição, `SELECT ... INTO`, `UPDATE ... SET`, valor de
`INSERT`, `RETURN`) vira uma `Formula`: destino, expressão, linha e **entradas**, cada uma levada
até `tabela.campo` quando o mapa permite (variável → última atribuição com dado; registro de laço
`for r in select` → coluna da consulta; função chamada → o nó dela). O **dossiê** junta as fórmulas
da função e das auxiliares que ela chama dentro de uma fórmula (largura primeiro, com limites) e
leva cada dado-base até o menu em que é cadastrado. O que não se resolve vai para `untraced`, com o
motivo: nunca um palpite.

A leitura é **léxica** (mesmo leitor das regras): a árvore do `parse_plpgsql` falha em
funções reais. O `pglast` só entra para ler SQL isolado (consulta de um laço,
`SELECT ... INTO`, `UPDATE`, `INSERT`), e uma falha dele derruba apenas aquele trecho.
"""

import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

import pglast
from pglast import ast
from pglast.stream import RawStream

from php_map.analysis import formula_topics as topics
from php_map.analysis import table_access as access_module
from php_map.analysis import value_origin as vo
from php_map.analysis.formula_topics import Topic
from php_map.analysis.table_access import FieldFill, TableAccess, Toucher
from php_map.errors import QueryError
from php_map.graph import routines as routine_store
from php_map.ingest.plpgsql_rules import _function_body, _mask, _scan_spans
from php_map.secret_scan import mask as mask_secret
from php_map.secret_scan import scan_text

DEFAULT_DEPTH = 2
DEFAULT_MAX_FUNCTIONS = 10
DEFAULT_MAX_FORMULAS = 60
DEFAULT_MAX_WORDS = 3000
MAX_PAGED_FORMULAS = 400  # teto de `max_formulas` quando o MCP pagina o dossiê
MAX_EXPRESSION = 1500
MAX_EXCERPT_LINES = 40
EXCERPT_MARGIN = 5
MAX_VAR_SOURCES = 3
MAX_VAR_DEPTH = 6
MAX_SUBSELECT_DEPTH = 8


class AmbiguousRoutineError(QueryError):
    """O nome casa com mais de uma função; `candidates` traz as assinaturas."""

    def __init__(self, message: str, candidates: Sequence[str]) -> None:
        super().__init__(message)
        self.candidates = tuple(candidates)


# --------------------------------------------------------------------------- tipos públicos


@dataclass(frozen=True, slots=True)
class FormulaInput:
    kind: str  # column | variable | parameter | record_field | function | literal
    name: str  # como está no texto: valor_receita, record_alias.k00_valor, fc_juros
    table_id: str | None = None  # Table:caixa.arrecad (quando resolvido)
    column: str | None = None  # k00_valor
    routine_id: str | None = None  # PgFunction:... (kind == "function")
    via_line: int | None = None  # linha onde a variável recebeu o valor resolvido
    expression: str | None = None  # a conta do campo calculado na consulta do laço


@dataclass(frozen=True, slots=True)
class Formula:
    routine_id: str
    target: str  # juro | tmpdadosiptu.vm2t (UPDATE SET/INSERT) | RETURN
    expression: str  # expressão com espaços normalizados e sem comentário
    line: int
    end_line: int
    kind: str  # assign | select_into | update_set | insert_value | return
    inputs: tuple[FormulaInput, ...]
    readable: str  # a expressão com o nome do dicionário
    excerpt: tuple[str, ...]  # linha-5 .. end_line+5 do texto da função
    topic: Topic | None = None  # o assunto: pista pelo nome, pela chamada ou rótulo


@dataclass(frozen=True, slots=True)
class BaseInput:
    table_id: str
    column: str
    label: str | None
    description: str | None
    used_in: tuple[tuple[str, int], ...]  # (routine_id, linha da fórmula)
    writers: tuple[str, ...]  # Method:cl_tabrecjm::incluir ...
    screens: tuple[str, ...]  # File:cai1_recejm001.php ...
    menus: tuple[str, ...]  # "Arrecadação > Cadastros > Juros e Multas > ..."


@dataclass(frozen=True, slots=True)
class CalcDossier:
    root: str
    functions: tuple[str, ...]  # raiz + auxiliares, na ordem da descida
    formulas: tuple[Formula, ...]
    inputs: tuple[BaseInput, ...]
    untraced: tuple[tuple[str, str], ...]  # (o quê, motivo)
    truncated: bool  # bateu nos limites (profundidade / funções / fórmulas)
    formulas_total: int = 0  # fórmulas achadas nas funções do dossiê, antes do corte
    functions_omitted: int = 0  # funções chamadas que ficaram de fora
    counted: tuple[Formula, ...] = ()  # todas as fórmulas do dossiê, antes do corte (visão geral)
    # como `root` foi escolhido quando o nome pedido era ambíguo.
    resolved_by: str = "unico"  # "unico" | "repasse" (nome com várias assinaturas)
    resolved_chain: tuple[str, ...] = ()  # só com "repasse": do membro de menos args ao destino
    resolved_others: tuple[str, ...] = ()  # só com "repasse": as outras assinaturas da família


# --------------------------------------------------------------------------- texto e léxico


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_KEYWORDS = _words(
    """and or not is null true false case when then else end in like ilike between distinct as
    select from where exists any all some current_date current_timestamp current_time localtime
    localtimestamp interval date timestamp time using on join left right inner outer cross group
    by order having limit offset union asc desc into returning over partition filter within
    array row only both leading trailing at zone default for loop if elsif elseif while return
    perform raise notice exception execute declare begin"""
)
# palavras que, seguidas de "(", não são chamada de função
_NOT_CALLS = _words(
    """and or not in exists any all some between case when then else end is like ilike on using
    where from select values over filter group order having limit union as return if elsif
    while"""
)
# `fc_*` que não calculam nada (sessão do usuário e depuração): uma atribuição só com elas não é
# fórmula, e a função não entra como auxiliar
_NOT_CALC_FUNCTIONS = _words("fc_getsession fc_putsession fc_sessionname fc_debug")
_CALC_FUNCTIONS = _words(
    "round trunc ceil ceiling floor abs power sqrt mod div greatest least sum avg exp ln log"
)

_REF_RE = re.compile(r"(?<![\w$.])(\$\d+|[A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)")
_CAST_RE = re.compile(
    r"::\s*(?:double\s+precision|character\s+varying|bit\s+varying"
    r"|timestamp(?:\s+with(?:out)?\s+time\s+zone)?|time(?:\s+with(?:out)?\s+time\s+zone)?"
    r"|[A-Za-z_][\w.]*)(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?(?:\s*\[\s*\])*",
    re.IGNORECASE,
)
_OP_CHARS = re.compile(r"[-+*/%^]")
_UNARY_KEYWORD = re.compile(
    r"(?:^|[^\w$])(?:then|else|when|and|or|not|return|in|between|case|select|distinct|is|like|"
    r"elsif|elseif|if|while|any|all|values|using|by)\s*$",
    re.IGNORECASE,
)
_UNARY_PUNCT = re.compile(r"(?:^|[(,=<>!|:+\-*/%^\[.])\s*$")
_TRIVIAL = re.compile(
    r"[-+]?\d+(?:\.\d+)?|null|true|false|current_date|current_timestamp|now\s*\(\s*\)",
    re.IGNORECASE,
)
_SEMI = re.compile(";")
_TRAILING_ALIAS = re.compile(r"\s+as\s+[A-Za-z_]\w*\s*$", re.IGNORECASE)
_LEAD = re.compile(
    r"\s*(?:(?P<for>for)\s+(?P<fvar>[A-Za-z_]\w*)\s+in\b"
    r"|(?P<cond>if|elsif|elseif|when|while)\b"
    r"|(?P<case>case)\b"
    r"|(?P<simple>begin|else|loop|declare|exception)\b)",
    re.IGNORECASE,
)
_TOP_TOKEN = re.compile(
    r"\(|\)|\bcase\b|\bend\b|\b(?:then|loop|when|into|from|where|group|order|having|limit|"
    r"union|returning)\b",
    re.IGNORECASE,
)
_ASSIGN = re.compile(
    r"\s*(?P<target>\$?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*(?P<op>:=|=)(?!=)\s*", re.DOTALL
)
_FIRST_WORD = re.compile(r"\s*([A-Za-z_]\w*)")
_ALIAS_FOR = re.compile(r"\b([A-Za-z_]\w*)\s+alias\s+for\s+\$(\d+)", re.IGNORECASE)
_RECORD_DECL = re.compile(r"\b([A-Za-z_]\w*)\s+(?:record\b|[\w.]+%rowtype\b)", re.IGNORECASE)
_SELECT_TAIL = frozenset(
    ("from", "where", "group", "order", "having", "limit", "union", "returning")
)
_SQL_START = frozenset(("select", "with", "execute"))


@dataclass(frozen=True, slots=True)
class _Text:
    """Um trecho do texto: `orig` sem comentários (strings intactas) e `masked` (strings e
    comentários em branco), do mesmo tamanho, para procurar operadores sem se enganar."""

    orig: str
    masked: str


def _blank(text: str, spans: list[Any], kinds: frozenset[str]) -> str:
    parts: list[str] = []
    cursor = 0
    for span in spans:
        if span.kind not in kinds:
            continue
        parts.append(text[cursor : span.start])
        inner = text[span.start : span.end]
        parts.append("".join(c if c == "\n" else " " for c in inner))
        cursor = span.end
    parts.append(text[cursor:])
    return "".join(parts)


_COMMENTS = frozenset(("comment_line", "comment_block"))


def _make_text(raw: str) -> _Text:
    spans = _scan_spans(raw)
    return _Text(_blank(raw, spans, _COMMENTS), _mask(raw, spans))


def _slice(plain: str, masked: str, start: int, end: int) -> _Text:
    """O trecho `[start:end]` sem espaços nas pontas, mantendo `orig` e `masked` do mesmo
    tamanho (o corte vem de `plain`, que guarda as strings; `masked` as apaga)."""
    segment = plain[start:end]
    lead = len(segment) - len(segment.lstrip())
    stop = len(segment.rstrip())
    return _Text(segment[lead:stop], masked[start + lead : start + stop])


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _cast_spans(masked: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _CAST_RE.finditer(masked)]


def _inside(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(s <= start and end <= e for s, e in spans)


def _match_paren(masked: str, open_at: int) -> int:
    """Posição do `)` que fecha o `(` em `open_at`; -1 se não fecha."""
    depth = 0
    for i in range(open_at, len(masked)):
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_top(masked: str, start: int, end: int) -> list[tuple[int, int]]:
    """Fatias separadas por vírgula fora de parênteses (posições em `masked`)."""
    pieces: list[tuple[int, int]] = []
    depth = 0
    cursor = start
    for i in range(start, end):
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            pieces.append((cursor, i))
            cursor = i + 1
    pieces.append((cursor, end))
    return pieces


def _find_top(masked: str, keywords: frozenset[str], start: int, end: int) -> re.Match[str] | None:
    """A 1ª palavra de `keywords` fora de parênteses e de `case ... end`."""
    depth = 0
    cases = 0
    for m in _TOP_TOKEN.finditer(masked, start, end):
        token = m.group(0).lower()
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif token == "case":
            cases += 1
        elif token == "end":
            cases = max(0, cases - 1)
        elif depth == 0 and cases == 0 and token in keywords:
            return m
    return None


def _binary_ops(masked: str, skip: list[tuple[int, int]]) -> list[tuple[int, str]]:
    """Operadores aritméticos **binários** de `masked` (posição, caractere). Sinal (`-1`),
    `count(*)`, `rec.*`, `->` e o que está num cast não contam."""
    blanked = list(masked)
    for s, e in skip:
        for i in range(s, e):
            blanked[i] = " "
    text = "".join(blanked)
    found: list[tuple[int, str]] = []
    for m in _OP_CHARS.finditer(text):
        i = m.start()
        ch = m.group(0)
        prefix = text[max(0, i - 30) : i]
        if _UNARY_PUNCT.search(prefix) or _UNARY_KEYWORD.search(prefix):
            continue
        following = text[i + 1 : i + 2]
        if ch == "-" and following == ">":
            continue
        if (
            ch in "+-"
            and prefix.rstrip().endswith(("e", "E"))
            and re.search(r"\d\.?\d*[eE]$", prefix)
        ):
            continue
        found.append((i, ch))
    return found


@dataclass(frozen=True, slots=True)
class _Ref:
    start: int
    end: int
    parts: tuple[str, ...]  # minúsculas
    is_call: bool
    arity: int = 0

    @property
    def dotted(self) -> str:
        return ".".join(self.parts)


def _refs(masked: str, skip: list[tuple[int, int]]) -> list[_Ref]:
    found: list[_Ref] = []
    for m in _REF_RE.finditer(masked):
        if _inside(skip, m.start(), m.end()):
            continue
        parts = tuple(p.strip().lower() for p in m.group(0).split("."))
        j = m.end()
        while j < len(masked) and masked[j] in " \t\r\n":
            j += 1
        is_call = j < len(masked) and masked[j] == "("
        last = parts[-1]
        if is_call and len(parts) == 1 and last in _NOT_CALLS:
            continue
        if not is_call and len(parts) == 1 and last in _KEYWORDS:
            continue
        arity = 0
        if is_call:
            close = _match_paren(masked, j)
            if close < 0:
                continue
            inner = masked[j + 1 : close]
            arity = len(_split_top(masked, j + 1, close)) if inner.strip() else 0
        found.append(_Ref(m.start(), m.end(), parts, is_call, arity))
    return found


def _is_calc_call(ref: _Ref) -> bool:
    name = ref.parts[-1]
    return ref.is_call and (
        name in _CALC_FUNCTIONS or (name.startswith("fc_") and name not in _NOT_CALC_FUNCTIONS)
    )


def _is_formula(masked: str, skip: list[tuple[int, int]], refs: list[_Ref]) -> bool:
    """Tem operador aritmético binário ou chama função de conta (round, trunc, fc_*...)."""
    return any(_is_calc_call(r) for r in refs) or bool(_binary_ops(masked, skip))


def _is_trivial(masked_rhs: str) -> bool:
    s = masked_rhs.strip().rstrip(";").strip().lower()
    return s == "" or _TRIVIAL.fullmatch(s) is not None


# --------------------------------------------------------------------------- varredura do corpo


@dataclass(slots=True)
class _Item:
    kind: str  # assign | select_into | return
    targets: list[str]
    texts: list[_Text]  # assign/return: 1; select_into: um por item da lista do SELECT
    line: int
    end_line: int
    select_sql: str | None = None  # select_into: o SELECT sem o INTO


@dataclass(frozen=True, slots=True)
class _RecordSource:
    name: str
    line: int
    select_sql: str | None  # None: registro sem consulta lida
    kind: str  # for_loop | select_into


@dataclass(frozen=True, slots=True)
class _Scope:
    """O `FROM` de um trecho SQL: apelido → (schema, tabela), e as tabelas que a consulta
    correspondente do mapa (`routine_queries`) já resolveu."""

    aliases: dict[str, tuple[str | None, str]]
    query_tables: tuple[str, ...]


def _peel(masked: str, start: int, end: int) -> tuple[int, list[tuple[str, int, int, int]]] | None:
    """Tira o prefixo de controle de uma instrução (`if ... then`, `else`, `begin`, `for ...
    loop`, `when ... then`, `case x when y then`) e devolve onde a instrução de verdade começa
    e os laços `for r in <select>` que o prefixo abriu: `(variável, posição do for, início da
    consulta, fim da consulta)`.
    `None` quando o prefixo não fecha dentro do segmento."""
    pos = start
    loops: list[tuple[str, int, int, int]] = []
    while pos < end:
        m = _LEAD.match(masked, pos, end)
        if m is None:
            break
        if m.group("for"):
            loop_kw = _find_top(masked, frozenset(("loop",)), m.end(), end)
            if loop_kw is None:
                return None
            loops.append((m.group("fvar").lower(), m.start("for"), m.end(), loop_kw.start()))
            pos = loop_kw.end()
        elif m.group("cond"):
            word = m.group("cond").lower()
            wanted = frozenset(("loop",)) if word == "while" else frozenset(("then",))
            hit = _find_top(masked, wanted, m.end(), end)
            if hit is None:
                return None
            pos = hit.end()
        elif m.group("case"):
            when = _find_top(masked, frozenset(("when",)), m.end(), end)
            then = _find_top(masked, frozenset(("then",)), when.end(), end) if when else None
            if then is None:
                return None
            pos = then.end()
        else:
            pos = m.end()
    return pos, loops


def _word_at(masked: str, pos: int, end: int) -> str:
    m = _FIRST_WORD.match(masked, pos, end)
    return m.group(1).lower() if m else ""


# --------------------------------------------------------------------------- resolvedor


@dataclass(frozen=True, slots=True)
class _OutColumn:
    table_id: str | None
    column: str | None
    computed: bool = False
    expression: str | None = None  # a expressão do campo calculado (quando `computed`)


@dataclass(frozen=True, slots=True)
class _FnCandidate:
    node_id: str
    min_args: int
    max_args: int


class _Resolver:
    """Índices do grafo compartilhados por todas as funções de um dossiê (uma conexão, nada
    reaberto por função)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._attrs: dict[str, dict[str, Any]] = {}
        self._columns: dict[str, frozenset[str] | None] = {}
        self._tables_by_name: dict[str, list[str]] | None = None
        self._functions: dict[str, list[_FnCandidate]] | None = None
        self._models: dict[str, _Model | None] = {}
        self._cadastre: dict[
            str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str | None]
        ] = {}

    def attrs(self, node_id: str) -> dict[str, Any]:
        if node_id not in self._attrs:
            self._attrs[node_id] = vo.node_attrs(self.conn, node_id)
        return self._attrs[node_id]

    def columns(self, table_id: str) -> frozenset[str] | None:
        if table_id not in self._columns:
            raw = self.attrs(table_id).get("columns")
            self._columns[table_id] = (
                frozenset(str(c).lower() for c in raw) if isinstance(raw, list) and raw else None
            )
        return self._columns[table_id]

    def column_info(self, table_id: str, column: str) -> tuple[str | None, str | None]:
        catalog = self.attrs(table_id).get("columns_catalog") or {}
        info = catalog.get(column) or catalog.get(column.lower()) or {}
        label, description = info.get("label"), info.get("description")
        return (
            str(label).strip() if isinstance(label, str) and label.strip() else None,
            str(description).strip()
            if isinstance(description, str) and description.strip()
            else None,
        )

    def tables_by_name(self) -> dict[str, list[str]]:
        if self._tables_by_name is None:
            index: dict[str, list[str]] = {}
            for (table_id,) in self.conn.execute("SELECT id FROM nodes WHERE label = 'Table'"):
                index.setdefault(table_id.rpartition(".")[2].lower(), []).append(table_id)
            self._tables_by_name = index
        return self._tables_by_name

    def table_exists(self, table_id: str) -> bool:
        return table_id in self.tables_by_name().get(table_id.rpartition(".")[2].lower(), ())

    def table_for(self, rel: str, schema: str | None, hints: Iterable[str]) -> str | None:
        """O `Table:` de um nome de tabela: pelo schema, pelas tabelas que a consulta/função já
        resolveram (`hints`, que seguem o `search_path`) e, por último, pelo nome único."""
        rel = rel.lower()
        candidates = self.tables_by_name().get(rel, [])
        if schema:
            wanted = f"Table:{schema.lower()}.{rel}"
            return wanted if wanted in candidates else None
        if len(candidates) == 1:
            return candidates[0]
        preferred = [c for c in candidates if c in set(hints)]
        return preferred[0] if len(preferred) == 1 else None

    def functions_named(self, name: str) -> list[_FnCandidate]:
        if self._functions is None:
            index: dict[str, list[_FnCandidate]] = {}
            rows = self.conn.execute(
                "SELECT id, json_extract(attrs, '$.name'), json_extract(attrs, '$.min_args'), "
                "json_extract(attrs, '$.max_args'), json_extract(attrs, '$.args') "
                "FROM nodes WHERE label = 'PgFunction' ORDER BY id"
            )
            for node_id, fname, min_args, max_args, args in rows:
                if not fname:
                    continue
                total = len(json.loads(args)) if isinstance(args, str) and args else 0
                low = int(min_args) if min_args is not None else total
                high = int(max_args) if max_args is not None else total
                index.setdefault(str(fname).lower(), []).append(_FnCandidate(node_id, low, high))
            self._functions = index
        return self._functions.get(name.lower(), [])

    def model(self, routine_id: str) -> "_Model | None":
        if routine_id not in self._models:
            source = routine_store.read_routine(self.conn, routine_id)
            self._models[routine_id] = _Model(self, routine_id, source.ddl) if source else None
        return self._models[routine_id]

    # ----- cadastro (tabela → quem grava → tela → menu), com os helpers da origem de valor

    def cadastre(
        self, table_id: str
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str | None]:
        """`(writers, screens, menus, motivo)`; `motivo` só quando não achou o menu."""
        if table_id in self._cadastre:
            return self._cadastre[table_id]
        steps: list[vo.Step] = []
        vo.climb_to_screen(self.conn, table_id, "", steps)
        writers = _unique(s.node for s in steps if s.kind == "writer_dao" and s.node is not None)
        screens = _unique(s.node for s in steps if s.kind == "screen" and s.node is not None)
        if not writers:
            writers = tuple(w for w in screens if w.startswith("File:"))
        menu_steps = [s for s in steps if s.kind == "menu" and s.node is not None]
        menus = _unique(
            str(path)
            for s in menu_steps
            if s.node is not None
            and isinstance(path := self.attrs(s.node).get("menu_path"), str)
            and path
        )
        reason: str | None = None
        if not menus:
            if menu_steps:
                reason = "menu sem caminho: rode o scan com `--catalog-db` (dicionário do e-cidade)"
            elif screens:
                reason = "tela de cadastro sem item de menu: " + ", ".join(
                    vo.short(s) for s in screens[:3]
                )
            else:
                hints = [vo.short(s.node) for s in steps if s.kind == "hypothesis" and s.node]
                reason = "tabela sem tela de cadastro no mapa"
                if hints:
                    reason += f" (hipótese pelo nome, a confirmar: {', '.join(hints[:3])})"
        result = (writers, screens, menus, reason)
        self._cadastre[table_id] = result
        return result


Resolver = _Resolver  # nome público: a busca por termo e o dossiê do menu a usam


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return tuple(seen)


# --------------------------------------------------------------------------- uma função


def _colref_parts(ref: Any) -> tuple[str | None, str]:
    """`(qualificador, coluna)` de um `ColumnRef`; a coluna é `*` para `t.*`."""
    fields = list(ref.fields or ())
    last = fields[-1] if fields else None
    column = "*" if isinstance(last, ast.A_Star) else str(getattr(last, "sval", "") or "").lower()
    qualifier = (
        str(fields[-2].sval).lower()
        if len(fields) > 1 and isinstance(fields[-2], ast.String)
        else None
    )
    return qualifier, column


def _aliases_of(stmt_from: Iterable[Any] | None) -> dict[str, tuple[str | None, str]]:
    aliases: dict[str, tuple[str | None, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, ast.RangeVar) and node.relname:
            rel = node.relname.lower()
            schema = node.schemaname.lower() if node.schemaname else None
            alias = node.alias.aliasname.lower() if node.alias and node.alias.aliasname else rel
            aliases[alias] = (schema, rel)
            aliases.setdefault(rel, (schema, rel))
        elif isinstance(node, ast.JoinExpr):
            walk(node.larg)
            walk(node.rarg)

    for item in stmt_from or ():
        walk(item)
    return aliases


class _Model:
    """O que se lê de uma função: itens do corpo, registros, variáveis e as fórmulas."""

    def __init__(self, res: _Resolver, routine_id: str, ddl: str) -> None:
        self.res = res
        self.routine_id = routine_id
        self.ddl = ddl
        self.lines = routine_store.split_lines(ddl)
        self.items: list[_Item] = []
        self.records: dict[str, list[_RecordSource]] = {}
        self.writes: dict[str, list[tuple[int, _Item, int]]] = {}  # var → [(linha, item, posição)]
        self.params: dict[str, int | None] = {}
        self.known: set[str] = set()
        self.record_vars: set[str] = set()
        self._notes: list[tuple[str, str]] = []
        self._select_cache: dict[int, tuple[Any, _Scope]] = {}
        self._out_cache: dict[tuple[int, str], _OutColumn | None] = {}
        self._queries = self._read_queries()
        self.routine_tables = self._read_routine_tables()
        self.notes_by_formula: dict[tuple[int, str, str], list[tuple[str, str]]] = {}
        self._formulas: list[Formula] | None = None
        self._scan()

    # ----- leitura do mapa

    def _read_queries(self) -> list[tuple[int, str, int, int, str, list[str]]]:
        if not routine_store.has_fields(self.res.conn):
            return []
        rows = self.res.conn.execute(
            f"SELECT seq, kind, line, end_line, text, tables FROM {routine_store.QUERIES_TABLE} "  # noqa: S608
            "WHERE routine_id = ? ORDER BY seq",
            (self.routine_id,),
        ).fetchall()
        return [(r[0], r[1], r[2], r[3], r[4], json.loads(r[5])) for r in rows]

    def _read_routine_tables(self) -> set[str]:
        tables: set[str] = set()
        for _seq, _kind, _line, _end, _text, ids in self._queries:
            tables.update(ids)
        return tables

    def query_tables_at(self, line: int) -> tuple[str, ...]:
        found: dict[str, None] = {}
        for _seq, _kind, first, last, _text, ids in self._queries:
            if first <= line <= last:
                for table_id in ids:
                    found.setdefault(table_id, None)
        return tuple(found)

    # ----- varredura

    def _scan(self) -> None:
        body_range = _function_body(self.ddl)
        if body_range is None:
            return
        start, end = body_range
        body = self.ddl[start:end]
        spans = _scan_spans(body)
        masked = _mask(body, spans)
        plain = _blank(body, spans, _COMMENTS)
        line_of = vo.line_of_factory(self.ddl, start)
        self._read_declarations(masked)
        pos = 0
        segments: list[tuple[int, int]] = []
        for m in _SEMI.finditer(masked):
            segments.append((pos, m.start()))
            pos = m.end()
        segments.append((pos, len(masked)))
        for seg_start, seg_end in segments:
            peeled = _peel(masked, seg_start, seg_end)
            if peeled is None:
                continue
            first, loops = peeled
            for var, for_pos, sel_start, sel_end in loops:
                select = plain[sel_start:sel_end].strip()
                if _word_at(masked, sel_start, sel_end) in _SQL_START or select.startswith("("):
                    self.record_vars.add(var)
                    self.known.add(var)
                    self.records.setdefault(var, []).append(
                        _RecordSource(var, line_of(for_pos), select, "for_loop")
                    )
            self._classify(plain, masked, first, seg_end, line_of)

    def _read_declarations(self, masked: str) -> None:
        profile = routine_store.read_profile(self.res.conn, self.routine_id) or {}
        for param in profile.get("params") or ():
            name = str(param.get("name") or "").lower()
            if name:
                self.known.add(name)
                if str(param.get("mode") or "IN").upper() in ("IN", "INOUT", "VARIADIC"):
                    self.params[name] = None
        for name in profile.get("declared") or ():
            self.known.add(str(name).lower())
        for m in _ALIAS_FOR.finditer(masked):
            name = m.group(1).lower()
            self.params[name] = int(m.group(2))
            self.known.add(name)
        declare = re.search(r"\bdeclare\b", masked, re.IGNORECASE)
        if declare is not None:
            begin = re.search(r"\bbegin\b", masked[declare.end() :], re.IGNORECASE)
            region = masked[declare.end() : declare.end() + begin.start()] if begin else ""
            for chunk in region.split(";"):
                first_word = _FIRST_WORD.match(chunk)
                if first_word:
                    self.known.add(first_word.group(1).lower())
        for m in _RECORD_DECL.finditer(masked):
            self.record_vars.add(m.group(1).lower())
            self.known.add(m.group(1).lower())

    def _classify(
        self,
        plain: str,
        masked: str,
        first: int,
        end: int,
        line_of: Any,
    ) -> None:
        found = _FIRST_WORD.match(masked, first, end)
        if found is None:
            return
        word = found.group(1).lower()
        word_start = found.start(1)
        if word == "select":
            self._select_into(plain, masked, word_start, end, line_of)
            return
        if word == "return":
            after = found.end(1)
            if _word_at(masked, after, end) in ("next", "query") or not masked[after:end].strip():
                return
            self.items.append(
                _Item(
                    "return",
                    ["RETURN"],
                    [_slice(plain, masked, after, end)],
                    line_of(word_start),
                    line_of(end),
                )
            )
            return
        m = _ASSIGN.match(masked, first, end)
        if m is None:
            return
        target = m.group("target").lower()
        if m.group("op") == "=" and ("." in target or target not in self.known):
            return
        text = _slice(plain, masked, m.end(), end)
        if not text.orig:
            return
        item = _Item("assign", [target], [text], line_of(m.start("target")), line_of(end))
        self.items.append(item)
        if "." not in target:
            self.writes.setdefault(target, []).append((item.line, item, 0))

    def _select_into(self, plain: str, masked: str, sel: int, end: int, line_of: Any) -> None:
        into = _find_top(masked, frozenset(("into",)), sel, end)
        if into is None:
            return
        tl_start = sel + len("select")
        distinct = re.match(r"\s*(?:distinct|all)\b", masked[tl_start:end], re.IGNORECASE)
        if distinct:
            tl_start += distinct.end()
        tail = _find_top(masked, _SELECT_TAIL, into.end(), end)
        vars_end = tail.start() if tail else end
        names_text = re.sub(r"^\s*strict\b", "", masked[into.end() : vars_end], flags=re.IGNORECASE)
        names = [n.strip().lower() for n in names_text.split(",") if n.strip()]
        if not names:
            return
        texts = []
        for a, b in _split_top(masked, tl_start, into.start()):
            if not plain[a:b].strip():
                continue
            alias = _TRAILING_ALIAS.search(masked, a, b)
            texts.append(_slice(plain, masked, a, alias.start() if alias else b))
        select_sql = (plain[sel : into.start()] + " " + plain[vars_end:end]).strip()
        item = _Item("select_into", names, texts, line_of(sel), line_of(end), select_sql)
        self.items.append(item)
        for position, name in enumerate(names):
            if "." in name:
                continue
            self.writes.setdefault(name, []).append((item.line, item, position))
            if name in self.record_vars or (len(names) == 1 and texts and texts[0].masked == "*"):
                self.record_vars.add(name)
                self.known.add(name)
                self.records.setdefault(name, []).append(
                    _RecordSource(name, item.line, select_sql, "select_into")
                )

    # ----- SQL isolado

    def _parse_select(self, sql: str) -> Any:
        try:
            stmts = pglast.parse_sql(sql)
        except pglast.parser.ParseError:
            return None
        return next((s.stmt for s in stmts if isinstance(s.stmt, ast.SelectStmt)), None)

    def select_scope(self, item: _Item) -> _Scope:
        key = id(item)
        if key not in self._select_cache:
            tree = self._parse_select(item.select_sql or "")
            aliases = _aliases_of(tree.fromClause) if tree is not None else {}
            self._select_cache[key] = (
                tree,
                _Scope(aliases, self.query_tables_at(item.line)),
            )
        return self._select_cache[key][1]

    # ----- registros de laço / SELECT * INTO rec

    def record_field(
        self, record: str, name: str, before_line: int
    ) -> tuple[_OutColumn | None, int]:
        sources = [s for s in self.records.get(record, ()) if s.line <= before_line]
        if not sources:
            return None, before_line
        source = max(sources, key=lambda s: s.line)
        key = (source.line, name)
        if key not in self._out_cache:
            self._out_cache[key] = self._resolve_out(source, name)
        return self._out_cache[key], source.line

    def _resolve_out(self, source: _RecordSource, name: str) -> _OutColumn | None:
        hints = (*self.query_tables_at(source.line), *self.routine_tables)
        tree = self._parse_select(source.select_sql or "")
        if tree is not None:
            found = self._out_column(tree, name, hints, 0)
            if found is not None:
                return found
        # sem árvore (ou sem resposta): a única tabela da consulta que tem a coluna
        owners = [
            t
            for t in self.query_tables_at(source.line)
            if (cols := self.res.columns(t)) is not None and name in cols
        ]
        return _OutColumn(owners[0], name) if len(owners) == 1 else None

    def _out_column(
        self, select: Any, name: str, hints: Sequence[str], depth: int
    ) -> _OutColumn | None:
        """De onde sai a coluna `name` do resultado de `select` (descendo por subconsultas e
        `UNION`)."""
        if depth > MAX_SUBSELECT_DEPTH or not isinstance(select, ast.SelectStmt):
            return None
        if select.larg is not None:
            return self._out_column(select.larg, name, hints, depth + 1) or self._out_column(
                select.rarg, name, hints, depth + 1
            )
        items: list[Any] = []

        def flatten(node: Any) -> None:
            if isinstance(node, ast.JoinExpr):
                flatten(node.larg)
                flatten(node.rarg)
            else:
                items.append(node)

        for from_item in select.fromClause or ():
            flatten(from_item)

        def from_column(node: Any, column: str) -> _OutColumn | None:
            if isinstance(node, ast.RangeVar) and node.relname:
                table_id = self.res.table_for(node.relname, node.schemaname, hints)
                if table_id is None:
                    return None
                cols = self.res.columns(table_id)
                if cols is not None and column not in cols:
                    return None
                if cols is None and len(items) > 1:
                    return None
                return _OutColumn(table_id, column)
            if isinstance(node, ast.RangeSubselect):
                return self._out_column(node.subquery, column, hints, depth + 1)
            return None

        def alias_of(node: Any) -> str:
            if isinstance(node, ast.RangeVar):
                if node.alias and node.alias.aliasname:
                    return str(node.alias.aliasname).lower()
                return str(node.relname or "").lower()
            alias = getattr(node, "alias", None)
            return str(alias.aliasname).lower() if alias is not None and alias.aliasname else ""

        def find_in(qualifier: str | None, column: str) -> _OutColumn | None:
            for node in items:
                if qualifier is None or alias_of(node) == qualifier:
                    found = from_column(node, column)
                    if found is not None:
                        return found
            return None

        for target in select.targetList or ():
            if not isinstance(target, ast.ResTarget) or target.val is None:
                continue
            value = target.val
            if isinstance(value, ast.ColumnRef):
                qualifier, column = _colref_parts(value)
                if column == "*":
                    found = find_in(qualifier, name)
                    if found is not None:
                        return found
                    continue
                if (target.name or column).lower() != name:
                    continue
                return find_in(qualifier, column) or _OutColumn(None, None, computed=False)
            if (target.name or "").lower() == name:
                try:
                    expression = _squash(RawStream()(value))
                except Exception:  # noqa: BLE001 - nó que o RawStream não reescreve
                    expression = None
                return _OutColumn(None, None, computed=True, expression=expression)
        return None

    # ----- entradas de uma expressão

    def note(self, what: str, reason: str) -> None:
        if (what, reason) not in self._notes:
            self._notes.append((what, reason))

    def _column_input(
        self, name: str, scope: _Scope, qualifier: str | None, column: str, line: int
    ) -> FormulaInput | None:
        hints = (*scope.query_tables, *self.routine_tables)
        if qualifier:
            target = scope.aliases.get(qualifier)
            if target is None:
                bare = [t for t in scope.query_tables if t.rpartition(".")[2].lower() == qualifier]
                table_id = bare[0] if len(bare) == 1 else None
            else:
                table_id = self.res.table_for(target[1], target[0], hints)
            if table_id is None:
                return None
            cols = self.res.columns(table_id)
            if cols is not None and column not in cols:
                return None
            return FormulaInput("column", name, table_id, column, via_line=line)
        tables: list[str] = []
        for schema, rel in scope.aliases.values():
            table_id = self.res.table_for(rel, schema, hints)
            if table_id is not None and table_id not in tables:
                tables.append(table_id)
        if not tables:
            tables = list(scope.query_tables)
        owners = [t for t in tables if (c := self.res.columns(t)) is not None and column in c]
        if len(owners) == 1:
            return FormulaInput("column", name, owners[0], column, via_line=line)
        if not owners and len(tables) == 1 and self.res.columns(tables[0]) is None:
            return FormulaInput("column", name, tables[0], column, via_line=line)
        return None

    def inputs_of(
        self,
        text: _Text,
        line: int,
        *,
        scope: _Scope | None,
        target: str | None,
    ) -> tuple[list[FormulaInput], list[_Ref], list[tuple[int, int]]]:
        skip = _cast_spans(text.masked)
        refs = _refs(text.masked, skip)
        inputs: list[FormulaInput] = []
        seen: set[tuple[str, str, str | None, str | None, str | None]] = set()
        for ref in refs:
            for found in self._ref_inputs(ref, line, scope, target, text):
                key = (found.kind, found.name, found.table_id, found.column, found.routine_id)
                if key not in seen:
                    seen.add(key)
                    inputs.append(found)
        return inputs, refs, skip

    def _ref_inputs(
        self, ref: _Ref, line: int, scope: _Scope | None, target: str | None, text: _Text
    ) -> list[FormulaInput]:
        name = ref.dotted
        if ref.is_call:
            return self._call_input(ref, text)
        if len(ref.parts) == 1:
            single = ref.parts[0]
            if target is not None and single == target:
                return [FormulaInput("variable", single)]
            if scope is not None:
                column = self._column_input(single, scope, None, single, line)
                if column is not None:
                    return [column]
            if single.startswith("$"):
                return [FormulaInput("parameter", single)]
            if single in self.params:
                return [FormulaInput("parameter", single)]
            if single in self.known:
                return self.trace_variable(single, line, single, 0)
            return []
        qualifier, column_name = ref.parts[-2], ref.parts[-1]
        if len(ref.parts) == 2 and qualifier in ("new", "old"):
            self.note(name, "linha NEW/OLD do gatilho: a tabela do gatilho não é resolvida")
            return [FormulaInput("record_field", name)]
        if len(ref.parts) == 2 and qualifier in self.records:
            found, via = self.record_field(qualifier, column_name, line)
            if found is not None and found.table_id and found.column:
                return [
                    FormulaInput("record_field", name, found.table_id, found.column, via_line=via)
                ]
            reason = (
                "campo calculado na própria consulta do laço"
                if found is not None and found.computed
                else "registro do laço sem a tabela de origem no mapa"
            )
            self.note(name, reason)
            computed = found.expression if found is not None and found.computed else None
            return [FormulaInput("record_field", name, via_line=via, expression=computed)]
        if scope is not None and len(ref.parts) == 2:
            column = self._column_input(name, scope, qualifier, column_name, line)
            if column is not None:
                return [column]
        if name.split(".")[0] in self.known:
            return [FormulaInput("variable", name)]
        return []

    def _call_input(self, ref: _Ref, text: _Text) -> list[FormulaInput]:
        name = ref.parts[-1]
        if name in _CALC_FUNCTIONS or name in _NOT_CALC_FUNCTIONS:
            return []  # round, trunc..., mesmo se a base tiver uma própria; sessão e depuração
        candidates = self.res.functions_named(name)
        if not candidates:
            if name.startswith("fc_"):
                self.note(f"{name}(...)", "função não encontrada no mapa")
                return [FormulaInput("function", name)]
            return []
        fitting = [c for c in candidates if c.min_args <= ref.arity <= c.max_args]
        if len(fitting) == 1:
            return [FormulaInput("function", name, routine_id=fitting[0].node_id)]
        if not fitting and len(candidates) == 1:
            return [FormulaInput("function", name, routine_id=candidates[0].node_id)]
        pool = fitting or candidates
        self.note(
            f"{name}(...)",
            "função ambígua (mais de uma assinatura serve): "
            + ", ".join(vo.short(c.node_id) for c in pool[:4]),
        )
        return [FormulaInput("function", name)]

    # ----- variável → de onde veio o valor

    def trace_variable(
        self, name: str, before_line: int, top: str, depth: int
    ) -> list[FormulaInput]:
        """As entradas de uma variável: as últimas atribuições com dado antes de `before_line`
        (literais como `:= 0` só inicializam e são puladas). Atribuição por **conta** é a
        própria fórmula, listada à parte: a variável fica como `variable`."""
        if depth > MAX_VAR_DEPTH:
            return [FormulaInput("variable", top)]
        found: list[FormulaInput] = []
        carrying = 0
        for write_line, item, position in reversed(self.writes.get(name, [])):
            if write_line >= before_line:
                continue
            if carrying >= MAX_VAR_SOURCES:
                break
            if item.kind == "assign":
                text = item.texts[0]
                if _is_trivial(text.masked):
                    continue
                carrying += 1
                found.extend(self._assign_source(item, top, write_line, depth))
            else:
                carrying += 1
                found.extend(self._select_source(item, position, top, write_line))
        if not found:
            if name in self.params:
                return [FormulaInput("parameter", top)]
            return [FormulaInput("variable", top)]
        unique: list[FormulaInput] = []
        for entry in found:
            if entry not in unique:
                unique.append(entry)
        return unique

    def _assign_source(
        self, item: _Item, top: str, write_line: int, depth: int
    ) -> list[FormulaInput]:
        text = item.texts[0]
        skip = _cast_spans(text.masked)
        refs = _refs(text.masked, skip)
        single = (
            len(refs) == 1
            and not refs[0].is_call
            and text.masked[refs[0].start : refs[0].end] == text.masked
        )
        if not single:
            return [FormulaInput("variable", top, via_line=write_line)]
        ref = refs[0]
        if len(ref.parts) == 2 and ref.parts[0] in self.records:
            found, via = self.record_field(ref.parts[0], ref.parts[1], write_line)
            if found is not None and found.table_id and found.column:
                return [
                    FormulaInput("column", top, found.table_id, found.column, via_line=write_line)
                ]
            reason = (
                "campo calculado na própria consulta do laço"
                if found is not None and found.computed
                else "registro do laço sem a tabela de origem no mapa"
            )
            self.note(top, reason)
            return [FormulaInput("variable", top, via_line=write_line)]
        if len(ref.parts) == 1:
            other = ref.parts[0]
            if other in self.params:
                return [FormulaInput("parameter", top, via_line=write_line)]
            if other in self.known and other != top:
                traced = self.trace_variable(other, write_line, top, depth + 1)
                return [
                    FormulaInput(
                        t.kind, top, t.table_id, t.column, t.routine_id, t.via_line or write_line
                    )
                    for t in traced
                ]
        return [FormulaInput("variable", top, via_line=write_line)]

    def _select_source(
        self, item: _Item, position: int, top: str, write_line: int
    ) -> list[FormulaInput]:
        if position >= len(item.texts):
            return [FormulaInput("variable", top, via_line=write_line)]
        scope = self.select_scope(item)
        text = item.texts[position]
        skip = _cast_spans(text.masked)
        columns: list[FormulaInput] = []
        for ref in _refs(text.masked, skip):
            if ref.is_call or len(ref.parts) > 2:
                continue
            qualifier = ref.parts[0] if len(ref.parts) == 2 else None
            found = self._column_input(top, scope, qualifier, ref.parts[-1], write_line)
            if found is not None and found not in columns:
                columns.append(found)
        if columns:
            return columns
        return [FormulaInput("variable", top, via_line=write_line)]

    # ----- fórmulas

    def formulas(self) -> list[Formula]:
        if self._formulas is None:
            built: list[Formula] = []
            for item in self.items:
                built.extend(self._formulas_of(item))
            built.extend(self._formulas_from_queries())
            built.sort(key=lambda f: (f.line, f.end_line, f.target))
            self._formulas = built
        return self._formulas

    def _make_formula(
        self,
        text: _Text,
        target: str,
        kind: str,
        line: int,
        end_line: int,
        scope: _Scope | None,
        self_target: str | None,
        force: bool = False,
    ) -> Formula | None:
        if not text.orig:
            return None
        skip = _cast_spans(text.masked)
        refs = _refs(text.masked, skip)
        if not force and not _is_formula(text.masked, skip, refs):
            return None
        self._notes = []
        inputs, refs, skip = self.inputs_of(text, line, scope=scope, target=self_target)
        expression = _squash(text.orig)
        if len(expression) > MAX_EXPRESSION:
            expression = expression[:MAX_EXPRESSION] + "…"
        formula = Formula(
            routine_id=self.routine_id,
            target=target,
            expression=expression,
            line=line,
            end_line=end_line,
            kind=kind,
            inputs=tuple(inputs),
            readable=self._readable(text, refs, skip, inputs),
            excerpt=self._excerpt(line, end_line),
        )
        formula = replace(
            formula, topic=topics.topic_of(formula, column_label=self._column_label(formula, scope))
        )
        if self._notes:
            self.notes_by_formula[(line, target, kind)] = list(self._notes)
        return formula

    def _column_label(self, formula: Formula, scope: _Scope | None) -> str | None:
        """O nome no dicionário da coluna que a fórmula grava (`UPDATE SET`/`INSERT`)."""
        if formula.kind not in ("update_set", "insert_value") or "." not in formula.target:
            return None
        rel, _sep, column = formula.target.partition(".")
        hints = scope.query_tables if scope is not None else ()
        table_id = self.res.table_for(rel, None, hints)
        return self.res.column_info(table_id, column)[0] if table_id else None

    def _formulas_of(self, item: _Item) -> list[Formula]:
        if item.kind == "assign":
            target = item.targets[0]
            made = self._make_formula(
                item.texts[0], target, "assign", item.line, item.end_line, None, target
            )
            return [made] if made else []
        if item.kind == "return":
            made = self._make_formula(
                item.texts[0], "RETURN", "return", item.line, item.end_line, None, None
            )
            return [made] if made else []
        scope = self.select_scope(item)
        results: list[Formula] = []
        for position, name in enumerate(item.targets):
            if position >= len(item.texts) or name in self.record_vars:
                continue
            made = self._make_formula(
                item.texts[position], name, "select_into", item.line, item.end_line, scope, name
            )
            if made:
                results.append(made)
        return results

    def column_write(self, seq: int, target: str) -> Formula | None:
        """O que a instrução `seq` grava em `target` (`tabela.coluna`), **mesmo quando é só uma
        cópia** (sem operador nem função de conta): é a leitura do dossiê de um campo."""
        found = self._formulas_from_queries(only_seq=seq, target=target, force=True)
        return found[0] if found else None

    def _formulas_from_queries(
        self, *, only_seq: int | None = None, target: str | None = None, force: bool = False
    ) -> list[Formula]:
        results: list[Formula] = []
        for seq, kind, line, end_line, sql, tables in self._queries:
            if kind not in ("update", "insert") or (only_seq is not None and seq != only_seq):
                continue
            try:
                parsed = pglast.parse_sql(sql)
            except pglast.parser.ParseError:
                continue
            stmt = next(
                (s.stmt for s in parsed if type(s.stmt) in (ast.UpdateStmt, ast.InsertStmt)), None
            )
            if isinstance(stmt, ast.UpdateStmt) and stmt.relation is not None:
                aliases = _aliases_of([stmt.relation, *(stmt.fromClause or ())])
                scope = _Scope(aliases, tuple(tables))
                rel = (stmt.relation.relname or "").lower()
                for res_target in stmt.targetList or ():
                    name = f"{rel}.{(res_target.name or '').lower()}" if res_target.name else ""
                    if target is not None and name != target:
                        continue
                    if isinstance(res_target, ast.ResTarget) and res_target.name and res_target.val:
                        made = self._query_formula(
                            res_target.val, name, "update_set", line, end_line, scope, force
                        )
                        if made:
                            results.append(made)
            elif isinstance(stmt, ast.InsertStmt) and stmt.relation is not None:
                cols = [c.name for c in stmt.cols or () if isinstance(c, ast.ResTarget) and c.name]
                select = stmt.selectStmt
                while isinstance(select, ast.SelectStmt) and select.larg is not None:
                    select = select.larg  # `insert … select … union select …`: vale a 1ª consulta
                if not cols or not isinstance(select, ast.SelectStmt):
                    continue
                values: Sequence[Any]
                if select.valuesLists:
                    values = select.valuesLists[0]
                else:
                    values = [
                        t.val for t in select.targetList or () if isinstance(t, ast.ResTarget)
                    ]
                scope = _Scope(_aliases_of(select.fromClause), tuple(tables))
                rel = (stmt.relation.relname or "").lower()
                for column, value in zip(cols, values, strict=False):
                    if value is None or (
                        target is not None and f"{rel}.{column.lower()}" != target
                    ):
                        continue
                    made = self._query_formula(
                        value,
                        f"{rel}.{column.lower()}",
                        "insert_value",
                        line,
                        end_line,
                        scope,
                        force,
                    )
                    if made:
                        results.append(made)
        return results

    def _query_formula(
        self,
        node: Any,
        target: str,
        kind: str,
        line: int,
        end_line: int,
        scope: _Scope,
        force: bool = False,
    ) -> Formula | None:
        try:
            raw = RawStream()(node)
        except Exception:  # noqa: BLE001 - nó que o RawStream não reescreve: segue sem a fórmula
            return None
        return self._make_formula(_make_text(raw), target, kind, line, end_line, scope, None, force)

    # ----- apresentação

    def _excerpt(self, line: int, end_line: int) -> tuple[str, ...]:
        first = max(1, line - EXCERPT_MARGIN)
        last = min(len(self.lines), end_line + EXCERPT_MARGIN)
        numbered = [f"{n}: {self.lines[n - 1].rstrip()}" for n in range(first, last + 1)]
        if len(numbered) > MAX_EXCERPT_LINES:
            head = MAX_EXCERPT_LINES // 2
            tail = MAX_EXCERPT_LINES - head - 1
            return (*numbered[:head], "…", *numbered[-tail:])
        return tuple(numbered)

    def _display(self, name: str, shown_name: str, inputs: list[FormulaInput]) -> str | None:
        columns = _unique(
            f"{t.table_id}#{t.column}" for t in inputs if t.name == name and t.table_id and t.column
        )
        if not columns:
            return None
        shown: list[str] = []
        label: str | None = None
        for key in columns:
            table_id, _sep, column = key.partition("#")
            shown.append(f"{vo.short(table_id).rpartition('.')[2]}.{column}")
            if len(columns) == 1:
                label = self.res.column_info(table_id, column)[0]
        if len(columns) == 1:
            return f"{label or shown_name} [{shown[0]}]"
        extra = " ou ".join(shown[:3]) + (" ou …" if len(shown) > 3 else "")
        return f"{shown_name} [{extra}]"

    def _readable(
        self, text: _Text, refs: list[_Ref], skip: list[tuple[int, int]], inputs: list[FormulaInput]
    ) -> str:
        edits: list[tuple[int, int, str]] = [(s, e, "") for s, e in skip]
        for ref in refs:
            if ref.is_call:
                continue
            shown = self._display(ref.dotted, text.orig[ref.start : ref.end], inputs)
            if shown is not None:
                edits.append((ref.start, ref.end, shown))
        for position, op in _binary_ops(text.masked, skip):
            if op in "*/":
                edits.append((position, position + 1, "×" if op == "*" else "÷"))
        result = text.orig
        for start, end, replacement in sorted(edits, reverse=True):
            result = result[:start] + replacement + result[end:]
        result = _squash(result)
        return result[:MAX_EXPRESSION] + ("…" if len(result) > MAX_EXPRESSION else "")


# --------------------------------------------------------------------------- API pública


def resolve_routine_info(conn: sqlite3.Connection, value: str) -> routine_store.RoutineResolution:
    """Como `resolve_routine`, mas devolve a resolução inteira: `resolved_by`
    (`"unico"`, `"repasse"`, `"ambiguo"` ou `"nenhum"`) e, quando `"repasse"`, a cadeia de
    `CALLS_RPC` internas à família que levou ao destino. Nunca levanta por ambiguidade ou nome
    não encontrado — quem quiser só o id usa `resolve_routine`."""
    if not routine_store.has_routines(conn):
        raise QueryError(
            "este mapa não tem o texto das funções: rode o scan com o texto das funções"
        )
    text = value.strip()
    found = routine_store.find_routines(conn, text)
    if not found and "(" in text and "." not in text.partition("(")[0]:
        # `fc_calcula(int4,int4)` sem o schema: pelo nome, conferindo a assinatura. É uma
        # assinatura completa (só sem o schema), então não passa pelo repasse da família.
        wanted = text.partition("(")[2].replace(" ", "").lower()
        suffix_found = [
            r
            for r in routine_store.find_routines(conn, text.partition("(")[0])
            if r.node_id.partition("(")[2].replace(" ", "").lower() == wanted
        ]
        if suffix_found:
            if len(suffix_found) == 1:
                return routine_store.RoutineResolution(
                    suffix_found[0], tuple(suffix_found), "unico"
                )
            return routine_store.RoutineResolution(None, tuple(suffix_found), "ambiguo")
    return routine_store.resolve_family(conn, text)


def _require_resolved(
    conn: sqlite3.Connection, text: str, resolution: routine_store.RoutineResolution
) -> routine_store.RoutineSource:
    """O escolhido da resolução, ou levanta a mensagem certa (não achou / ambíguo, com o próximo
    passo em português: use o id completo de uma delas)."""
    if resolution.chosen is not None:
        return resolution.chosen
    if resolution.resolved_by == "nenhum":
        hints = routine_store.suggest_names(conn, text)
        more = f" Parecidas: {', '.join(hints)}." if hints else ""
        raise QueryError(f"função não encontrada: {text}.{more}")
    ids = [c.node_id for c in resolution.candidates]
    raise AmbiguousRoutineError(
        f"mais de uma função casa com {text!r}; use o id completo de uma delas: "
        f"{', '.join(ids[:10])}",
        ids,
    )


def resolve_routine(conn: sqlite3.Connection, value: str) -> str:
    """O id de uma função por id, `schema.nome(args)`, `schema.nome` ou só o nome. Quando o nome
    casa com várias assinaturas da mesma família e as outras só **repassam** para uma (`CALLS_RPC`
    interna), resolve sozinho para o destino. Levanta `QueryError` se não achar ou não
    houver o texto, e `AmbiguousRoutineError` (com as assinaturas) se continuar ambíguo."""
    text = value.strip()
    resolution = resolve_routine_info(conn, text)
    return _require_resolved(conn, text, resolution).node_id


def extract_formulas(conn: sqlite3.Connection, routine_id: str) -> list[Formula]:
    """Todas as fórmulas de uma função, em ordem de linha."""
    model = _Resolver(conn).model(routine_id)
    if model is None:
        raise QueryError(
            f"a função {routine_id} não tem texto no mapa: rode o scan com o texto das funções"
        )
    return list(model.formulas())


def formulas_of(res: "Resolver", routine_id: str) -> list[Formula] | None:
    """Como `extract_formulas`, mas recebendo um `_Resolver`/`Resolver` já aberto (índice por
    assunto): quem indexa o lote inteiro cria **um só** resolvedor (`Resolver(conn)`, nome
    público) e chama esta função por rotina, reaproveitando os índices do
    grafo (atributos, colunas, tabelas por nome) em vez de remontá-los por função como
    `extract_formulas` faz — essencial para o lote caber na meta de tempo. `None` quando o texto
    da função não parseia."""
    model = res.model(routine_id)
    return list(model.formulas()) if model is not None else None


def _priority(formula: Formula, included: set[str]) -> int:
    if any(i.kind == "function" and i.routine_id in included for i in formula.inputs):
        return 3
    if any(i.table_id and i.column for i in formula.inputs):
        return 2
    return 1


def calc_dossier(
    conn: sqlite3.Connection,
    routine_id: str,
    *,
    depth: int = DEFAULT_DEPTH,
    max_functions: int = DEFAULT_MAX_FUNCTIONS,
    max_formulas: int = DEFAULT_MAX_FORMULAS,
) -> CalcDossier:
    text = routine_id.strip()
    resolution = resolve_routine_info(conn, text)
    root = _require_resolved(conn, text, resolution).node_id
    resolved_by = resolution.resolved_by
    resolved_chain = resolution.chain if resolved_by == "repasse" else ()
    resolved_others = (
        tuple(c.node_id for c in resolution.candidates if c.node_id != root)
        if resolved_by == "repasse"
        else ()
    )
    res = _Resolver(conn)
    if res.model(root) is None:
        raise QueryError(
            f"a função {root} não tem texto no mapa: rode o scan com o texto das funções"
        )
    functions: list[str] = [root]
    untraced: list[tuple[str, str]] = []
    truncated = False
    frontier = [root]
    for _level in range(max(0, depth)):
        following: list[str] = []
        for routine in frontier:
            model = res.model(routine)
            if model is None:
                continue
            for formula in model.formulas():
                for entry in formula.inputs:
                    called = entry.routine_id
                    if entry.kind != "function" or called is None or called in functions:
                        continue
                    if len(functions) >= max_functions:
                        truncated = True
                        continue
                    functions.append(called)
                    following.append(called)
        frontier = following
        if not frontier:
            break
    # funções chamadas que não entraram (profundidade ou limite)
    omitted: set[str] = set()
    for routine in functions:
        model = res.model(routine)
        if model is None:
            if routine != root:
                untraced.append((vo.short(routine), "a função não tem texto no mapa"))
            continue
        for formula in model.formulas():
            for entry in formula.inputs:
                if (
                    entry.kind == "function"
                    and entry.routine_id
                    and entry.routine_id not in functions
                ):
                    omitted.add(entry.routine_id)
    if omitted:
        truncated = True
    # fórmulas: repartidas entre as funções, as que chamam auxiliar primeiro
    included = set(functions)
    ranked: dict[str, list[Formula]] = {}
    total = 0
    for routine in functions:
        model = res.model(routine)
        found = model.formulas() if model else []
        total += len(found)
        ranked[routine] = sorted(found, key=lambda f: (-_priority(f, included), f.line))
    # a raiz fica com até 60% das fórmulas; as auxiliares dividem o resto, uma de cada vez
    # (as que chamam auxiliar primeiro), e o que sobrar delas volta para a raiz
    auxiliaries = [routine for routine in functions if routine != root]
    root_quota = len(ranked[root])
    if auxiliaries:
        root_quota = min(root_quota, -(-max_formulas * 3 // 5))
    chosen: dict[str, list[Formula]] = {routine: [] for routine in functions}
    chosen[root] = ranked[root][:root_quota]
    budget = max_formulas - len(chosen[root])
    cursor = dict.fromkeys(auxiliaries, 0)
    while budget > 0 and any(cursor[r] < len(ranked[r]) for r in auxiliaries):
        for routine in auxiliaries:
            if budget > 0 and cursor[routine] < len(ranked[routine]):
                chosen[routine].append(ranked[routine][cursor[routine]])
                cursor[routine] += 1
                budget -= 1
    if budget > 0:
        more = ranked[root][root_quota : root_quota + budget]
        chosen[root] = [*chosen[root], *more]
    selected: list[Formula] = []
    for routine in functions:
        selected.extend(sorted(chosen[routine], key=lambda f: (f.line, f.end_line, f.target)))
    if len(selected) < total:
        truncated = True
    # o que ficou sem rastro dentro das fórmulas escolhidas
    for formula in selected:
        model = res.model(formula.routine_id)
        if model is None:
            continue
        for note in model.notes_by_formula.get((formula.line, formula.target, formula.kind), ()):
            if note not in untraced:
                untraced.append(note)
    inputs = _base_inputs(res, functions, selected, untraced)
    return CalcDossier(
        root=root,
        functions=tuple(functions),
        formulas=tuple(selected),
        inputs=tuple(inputs),
        untraced=tuple(untraced),
        truncated=truncated,
        formulas_total=total,
        functions_omitted=len(omitted),
        counted=tuple(f for routine in functions for f in ranked[routine]),
        resolved_by=resolved_by,
        resolved_chain=resolved_chain,
        resolved_others=resolved_others,
    )


def _base_inputs(
    res: _Resolver,
    functions: Sequence[str],
    formulas: Sequence[Formula],
    untraced: list[tuple[str, str]],
) -> list[BaseInput]:
    used: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for formula in formulas:
        for entry in formula.inputs:
            if entry.table_id and entry.column:
                used.setdefault((entry.table_id, entry.column), []).append(
                    (formula.routine_id, formula.line)
                )
    if routine_store.has_fields(res.conn):
        for routine in functions:
            for table_id, column in res.conn.execute(
                f"SELECT DISTINCT table_id, column_name FROM {routine_store.FIELDS_TABLE} "  # noqa: S608
                "WHERE routine_id = ? AND role IN ('read', 'filter') AND column_name != '*' "
                "ORDER BY table_id, column_name",
                (routine,),
            ):
                used.setdefault((table_id, column), [])
    bases: list[BaseInput] = []
    reported: set[str] = set()
    for (table_id, column), where in sorted(used.items()):
        label, description = res.column_info(table_id, column)
        writers, screens, menus, reason = res.cadastre(table_id)
        if reason is not None and table_id not in reported:
            reported.add(table_id)
            untraced.append((vo.short(table_id), reason))
        bases.append(
            BaseInput(
                table_id=table_id,
                column=column,
                label=label,
                description=description,
                used_in=tuple(dict.fromkeys(where)),
                writers=writers,
                screens=screens,
                menus=menus,
            )
        )
    return bases


# --------------------------------------------------------------------------- saída


def dossier_json(d: CalcDossier) -> dict[str, Any]:
    data = asdict(replace(d, counted=()))
    del data["counted"]
    data["untraced"] = [{"what": what, "reason": reason} for what, reason in d.untraced]
    data["overview"] = topics.overview_json(topics.overview(d))
    return dict(json.loads(json.dumps(data, ensure_ascii=False)))


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _input_row(base: BaseInput) -> str:
    field = f"`{vo.short(base.table_id)}.{base.column}`"
    menus = "; ".join(base.menus) if base.menus else "— (ver não rastreado)"
    return f"| {_cell(field)} | {_cell(base.label or '—')} | {_cell(menus)} |"


def _topic_note(f: Formula) -> str:
    """` — assunto: juros`; sem nada quando o assunto é «outros» (não ocupa palavra à toa)."""
    topic = topics.topic_of_cached(f)
    return "" if topic.key == topics.OTHERS else f" — assunto: {topics.short_label(topic)}"


def _formula_line(f: Formula) -> str:
    return (
        f"- linha {f.line}: {f.target} = {f.readable} — técnica `{f.kind}`: `{f.expression}`"
        f"{_topic_note(f)}"
    )


def _resolved_note(d: CalcDossier) -> str | None:
    """A linha de abertura quando o nome pedido resolveu por repasse entre sobrecargas;
    `None` fora desse caso."""
    if d.resolved_by != "repasse":
        return None
    name = d.root.rpartition(".")[2].partition("(")[0]
    return routine_store.describe_repasse_text(name, d.root, d.resolved_others)


FORMULA_WEIGHT = 0.6  # no corte, uma palavra de fórmula pesa menos: as fórmulas são o assunto


def render_calc_dossier(
    d: CalcDossier, *, max_words: int = DEFAULT_MAX_WORDS, full: bool = False
) -> str:
    """O dossiê em Markdown, em seções fixas. Passou de `max_words`: corta do fim, o que mais
    ocupa primeiro (fórmulas, campos de entrada ou itens de "não rastreado"), e diz quanto ficou de
    fora de cada um. Os campos com menu ficam na frente dos sem menu, que já aparecem em
    "não rastreado". Com `full=True`: nunca corta por `max_words` — quem pagina o
    dossiê inteiro é `report/paging.py`."""
    max_words = max(300, max_words)
    by_function: dict[str, list[Formula]] = {r: [] for r in d.functions}
    for formula in d.formulas:
        by_function.setdefault(formula.routine_id, []).append(formula)
    inputs = sorted(d.inputs, key=lambda base: not base.menus)  # estável: com menu primeiro
    # a visão geral conta todas as fórmulas, tem teto próprio e nunca é cortada
    overview_data = topics.overview(d)

    def build(
        shown: int, shown_inputs: int | None = None, shown_untraced: int | None = None
    ) -> str:
        shown_inputs = len(inputs) if shown_inputs is None else shown_inputs
        shown_untraced = len(d.untraced) if shown_untraced is None else shown_untraced
        left = shown
        overview_text = topics.render_overview(
            overview_data, cut=shown < len(d.formulas) or overview_data.shown < overview_data.total
        ).rstrip()
        resolved_note = _resolved_note(d)
        parts = [
            f"# Dossiê de cálculo: {vo.short(d.root)}",
            "",
            "> Gerado pelo php-map, sem IA: fórmulas lidas do texto das funções e dados de "
            "entrada levados até o menu de cadastro.",
            "",
            *([resolved_note, ""] if resolved_note else []),
            *overview_text.splitlines(),
            "",
            f"## Cálculos de {vo.short(d.root)}",
            "",
        ]
        mine = by_function.get(d.root, [])
        for formula in mine[:left]:
            parts.append(_formula_line(formula))
        left = max(0, left - len(mine))
        if not mine:
            parts.append("Nenhuma fórmula achada nesta função.")
        parts += ["", "## Funções auxiliares", ""]
        auxiliaries = [r for r in d.functions if r != d.root]
        if not auxiliaries:
            parts.append("Nenhuma função auxiliar com conta foi chamada.")
        for routine in auxiliaries:
            parts += [f"### {vo.short(routine)}", ""]
            theirs = by_function.get(routine, [])
            for formula in theirs[:left]:
                parts.append(_formula_line(formula))
            if not theirs:
                parts.append("Nenhuma fórmula achada.")
            left = max(0, left - len(theirs))
            parts.append("")
        parts += [
            "## Dados de entrada e onde são cadastrados",
            "",
            "| Campo | Nome | Menu |",
            "|---|---|---|",
        ]
        for base in inputs[:shown_inputs]:
            parts.append(_input_row(base))
        if not inputs:
            parts.append("| — | — | — |")
        if shown_inputs < len(inputs):
            parts += ["", f"_(cortado: {len(inputs) - shown_inputs} de {len(inputs)} campos)_"]
        parts += ["", "## Não rastreado", ""]
        if d.untraced:
            parts += [f"- `{what}`: {reason}" for what, reason in d.untraced[:shown_untraced]]
        else:
            parts.append("Nada ficou sem rastro.")
        if shown_untraced < len(d.untraced):
            cut = len(d.untraced) - shown_untraced
            parts += ["", f"_(cortado: {cut} de {len(d.untraced)} itens)_"]
        if shown < len(d.formulas):
            parts += ["", f"_(cortado: {len(d.formulas) - shown} de {len(d.formulas)} fórmulas)_"]
        if d.truncated:
            parts += [
                "",
                f"_O dossiê bateu nos limites: {len(d.formulas)} de {d.formulas_total} fórmulas "
                f"e {len(d.functions)} funções; {d.functions_omitted} função(ões) chamada(s) "
                "ficou(aram) de fora._",
            ]
        return "\n".join(parts).rstrip() + "\n"

    # Quanto custa cada item (em palavras): o corte tira 10% do que mais pesa, até caber.
    cost = {
        "formulas": [len(_formula_line(f).split()) * FORMULA_WEIGHT for f in d.formulas],
        "inputs": [float(len(_input_row(b).split())) for b in inputs],
        "untraced": [float(len(f"- `{w}`: {r}".split())) for w, r in d.untraced],
    }
    shown = {"formulas": len(d.formulas), "inputs": len(inputs), "untraced": len(d.untraced)}
    text = build(shown["formulas"], shown["inputs"], shown["untraced"])
    while not full and len(text.split()) > max_words and any(shown.values()):
        biggest = max(shown, key=lambda key: sum(cost[key][: shown[key]]))
        shown[biggest] = max(0, shown[biggest] - max(1, shown[biggest] // 10))
        text = build(shown["formulas"], shown["inputs"], shown["untraced"])
    hits = list(scan_text(text))
    if hits:
        pattern, value = hits[0]
        raise QueryError(
            f"o dossiê contém um segredo aparente (padrão {pattern}, valor {mask_secret(value)});"
            " não foi liberado"
        )
    return text


# --------------------------------------------------------------------------- dossiê de um campo


MAX_FIELD_WRITERS = 10
MAX_PARSED_WRITERS = 40  # funções cujo texto é lido para ordenar "quem grava"
MAX_WRITES_PER_FUNCTION = 3
MAX_FIELD_READERS = 20


@dataclass(frozen=True, slots=True)
class FieldWrite:
    """Uma instrução de uma função que grava o campo, com o que se sabe do valor gravado."""

    routine_id: str
    line: int
    end_line: int
    expression: str | None  # o lado direito (`SET col = expr`, valor do INSERT); None se não lida
    readable: str | None  # a expressão com o nome do dicionário
    formula: Formula | None  # a fórmula do valor; None = cópia direta (ver `copy_of`)
    copy_of: tuple[str, ...]  # `caixa.arrecad.k00_valor` de onde o valor é copiado
    slice: tuple[Formula, ...]  # as contas que dão valor às variáveis usadas na gravação
    inputs: tuple[BaseInput, ...]  # entradas da fórmula e da fatia, até o menu de cadastro
    calls: tuple[str, ...]  # funções chamadas dentro da conta (para "analisar a função")
    confidence: float


@dataclass(frozen=True, slots=True)
class FieldDossier:
    table_id: str
    column: str
    label: str | None
    description: str | None
    registered_in: BaseInput  # onde o próprio campo é cadastrado (writers, telas, menus)
    computed_by: tuple[FieldWrite, ...]
    writers_total: int  # funções que gravam o campo
    more_writers: int  # as que ficaram de fora do corte
    read_by: tuple[tuple[str, str], ...]  # (função, papéis: read, filter, join, sort)
    read_by_total: int
    star_readers: tuple[str, ...]  # `select *` da tabela: pode usar o campo, sem dizer o nome
    star_readers_total: int
    untraced: tuple[tuple[str, str], ...]
    access: TableAccess | None = None  # quem grava e quem lê no código PHP
    menus_from_layers: bool = False  # o menu veio da subida das camadas, não do cadastro
    # quem preenche ESTE campo antes de gravar pelo DAO (atribuição ou formulário)
    fills: tuple[tuple[Toucher, tuple[FieldFill, ...]], ...] = ()


def _table_id_of(res: _Resolver, text: str) -> str:
    name = text.strip()
    if name.startswith("Table:"):
        name = name[len("Table:") :]
    if "." in name:
        table_id = f"Table:{name.lower()}"
        if not res.table_exists(table_id):
            raise QueryError(f"tabela não encontrada: {name}")
        return table_id
    found = res.tables_by_name().get(name.lower(), [])
    if len(found) == 1:
        return found[0]
    if not found:
        raise QueryError(f"tabela não encontrada: {name}")
    shown = ", ".join(vo.short(t) for t in found[:10])
    raise QueryError(f"mais de uma tabela chamada {name!r}; use schema.tabela: {shown}")


def resolve_field(
    conn: sqlite3.Connection, table: str, column: str | None = None
) -> tuple[str, str]:
    """`(Table:schema.tabela, coluna)`. Aceita `schema.tabela` + coluna, `schema.tabela.coluna`,
    `tabela.coluna` (se a tabela for única) e `Table:schema.tabela#coluna`. `QueryError` clara
    para tabela ou coluna que não existem."""
    text = table.strip()
    name = column.strip().lower() if column else ""
    if not name:
        if "#" in text:
            text, _sep, name = text.partition("#")
        else:
            text, _sep, name = text.rpartition(".")
        name = name.strip().lower()
        if not text or not name:
            raise QueryError("informe a tabela e o campo: schema.tabela.coluna")
    res = _Resolver(conn)
    table_id = _table_id_of(res, text)
    known = res.columns(table_id)
    if known is not None and name not in known:
        close = sorted(c for c in known if name[:3] and c.startswith(name[:3]))[:5]
        hint = f" Parecidas: {', '.join(close)}." if close else ""
        raise QueryError(f"coluna inexistente em {vo.short(table_id)}: {name}.{hint}")
    return table_id, name


def _field_writes(
    res: _Resolver, table_id: str, column: str, routine_id: str, rows: list[tuple[int, int, float]]
) -> tuple[list[FieldWrite], list[tuple[str, str]]]:
    """As gravações de `routine_id` no campo: `rows` = `(query_seq, linha, confiança)`."""
    model = res.model(routine_id)
    notes: list[tuple[str, str]] = []
    if model is None:
        notes.append((vo.short(routine_id), "a função que grava o campo não tem texto no mapa"))
        return [], notes
    wanted = f"{table_id.rpartition('.')[2]}.{column}"
    formula_keys = {(f.line, f.target, f.kind) for f in model.formulas()}
    writes: list[FieldWrite] = []
    for seq, line, confidence in rows[:MAX_WRITES_PER_FUNCTION]:
        made = model.column_write(seq, wanted)
        if made is None:
            writes.append(
                FieldWrite(routine_id, line, line, None, None, None, (), (), (), (), confidence)
            )
            notes.append(
                (
                    f"{vo.short(routine_id)}:{line}",
                    "grava o campo, mas o valor não pôde ser lido da instrução",
                )
            )
            continue
        is_formula = (made.line, made.target, made.kind) in formula_keys
        sliced: list[Formula] = []
        for entry in made.inputs:
            if entry.via_line is None:
                continue
            for formula in model.formulas():
                if (
                    formula.target.lower() == entry.name.lower()
                    and formula.line == entry.via_line
                    and formula not in sliced
                ):
                    sliced.append(formula)
        for entry in made.inputs:
            if entry.expression and entry.via_line is not None:
                sliced.append(
                    Formula(
                        routine_id,
                        entry.name,
                        entry.expression,
                        entry.via_line,
                        entry.via_line,
                        "loop_column",
                        (),
                        entry.expression,
                        (),
                    )  # fmt: skip
                )
        local: list[tuple[str, str]] = []
        inputs = _base_inputs(res, (), [made, *sliced], local)
        notes.extend(note for note in local if note not in notes)
        for note in model.notes_by_formula.get((made.line, made.target, made.kind), ()):
            if note not in notes:
                notes.append(note)
        copy_of = _unique(
            f"{vo.short(i.table_id)}.{i.column}" for i in made.inputs if i.table_id and i.column
        )
        calls = _unique(
            i.routine_id
            for f in (made, *sliced)
            for i in f.inputs
            if i.kind == "function" and i.routine_id
        )
        writes.append(
            FieldWrite(
                routine_id=routine_id,
                line=made.line,
                end_line=made.end_line,
                expression=made.expression,
                readable=made.readable,
                formula=made if is_formula else None,
                copy_of=copy_of,
                slice=tuple(sliced),
                inputs=tuple(inputs),
                calls=calls,
                confidence=confidence,
            )
        )
    return writes, notes


def field_dossier(
    conn: sqlite3.Connection,
    table: str,
    column: str | None = None,
    *,
    max_writers: int = MAX_FIELD_WRITERS,
    max_readers: int = access_module.DEFAULT_MAX_READERS,
    max_menu_climbs: int = access_module.DEFAULT_MAX_MENU_CLIMBS,
) -> FieldDossier:
    """Onde o campo é cadastrado, como o valor é calculado (as funções que o gravam,
    com a fórmula e as entradas até o menu) e quem o usa. `access` traz todo o código
    PHP que grava e lê a tabela (SQL direto, DAO, Eloquent, migração). Determinístico, sem IA."""
    table_id, name = resolve_field(conn, table, column)
    if not routine_store.has_fields(conn):
        raise QueryError(
            "este mapa não tem as consultas das funções da base: rode o scan com o texto delas"
        )
    res = _Resolver(conn)
    label, description = res.column_info(table_id, name)
    untraced: list[tuple[str, str]] = []
    writers, screens, menus, reason = res.cadastre(table_id)
    if reason is not None:
        untraced.append((vo.short(table_id), reason))
    access = access_module.table_access(
        conn, table_id, max_readers=max_readers, max_menu_climbs=max_menu_climbs
    )
    from_layers = not menus and bool(_layer_menus(access))
    if from_layers:
        menus = _layer_menus(access)
        untraced = [u for u in untraced if u != (vo.short(table_id), reason)]
    registered = BaseInput(table_id, name, label, description, (), writers, screens, menus)

    table = routine_store.FIELDS_TABLE
    grouped: dict[str, list[tuple[int, int, float]]] = {}
    for routine, seq, line, confidence in conn.execute(
        f"SELECT routine_id, query_seq, line, confidence FROM {table} "  # noqa: S608
        "WHERE table_id = ? AND column_name = ? AND role = 'write' ORDER BY routine_id, line",
        (table_id, name),
    ):
        grouped.setdefault(routine, []).append((seq, line, float(confidence)))
    writers_total = len(grouped)
    candidates = sorted(grouped, key=lambda r: (-max(c for _s, _l, c in grouped[r]), vo.short(r)))[
        :MAX_PARSED_WRITERS
    ]
    if writers_total > len(candidates):
        untraced.append(
            (
                "ordem das funções",
                f"só as {len(candidates)} de {writers_total} funções foram lidas para ordenar",
            )
        )
    parsed: list[tuple[str, list[FieldWrite]]] = []
    for routine in candidates:
        found, notes = _field_writes(res, table_id, name, routine, grouped[routine])
        untraced.extend(note for note in notes if note not in untraced)
        parsed.append((routine, found))
    parsed.sort(
        key=lambda item: (
            not any(w.formula is not None or w.slice for w in item[1]),
            -max((w.confidence for w in item[1]), default=0.0),
            vo.short(item[0]),
        )
    )
    chosen = parsed[: max(1, max_writers)]
    computed = tuple(w for _routine, found in chosen for w in found)

    readers: dict[str, set[str]] = {}
    for routine, role in conn.execute(
        f"SELECT DISTINCT routine_id, role FROM {table} "  # noqa: S608
        "WHERE table_id = ? AND column_name = ? AND role != 'write'",
        (table_id, name),
    ):
        readers.setdefault(routine, set()).add(role)
    order = ("read", "filter", "join", "sort")
    read_by = tuple(
        (r, ", ".join(sorted(roles, key=lambda x: order.index(x) if x in order else 9)))
        for r, roles in sorted(readers.items(), key=lambda item: vo.short(item[0]))
    )
    stars = [
        routine
        for (routine,) in conn.execute(
            f"SELECT DISTINCT routine_id FROM {table} "  # noqa: S608
            "WHERE table_id = ? AND column_name = '*' AND role = 'read' ORDER BY routine_id",
            (table_id,),
        )
    ]
    return FieldDossier(
        table_id=table_id,
        column=name,
        label=label,
        description=description,
        registered_in=registered,
        computed_by=computed,
        writers_total=writers_total,
        more_writers=max(0, writers_total - len(chosen)),
        read_by=read_by[:MAX_FIELD_READERS],
        read_by_total=len(read_by),
        star_readers=tuple(stars[:MAX_FIELD_READERS]),
        star_readers_total=len(stars),
        untraced=tuple(untraced),
        access=access,
        menus_from_layers=from_layers,
        fills=access_module.field_fills(access, name),
    )


def field_dossier_json(d: FieldDossier) -> dict[str, Any]:
    data = asdict(d)
    data["fills"] = [
        {"toucher": asdict(toucher), "fills": [asdict(fill) for fill in items]}
        for toucher, items in d.fills
    ]
    data["untraced"] = [{"what": what, "reason": reason} for what, reason in d.untraced]
    data["read_by"] = [{"routine_id": r, "roles": roles} for r, roles in d.read_by]
    return dict(json.loads(json.dumps(data, ensure_ascii=False)))


def _field_name(d: FieldDossier) -> str:
    return f"{vo.short(d.table_id)}.{d.column}"


_FROM_LAYERS = " (pelas camadas, a partir de quem grava a tabela)"


def _whole_table_field_note(access: TableAccess | None) -> str:
    """Fecha a frase «a tela grava a tabela inteira»: com `php_field_writes` no mapa, o
    campo a campo está na seção «Quem preenche este campo»."""
    if access is not None and access.fills_total:
        return ": quais campos ela preenche está em «Quem preenche este campo no código PHP»"
    return ": o mapa não diz coluna por coluna"


def _whole_table_table_note(access: TableAccess) -> str:
    if access.fills_total:
        return ": quais campos ela preenche está na coluna «Preenche» de «Quem grava»"
    return ": o mapa não diz coluna por coluna"


def _layer_menus(access: TableAccess) -> tuple[str, ...]:
    """Menus de quem grava a tabela (achados pela subida das camadas), telas primeiro: serve de
    «onde é cadastrada» quando o `climb_to_screen` (teto de 3 gravadores × 2 chamadores, só
    `File:`) não achou menu. Model Eloquent não entra: o mapa não diz se grava."""
    found: dict[str, None] = {}
    writers = [w for w in access.writers if w.how != access_module.HOW_ELOQUENT]
    for toucher in sorted(writers, key=lambda w: w.layer != "tela"):
        for menu in toucher.menus:
            found.setdefault(menu, None)
    return tuple(found)[:3]


def _more_writers_line(access: TableAccess | None, shown: int) -> list[str]:
    """«Onde é cadastrado» mostra as telas que o `climb_to_screen` achou (teto de 3 × 2); quando a
    tabela tem mais fontes gravando, aponta para a seção completa."""
    if access is None or access.writers_total <= max(shown, 0):
        return []
    return [
        f"- há {access.writers_total} fonte(s) PHP que gravam esta tabela (acima, só a tela "
        'principal do cadastro): ver "Quem grava e quem lê no código PHP"'
    ]


_BARE_REFERENCE = re.compile(r"^\s*[A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)?\s*(?:::\s*\w+)?\s*$")


def _write_block(w: FieldWrite) -> list[str]:
    lines = [f"### {vo.short(w.routine_id)} (linha {w.line})", ""]
    if w.formula is not None:
        lines.append(
            f"- cálculo: {w.formula.target} = {w.formula.readable} — "
            f"técnica `{w.formula.kind}`: `{w.formula.expression}`{_topic_note(w.formula)}"
        )
    elif w.expression is None:
        lines.append("- o valor gravado não pôde ser lido da instrução")
    else:
        sources = ", ".join(f"`{c}`" for c in w.copy_of)
        if not w.copy_of:
            origin = "valor sem conta (variável, parâmetro ou literal)"
        elif _BARE_REFERENCE.match(w.expression):
            origin = f"cópia direta de {sources}"
        else:
            origin = f"valor escolhido sem conta, a partir de {sources}"
        lines.append(f"- {origin}: `{w.expression}`")
    for formula in w.slice:
        lines.append(
            f"- vem de: linha {formula.line}: {formula.target} = {formula.readable} — "
            f"`{formula.expression}`"
        )
    if w.calls:
        lines.append("- chama: " + ", ".join(f"`{vo.short(c)}`" for c in w.calls))
    if w.inputs:
        lines += ["", "| Entrada | Nome | Menu |", "|---|---|---|"]
        lines += [_input_row(base) for base in w.inputs]
    lines.append("")
    return lines


def render_field_dossier(
    d: FieldDossier, *, max_words: int = DEFAULT_MAX_WORDS, full: bool = False
) -> str:
    """O dossiê do campo em Markdown, em seções fixas. Passou de `max_words`: tira gravações do
    fim (as de menos peso na ordem) e diz quantas ficaram de fora. Com `full=True`:
    nunca corta por `max_words` nem pelo teto de linhas de `table_access.render_table_access`/
    `render_field_fills` — quem pagina o dossiê inteiro é `report/paging.py`."""
    max_words = max(300, max_words)

    def build(shown: int) -> str:
        title = _field_name(d) + (f" ({d.label})" if d.label else "")
        parts = [
            f"# Dossiê do campo: {title}",
            "",
            "> Gerado pelo php-map, sem IA: funções que gravam o campo, fórmula do valor, "
            "entradas até o menu de cadastro e quem usa o campo.",
            "",
            "## Onde é cadastrado",
            "",
        ]
        reg = d.registered_in
        if reg.menus:
            parts += [
                f"- menu{_FROM_LAYERS if d.menus_from_layers else ''}: {menu}" for menu in reg.menus
            ]
        else:
            parts.append("- sem menu de cadastro rastreado (ver não rastreado)")
        if reg.screens:
            parts.append("- tela: " + ", ".join(f"`{vo.short(s)}`" for s in reg.screens))
        classes = [w for w in reg.writers if w.startswith("Method:")]
        if classes and reg.screens:
            parts.append(
                "- a tela grava a **tabela inteira** (todos os campos) pela classe "
                + ", ".join(f"`{vo.short(w)}`" for w in classes)
                + _whole_table_field_note(d.access)
            )
        parts += _more_writers_line(d.access, len(reg.screens))
        parts += ["", "## Como é calculado", ""]
        written = d.computed_by[:shown]
        if not d.computed_by:
            parts += ["Nenhuma função da base grava este campo.", ""]
        for write in written:
            parts += _write_block(write)
        left_out = d.more_writers + (len(d.computed_by) - len(written))
        if left_out > 0:
            parts += [
                f"_e mais {left_out} gravação(ões) em outras funções "
                f"(total: {d.writers_total} função(ões))_",
                "",
            ]
        parts += ["## Quem usa", ""]
        if d.read_by:
            parts += [f"- `{vo.short(r)}`: {roles}" for r, roles in d.read_by]
            if d.read_by_total > len(d.read_by):
                parts.append(f"- _e mais {d.read_by_total - len(d.read_by)} função(ões)_")
        else:
            parts.append("Nenhuma função da base usa este campo pelo nome.")
        if d.star_readers:
            parts += [
                "",
                "Lê a tabela inteira (`select *`; pode usar o campo, o mapa não diz):",
                "",
            ]
            parts += [f"- `{vo.short(r)}`" for r in d.star_readers]
            if d.star_readers_total > len(d.star_readers):
                parts.append(f"- _e mais {d.star_readers_total - len(d.star_readers)} função(ões)_")
        if d.access is not None:
            # A seção do campo divide com a do dossiê da tabela o mesmo terço das palavras.
            fills_text = access_module.render_field_fills(
                d.access, d.column, max_rows=None if full else access_module.MAX_FILL_ROWS
            ).rstrip()
            budget = None if full else max(300, max_words // 3 - len(fills_text.split()))
            parts += [
                "",
                access_module.render_table_access(d.access, max_words=budget, full=full).rstrip(),
                "",
                fills_text,
            ]
        parts += ["", "## Não rastreado", ""]
        if d.untraced:
            parts += [f"- `{what}`: {reason}" for what, reason in d.untraced]
        else:
            parts.append("Nada ficou sem rastro.")
        return "\n".join(parts).rstrip() + "\n"

    shown = len(d.computed_by)
    text = build(shown)
    while not full and len(text.split()) > max_words and shown > 1:
        shown -= 1
        text = build(shown)
    hits = list(scan_text(text))
    if hits:
        pattern, value = hits[0]
        raise QueryError(
            f"o dossiê contém um segredo aparente (padrão {pattern}, valor {mask_secret(value)});"
            " não foi liberado"
        )
    return text


# --------------------------------------------------------------------------- dossiê de uma tabela


MAX_TABLE_COLUMNS = 80


@dataclass(frozen=True, slots=True)
class TableDossier:
    """A tabela como ponto de partida, sem escolher o campo."""

    table_id: str
    label: str | None  # nome da tabela no dicionário do e-cidade
    description: str | None
    columns: tuple[tuple[str, str | None], ...]  # (coluna, nome do dicionário)
    registered_in: BaseInput  # onde a tabela é cadastrada (coluna vazia)
    access: TableAccess
    pg_writers_total: int  # funções da base (PL/pgSQL) que gravam a tabela
    pg_readers_total: int  # funções da base que leem a tabela
    untraced: tuple[tuple[str, str], ...]
    pg_writers: tuple[str, ...] = ()  # as primeiras que gravam (para as perguntas sugeridas)
    menus_from_layers: bool = False


def resolve_table(conn: sqlite3.Connection, table: str) -> str:
    """`Table:schema.tabela` a partir de `schema.tabela`, `tabela` (se única) ou o id.
    `QueryError` clara para tabela inexistente ou ambígua."""
    return _table_id_of(_Resolver(conn), table)


def table_dossier(
    conn: sqlite3.Connection,
    table: str,
    *,
    max_readers: int = access_module.DEFAULT_MAX_READERS,
    max_menu_climbs: int = access_module.DEFAULT_MAX_MENU_CLIMBS,
) -> TableDossier:
    """O que o mapa sabe de uma tabela — campos, onde é cadastrada, quem grava e quem
    lê no código PHP e quantas funções da base mexem nela. Determinístico, sem IA."""
    res = _Resolver(conn)
    table_id = _table_id_of(res, table)
    attrs = res.attrs(table_id)
    catalog = attrs.get("catalog") or {}
    label = catalog.get("label") if isinstance(catalog, dict) else None
    description = catalog.get("description") if isinstance(catalog, dict) else None
    raw_columns = attrs.get("columns")
    columns = tuple(
        (str(c).lower(), res.column_info(table_id, str(c).lower())[0])
        for c in (raw_columns if isinstance(raw_columns, list) else ())
    )
    untraced: list[tuple[str, str]] = []
    writers, screens, menus, reason = res.cadastre(table_id)
    if reason is not None:
        untraced.append((vo.short(table_id), reason))
    access = access_module.table_access(
        conn, table_id, max_readers=max_readers, max_menu_climbs=max_menu_climbs
    )
    from_layers = not menus and bool(_layer_menus(access))
    if from_layers:
        menus = _layer_menus(access)
        untraced = [u for u in untraced if u != (vo.short(table_id), reason)]
    registered = BaseInput(table_id, "", None, None, (), writers, screens, menus)
    counts: dict[str, int] = {}
    for kind, total in conn.execute(
        "SELECT CASE WHEN type = 'QUERIES' THEN 'read' ELSE 'write' END, COUNT(DISTINCT src)"
        " FROM edges WHERE dst = ? AND src LIKE 'PgFunction:%'"
        " AND type IN ('INSERTS', 'UPDATES', 'DELETES', 'QUERIES') GROUP BY 1",
        (table_id,),
    ):
        counts[kind] = int(total)
    pg_writers = tuple(
        routine
        for (routine,) in conn.execute(
            "SELECT DISTINCT src FROM edges WHERE dst = ? AND src LIKE 'PgFunction:%'"
            " AND type IN ('INSERTS', 'UPDATES', 'DELETES') ORDER BY src LIMIT 5",
            (table_id,),
        )
    )
    return TableDossier(
        table_id=table_id,
        label=str(label).strip() if isinstance(label, str) and label.strip() else None,
        description=str(description).strip()
        if isinstance(description, str) and description.strip()
        else None,
        columns=columns,
        registered_in=registered,
        access=access,
        pg_writers_total=counts.get("write", 0),
        pg_readers_total=counts.get("read", 0),
        pg_writers=pg_writers,
        untraced=tuple(untraced),
        menus_from_layers=from_layers,
    )


def table_dossier_json(d: TableDossier) -> dict[str, Any]:
    data = asdict(d)
    data["untraced"] = [{"what": what, "reason": reason} for what, reason in d.untraced]
    data["columns"] = [{"name": name, "label": label} for name, label in d.columns]
    return dict(json.loads(json.dumps(data, ensure_ascii=False)))


def render_table_dossier(
    d: TableDossier, *, max_words: int = DEFAULT_MAX_WORDS, full: bool = False
) -> str:
    """O dossiê da tabela em Markdown, em seções fixas. A seção do código PHP leva até 4/5 das
    palavras; o resto são campos, cadastro e o que mexe na tabela dentro da base. Com `full=True`:
    nunca corta por `max_words` nem pelo teto de linhas de gravadores — quem pagina o
    dossiê inteiro é `report/paging.py`."""
    max_words = max(300, max_words)
    name = vo.short(d.table_id)
    title = name + (f" ({d.label})" if d.label else "")
    parts = [
        f"# Dossiê da tabela: {title}",
        "",
        "> Gerado pelo php-map, sem IA: campos, onde a tabela é cadastrada e todo o código PHP "
        "que a grava e a lê (SQL direto, DAO, model Eloquent e migrações).",
        "",
    ]
    if d.description and d.description != d.label:
        parts += [d.description, ""]
    parts += ["## Campos", ""]
    if d.columns:
        parts += ["| Campo | Nome |", "|---|---|"]
        parts += [
            f"| `{c}` | {_cell(label or '—')} |" for c, label in d.columns[:MAX_TABLE_COLUMNS]
        ]
        if len(d.columns) > MAX_TABLE_COLUMNS:
            parts.append(f"_e mais {len(d.columns) - MAX_TABLE_COLUMNS} campo(s)_")
    else:
        parts.append("O mapa não tem a lista de campos desta tabela.")
    parts += ["", "## Onde é cadastrado", ""]
    reg = d.registered_in
    if reg.menus:
        parts += [
            f"- menu{_FROM_LAYERS if d.menus_from_layers else ''}: {menu}" for menu in reg.menus
        ]
    else:
        parts.append("- sem menu de cadastro rastreado (ver não rastreado)")
    if reg.screens:
        parts.append("- tela: " + ", ".join(f"`{vo.short(s)}`" for s in reg.screens))
    classes = [w for w in reg.writers if w.startswith("Method:")]
    if classes and reg.screens:
        parts.append(
            "- a tela grava a **tabela inteira** (todos os campos) pela classe "
            + ", ".join(f"`{vo.short(w)}`" for w in classes)
            + _whole_table_table_note(d.access)
        )
    parts += _more_writers_line(d.access, len(reg.screens))
    parts += [
        "",
        access_module.render_table_access(
            d.access,
            max_words=None if full else max(300, max_words * 4 // 5),
            full=full,
        ).rstrip(),
        "",
        "## Na base de dados",
        "",
        f"- {d.pg_writers_total} função(ões) da base (PL/pgSQL) gravam esta tabela e "
        f"{d.pg_readers_total} leem.",
        "- Para ver como cada campo é calculado, peça o dossiê do campo (`schema.tabela.coluna`).",
        "",
        "## Não rastreado",
        "",
    ]
    if d.untraced:
        parts += [f"- `{what}`: {reason}" for what, reason in d.untraced]
    else:
        parts.append("Nada ficou sem rastro.")
    text = (chr(10).join(parts)).rstrip() + chr(10)
    hits = list(scan_text(text))
    if hits:
        pattern, value = hits[0]
        raise QueryError(
            f"o dossiê contém um segredo aparente (padrão {pattern}, valor {mask_secret(value)});"
            " não foi liberado"
        )
    return text


__all__ = [
    "AmbiguousRoutineError",
    "BaseInput",
    "CalcDossier",
    "FieldDossier",
    "FieldWrite",
    "Formula",
    "FormulaInput",
    "TableDossier",
    "calc_dossier",
    "dossier_json",
    "extract_formulas",
    "field_dossier",
    "field_dossier_json",
    "render_calc_dossier",
    "render_field_dossier",
    "render_table_dossier",
    "resolve_field",
    "resolve_routine",
    "resolve_table",
    "table_dossier",
    "table_dossier_json",
]
