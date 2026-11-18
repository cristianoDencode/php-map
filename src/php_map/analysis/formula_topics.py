"""Assunto das fórmulas, visão geral e perguntas sugeridas: sem IA.

Nenhum assunto é dado pelo e-cidade. Ele sai, por regras fixas e nesta ordem, (1) da **função
chamada** (`fc_juros`), (2) do **rótulo do dicionário** da coluna gravada e (3) de um **pedaço do
nome do destino** (`vlrjuros`); senão é «outros». É uma **pista**: cada `Topic` diz de onde veio
(`by` + `evidence`) e a visão geral avisa isso. A IA só redige o «para quê» a partir dela.

Este módulo não importa `formulas.py` em tempo de execução (é `formulas.py` quem o usa); os tipos
dos dossiês entram só para a checagem de tipos.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from php_map.analysis.field_search import FieldSearch
    from php_map.analysis.formulas import CalcDossier, FieldDossier, Formula, TableDossier
    from php_map.analysis.menu_dossier import MenuDossier

OVERVIEW_MAX_WORDS = 150
MAX_SUGGESTIONS = 8
MAX_TOPIC_QUESTIONS = 3
MAX_AUX_QUESTIONS = 2
MAX_BASE_QUESTIONS = 2
MAX_TERM_QUESTIONS = 4
MAX_MENU_QUESTIONS = 4
MAX_TABLE_SUGGESTIONS = 6
MAX_TABLE_FUNCTION_QUESTIONS = 2
MAX_LINES = 6  # linhas guardadas por assunto no `Overview`
SHOWN_LINES = 3  # linhas mostradas no Markdown (cai para 0 se a seção passar do teto)
SHOWN_FUNCTIONS = 3  # funções mostradas por assunto no Markdown

# --------------------------------------------------------------------------- tabelas fixas

OTHERS = "outros"

# key → rótulo em português (a ordem é a de apresentação dos empates)
LABELS: dict[str, str] = {
    "correcao": "correção monetária",
    "juros": "juros",
    "multa": "multa",
    "desconto": "desconto",
    "inflator": "inflator (conversão por índice)",
    "historico": "valor histórico",
    "estorno": "estorno",
    "total": "total",
    "percentual": "percentual",
    OTHERS: "outros",
}

# função chamada → assunto
CALL_TOPICS: dict[str, str] = {
    "fc_juros": "juros",
    "fc_multa": "multa",
    "fc_corre": "correcao",
    "fc_desconto": "desconto",
    "fc_vlinf": "inflator",
}

# pedaço do nome (sem acento, minúsculo) → assunto. A ORDEM É A PRIORIDADE: o assunto de negócio
# vem antes de `percentual` e `total` (npercmulta é multa, não percentual).
NAME_TOPICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("juros", ("juro",)),
    ("multa", ("mult",)),
    ("correcao", ("corre", "correc")),
    ("desconto", ("desc",)),
    ("inflator", ("infla",)),
    ("historico", ("hist",)),
    ("estorno", ("estorn",)),
    ("percentual", ("perc",)),
    ("total", ("tot",)),
)
# `desc` não casa `descr` (descrição)
_NAME_EXCLUDE: dict[str, tuple[str, ...]] = {"desconto": ("descr",)}
_WEAK = frozenset(("total", "percentual"))  # assuntos que cedem a um de negócio
_SQL_WORDS = frozenset(("desc", "asc", "case", "when", "then", "else", "end", "null", "and", "or"))

# «Como são calculados os juros» por assunto
QUESTIONS: dict[str, str] = {
    "correcao": "Como é calculada a correção monetária",
    "juros": "Como são calculados os juros",
    "multa": "Como é calculada a multa",
    "desconto": "Como é calculado o desconto",
    "inflator": "Como é calculado o inflator",
    "historico": "Como é calculado o valor histórico",
    "estorno": "Como é calculado o estorno",
    "total": "Como é calculado o total",
    "percentual": "Como é calculado o percentual",
}

SHAPE_LABELS: dict[str, str] = {
    "acumula": "acumula",
    "rateio": "rateio por percentual",
    "ajuste": "ajuste de centavos",
}

_CALL_RE = re.compile(
    r"(?<![\w$])(" + "|".join(re.escape(name) for name in CALL_TOPICS) + r")\s*\(", re.IGNORECASE
)
_IDENT_RE = re.compile(r"(?<![\w:$.])([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)(\s*\()?")
_ADJUST_TARGET = re.compile(r"^n?diferenca")
_ADJUST_ADDEND = re.compile(r"(?<![\w$])n?diferenca\w*", re.IGNORECASE)
_ROUND_DIFF = re.compile(r"round\s*\(\s*([\w.]+)\s*-\s*([\w.]+)", re.IGNORECASE)
_RATEIO = re.compile(r"\*\s*\(?\s*[\w.]*perc[\w.]*\s*\)?\s*/\s*100", re.IGNORECASE)


# --------------------------------------------------------------------------- tipos


@dataclass(frozen=True, slots=True)
class Topic:
    key: str  # correcao | juros | multa | desconto | inflator | historico | estorno | total | ...
    label: str  # em português
    by: str  # chamada | dicionario | nome | nenhum
    evidence: str  # «chama fc_juros», «destino 'vlrjuros'», «rótulo 'Valor Juros'»
    shape: tuple[str, ...] = ()  # acumula | rateio | ajuste


@dataclass(frozen=True, slots=True)
class TopicRow:
    key: str
    label: str
    count: int
    functions: tuple[tuple[str, int], ...]  # (routine_id, fórmulas do assunto nela), raiz primeiro
    lines: tuple[int, ...]  # até MAX_LINES linhas do assunto **na função raiz**


@dataclass(frozen=True, slots=True)
class FunctionRow:
    routine_id: str
    count: int
    top_topics: tuple[tuple[str, str, int], ...]  # (key, label, contagem), até 3


@dataclass(frozen=True, slots=True)
class Overview:
    root: str
    total: int  # todas as fórmulas do dossiê, inclusive as cortadas
    root_total: int
    shown: int  # as que a lista do dossiê traz (pode ser menos que `total`)
    by_topic: tuple[TopicRow, ...]  # em ordem de contagem; «outros» por último
    by_function: tuple[FunctionRow, ...]  # a raiz primeiro, depois as auxiliares por contagem
    others_total: int


@dataclass(frozen=True, slots=True)
class Suggestion:
    text: str  # a pergunta, em português
    anchor: str  # id do nó (PgFunction, Table, BusinessAction) ou `Termo:<texto>`
    column: str | None  # quando a âncora é uma tabela
    kind: str  # assunto | auxiliar | cadastro | quem_usa | nao_rastreado | campo | funcao


# --------------------------------------------------------------------------- texto


def normalize(text: str) -> str:
    """Minúsculo e sem acento."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def _squash_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normalize(text)).strip()


def _match_name(text: str) -> tuple[str, str] | None:
    """`(assunto, pedaço)` do primeiro assunto da tabela que o texto contém; `None` se nenhum."""
    low = normalize(text)
    for key, pieces in NAME_TOPICS:
        for piece in pieces:
            at = low.find(piece)
            while at >= 0:
                tail = low[at:]
                if not any(tail.startswith(bad) for bad in _NAME_EXCLUDE.get(key, ())):
                    return key, piece
                at = low.find(piece, at + 1)
    return None


def short_name(routine_id: str) -> str:
    """`fc_calcula` em vez de `PgFunction:public.fc_calcula(int4,...)`."""
    bare = routine_id.partition(":")[2] or routine_id
    return bare.removeprefix("public.").partition("(")[0]


def _column_of(target: str) -> str:
    return target.rpartition(".")[2].strip()


def _identifiers(expression: str) -> list[str]:
    """Os nomes usados na conta, na ordem do texto, sem chamadas de função nem cast."""
    found: list[str] = []
    for match in _IDENT_RE.finditer(expression):
        if match.group(2):
            continue
        name = re.sub(r"\s+", "", match.group(1)).rpartition(".")[2]
        low = name.lower()
        if low not in found:
            found.append(low)
    return found


def _first_business(expression: str, skip: str) -> tuple[str, str, str] | None:
    """O primeiro nome da conta (fora o próprio destino) que casa um assunto **de negócio**:
    `(nome, assunto, pedaço)`."""
    for name in _identifiers(expression):
        if name == skip or name in _SQL_WORDS:
            continue
        hit = _match_name(name)
        if hit is not None and hit[0] not in _WEAK:
            return name, hit[0], hit[1]
    return None


def shape_of(target: str, expression: str) -> tuple[str, ...]:
    """A forma da conta: `ajuste` (centavos), `rateio` (por percentual) ou `acumula`."""
    column = normalize(_column_of(target))
    expr = normalize(expression)
    shapes: list[str] = []
    adjust = bool(_ADJUST_TARGET.match(column)) or bool(_ADJUST_ADDEND.search(expr))
    if not adjust:
        diff = _ROUND_DIFF.search(expr)
        if diff and "acum" in diff.group(2).rpartition(".")[2]:
            adjust = True
    if adjust:
        shapes.append("ajuste")
    elif column and re.match(
        r"^\(?\s*(?:round\s*\(\s*)?\(?\s*" + re.escape(column) + r"\s*\+", expr
    ):
        shapes.append("acumula")
    if _RATEIO.search(expr):
        shapes.append("rateio")
    return tuple(shapes)


def topic_of(formula: "Formula", *, column_label: str | None = None) -> Topic:
    """O assunto de uma fórmula. Regras fixas, nesta ordem: função chamada; rótulo do dicionário
    da coluna gravada; pedaço do nome do destino; senão «outros»."""
    target = formula.target
    expression = formula.expression
    shape = shape_of(target, expression)
    calls = [m.group(1).lower() for m in _CALL_RE.finditer(expression)]
    if calls:
        first = calls[0]
        extra = [c for c in dict.fromkeys(calls[1:]) if CALL_TOPICS[c] != CALL_TOPICS[first]]
        evidence = f"chama {first}" + (f" (também {', '.join(extra)})" if extra else "")
        key = CALL_TOPICS[first]
        return Topic(key, LABELS[key], "chamada", evidence, shape)
    if column_label:
        hit = _match_name(column_label)
        if hit is not None and hit[0] not in _WEAK:
            return Topic(hit[0], LABELS[hit[0]], "dicionario", f"rótulo '{column_label}'", shape)
    column = _column_of(target).lower()
    named = _match_name(column) if target != "RETURN" else None
    if named is not None and named[0] not in _WEAK:
        return Topic(named[0], LABELS[named[0]], "nome", f"destino '{column}'", shape)
    # `total` cede ao assunto de negócio da conta (`vlrtotinf = vlrtotinf + vlrinflator`), e o
    # ajuste de centavos (`ndiferencacor = round(vlrcorrecao - nacumcorrecao, 2)`) tem o assunto
    # do que ele ajusta
    is_total = named is not None and named[0] == "total"
    if is_total or (named is None and "ajuste" in shape):
        inherited = _first_business(expression, column)
        if inherited is not None:
            name, key, _piece = inherited
            how = "total de" if is_total else "ajuste de"
            return Topic(key, LABELS[key], "nome", f"destino '{column}' ({how} '{name}')", shape)
    if column_label:
        hit = _match_name(column_label)
        if hit is not None:
            return Topic(hit[0], LABELS[hit[0]], "dicionario", f"rótulo '{column_label}'", shape)
    if named is not None:
        return Topic(named[0], LABELS[named[0]], "nome", f"destino '{column}'", shape)
    return Topic(OTHERS, LABELS[OTHERS], "nenhum", "", shape)


def topic_of_cached(formula: "Formula") -> Topic:
    """O assunto já guardado na fórmula (com o rótulo do dicionário) ou, sem ele, o calculado."""
    stored = getattr(formula, "topic", None)
    return stored if isinstance(stored, Topic) else topic_of(formula)


def short_label(topic: Topic) -> str:
    """O rótulo sem o parêntese: «inflator» em vez de «inflator (conversão por índice)»."""
    return topic.label.partition(" (")[0]


def resolve_topic_key(text: str) -> str | None:
    """A chave do assunto (`LABELS`) a partir da própria chave ou do rótulo em português
    (`formula_search`/`routine_rules(topic=…)`): sem acento, sem diferenciar caixa,
    e tolera o rótulo sem o parêntese (`short_label`). `None` se não casar com nenhum assunto."""
    norm = normalize(text.strip())
    if not norm:
        return None
    if norm in LABELS:
        return norm
    for key, label in LABELS.items():
        if norm in (normalize(label), normalize(short_label(Topic(key, label, "", "")))):
            return key
    return None


# --------------------------------------------------------------------------- visão geral


def overview(dossier: "CalcDossier") -> Overview:
    """A contagem por assunto e por função. Conta **todas** as fórmulas do dossiê (`counted`),
    mesmo as que o corte tirou da lista."""
    formulas = list(dossier.counted or dossier.formulas)
    total = max(dossier.formulas_total, len(formulas))
    root = dossier.root
    per_topic: dict[str, list[Formula]] = {}
    per_function: dict[str, list[Topic]] = {}
    for formula in sorted(formulas, key=lambda f: (f.line, f.end_line, f.target)):
        topic = topic_of_cached(formula)
        per_topic.setdefault(topic.key, []).append(formula)
        per_function.setdefault(formula.routine_id, []).append(topic)
    order = {key: i for i, key in enumerate(LABELS)}
    rows: list[TopicRow] = []
    for key, members in per_topic.items():
        by_routine = Counter(f.routine_id for f in members)
        functions = sorted(
            by_routine.items(), key=lambda item: (item[0] != root, -item[1], item[0])
        )
        lines = tuple(f.line for f in members if f.routine_id == root)[:MAX_LINES]
        rows.append(TopicRow(key, LABELS[key], len(members), tuple(functions), lines))
    rows.sort(key=lambda r: (r.key == OTHERS, -r.count, order[r.key]))
    function_rows: list[FunctionRow] = []
    for routine, topics in per_function.items():
        counted = Counter(t.key for t in topics)
        top = sorted(
            counted.items(), key=lambda item: (item[0] == OTHERS, -item[1], order[item[0]])
        )
        function_rows.append(
            FunctionRow(routine, len(topics), tuple((k, LABELS[k], n) for k, n in top[:3]))
        )
    function_rows.sort(key=lambda r: (r.routine_id != root, -r.count, r.routine_id))
    return Overview(
        root=root,
        total=total,
        root_total=len(per_function.get(root, ())),
        shown=len(dossier.formulas),
        by_topic=tuple(rows),
        by_function=tuple(function_rows),
        others_total=len(per_topic.get(OTHERS, ())),
    )


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _summary_line(ov: Overview) -> str:
    root = short_name(ov.root)
    aux_rows = [r for r in ov.by_function if r.routine_id != ov.root]
    aux_total = sum(r.count for r in aux_rows)
    text = f"{_plural(ov.root_total, 'fórmula', 'fórmulas')} na {root}"
    if aux_rows:
        if len(aux_rows) == 1:
            text += f"; {aux_total} na única função auxiliar"
        else:
            text += f"; {aux_total} nas {len(aux_rows)} funções auxiliares"
    return text + "."


def _where(row: TopicRow, ov: Overview, lines_shown: int, functions_shown: int) -> str:
    parts: list[str] = []
    for routine, count in row.functions[:functions_shown]:
        name = short_name(routine)
        if routine == ov.root:
            shown = ", ".join(str(n) for n in row.lines[:lines_shown])
            more = "…" if shown and (count > lines_shown or len(row.lines) > lines_shown) else ""
            parts.append(f"{name} {shown}{more}".rstrip() if shown else f"{name} ({count})")
        else:
            parts.append(f"{name} ({count})")
    if len(row.functions) > functions_shown:
        parts.append("…")
    return "; ".join(parts)


def render_overview(
    ov: Overview, *, max_words: int = OVERVIEW_MAX_WORDS, cut: bool | None = None
) -> str:
    """A seção «Visão geral» em Markdown, com teto de palavras próprio (`max_words`): passou, mostra
    menos linhas e menos funções por assunto, depois só os 4 maiores assuntos. Nunca é cortada pelo
    limite do dossiê (quem chama a põe fora da conta do corte). `cut`: a lista de fórmulas abaixo
    está cortada (padrão: a lista traz menos fórmulas do que a contagem)."""
    header = "## Visão geral"
    if ov.total == 0:
        return f"{header}\n\nNenhuma fórmula achada nesta função e nas auxiliares.\n"
    is_cut = ov.shown < ov.total if cut is None else cut
    note = " Contagem completa; a lista abaixo pode estar cortada." if is_cut else ""
    summary = _summary_line(ov)
    by_function = [r for r in ov.by_function if r.routine_id != ov.root]
    foot = "Assunto pelo nome do destino ou pela função chamada: é uma pista, confira na fórmula."

    def build(lines: int, functions: int, topics: int, with_functions: bool) -> str:
        parts = [header, "", summary + note, ""]
        parts += ["| Assunto | Fórmulas | Onde (linhas) |", "|---|---|---|"]
        for row in ov.by_topic[:topics]:
            parts.append(f"| {row.label} | {row.count} | {_where(row, ov, lines, functions)} |")
        if topics < len(ov.by_topic):
            rest = sum(r.count for r in ov.by_topic[topics:])
            parts.append(f"| demais | {rest} | |")
        if with_functions and by_function:
            shown = ", ".join(f"{short_name(r.routine_id)} {r.count}" for r in by_function[:5])
            more = ", …" if len(by_function) > 5 else ""
            parts += ["", f"Auxiliares: {shown}{more}."]
        parts += ["", foot]
        return "\n".join(parts) + "\n"

    for lines, functions, topics, with_functions in (
        (SHOWN_LINES, SHOWN_FUNCTIONS, len(ov.by_topic), True),
        (2, 2, len(ov.by_topic), True),
        (1, 2, len(ov.by_topic), True),
        (0, 2, len(ov.by_topic), True),
        (0, 1, len(ov.by_topic), False),
        (0, 1, 6, False),
        (0, 1, 4, False),
    ):
        text = build(lines, functions, topics, with_functions)
        if len(text.split()) <= max_words:
            return text
    return text


def overview_json(ov: Overview) -> dict[str, Any]:
    return {
        "root": ov.root,
        "total": ov.total,
        "root_total": ov.root_total,
        "shown": ov.shown,
        "others_total": ov.others_total,
        "by_topic": [
            {
                "key": r.key,
                "label": r.label,
                "count": r.count,
                "functions": [{"routine_id": f, "count": n} for f, n in r.functions],
                "lines": list(r.lines),
            }
            for r in ov.by_topic
        ],
        "by_function": [
            {
                "routine_id": r.routine_id,
                "count": r.count,
                "top_topics": [
                    {"key": k, "label": label, "count": n} for k, label, n in r.top_topics
                ],
            }
            for r in ov.by_function
        ],
    }


# --------------------------------------------------------------------------- perguntas sugeridas


def suggestion_json(items: Iterable[Suggestion]) -> list[dict[str, Any]]:
    return [{"text": s.text, "anchor": s.anchor, "column": s.column, "kind": s.kind} for s in items]


def render_suggestions(items: Sequence[Suggestion]) -> str:
    """As perguntas sugeridas em Markdown (CLI `--sugestoes`)."""
    parts = ["## Perguntas sugeridas", ""]
    if not items:
        parts.append("Nenhuma pergunta sugerida para este ponto de partida.")
    for i, item in enumerate(items, start=1):
        where = item.anchor if item.column is None else f"{item.anchor}#{item.column}"
        parts.append(f"{i}. {item.text}  _(âncora: `{where}`)_")
    return "\n".join(parts) + "\n"


def question_key(text: str) -> str:
    """A pergunta normalizada (sem acento, caixa nem pontuação) para achar a repetida."""
    return _squash_key(text)


def drop_asked(items: Iterable[Suggestion], asked: Iterable[str]) -> list[Suggestion]:
    """Tira a sugestão igual a uma pergunta já feita na conversa (texto normalizado)."""
    done = {question_key(q) for q in asked}
    return [s for s in items if question_key(s.text) not in done]


def _finish(
    items: Iterable[Suggestion], asked: Iterable[str] | None, limit: int
) -> list[Suggestion]:
    unique: list[Suggestion] = []
    seen: set[str] = set()
    for item in items:
        key = question_key(item.text)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    if asked:
        unique = drop_asked(unique, asked)
    return unique[:limit]


def _label_or_column(label: str | None, column: str) -> str:
    return label.strip() if label and label.strip() else column


def suggestions_for_calc(
    dossier: "CalcDossier",
    ov: Overview | None = None,
    *,
    asked: Iterable[str] | None = None,
) -> list[Suggestion]:
    """Até 8 perguntas a partir do dossiê de uma função: por assunto, por auxiliar, por dado-base
    com menu e «o que não foi rastreado»."""
    ov = ov or overview(dossier)
    items: list[Suggestion] = []
    # 1. um por assunto com >= 2 fórmulas (até 3); a função é a raiz, se ela tem o assunto
    for row in ov.by_topic:
        if row.key == OTHERS or row.count < 2 or row.key not in QUESTIONS:
            continue
        if len([i for i in items if i.kind == "assunto"]) >= MAX_TOPIC_QUESTIONS:
            break
        in_root = dict(row.functions).get(dossier.root, 0)
        routine = dossier.root if in_root >= 2 or not row.functions else row.functions[0][0]
        items.append(
            Suggestion(f"{QUESTIONS[row.key]} na {short_name(routine)}?", routine, None, "assunto")
        )
    # 2. as auxiliares com mais fórmulas (até 2)
    auxiliaries = [r for r in ov.by_function if r.routine_id != dossier.root and r.count > 0]
    auxiliaries.sort(key=lambda r: (-r.count, r.routine_id))
    for helper in auxiliaries[:MAX_AUX_QUESTIONS]:
        items.append(
            Suggestion(
                f"O que a {short_name(helper.routine_id)} calcula?",
                helper.routine_id,
                None,
                "auxiliar",
            )
        )
    # 3. entradas-base com menu (até 2): as mais usadas, com nome no dicionário na frente
    bases = [b for b in dossier.inputs if b.menus]
    bases.sort(key=lambda b: (b.label is None, -len(b.used_in)))
    for base in bases[:MAX_BASE_QUESTIONS]:
        name = _label_or_column(base.label, base.column)
        items.append(
            Suggestion(
                f"Onde se cadastra {name.lower()} ({base.column})?",
                base.table_id,
                base.column,
                "cadastro",
            )
        )
    # 4. o que ficou sem rastro
    if dossier.untraced:
        items.append(
            Suggestion(
                "O que não foi possível rastrear e por quê?", dossier.root, None, "nao_rastreado"
            )
        )
    return _finish(items, asked, MAX_SUGGESTIONS)


def suggestions_for_field(
    dossier: "FieldDossier", *, asked: Iterable[str] | None = None
) -> list[Suggestion]:
    """Perguntas a partir do dossiê de um campo: como é calculado, onde se cadastra, quem usa."""
    name = _label_or_column(dossier.label, dossier.column)
    low = name.lower()
    table = dossier.table_id
    items: list[Suggestion] = []
    if dossier.computed_by:
        writer = short_name(dossier.computed_by[0].routine_id)
        items.append(
            Suggestion(f"Como {low} é calculado na {writer}?", table, dossier.column, "campo")
        )
    if dossier.registered_in.menus:
        items.append(Suggestion(f"Onde se cadastra {low}?", table, dossier.column, "cadastro"))
    if dossier.access is not None and dossier.access.writers_total:
        items.append(Suggestion(f"Quem grava {low}?", table, dossier.column, "quem_grava"))
    if dossier.fills:  # há quem preencha o campo antes de gravar pelo DAO
        items.append(Suggestion(f"Quem preenche {low}?", table, dossier.column, "quem_preenche"))
    if dossier.read_by_total or dossier.star_readers_total:
        items.append(Suggestion(f"Quem usa {low}?", table, dossier.column, "quem_usa"))
    return _finish(items, asked, MAX_SUGGESTIONS)


def suggestions_for_table(
    dossier: "TableDossier", *, asked: Iterable[str] | None = None
) -> list[Suggestion]:
    """Até 6 perguntas a partir do dossiê de uma tabela: quem grava, onde se
    cadastra, quem lê e o que calculam as funções da base que gravam a tabela."""
    name = _label_or_column(dossier.label, dossier.table_id.rpartition(".")[2])
    low = name.lower()
    table = dossier.table_id
    access = dossier.access
    items: list[Suggestion] = []
    if access.writers_total or access.migrations:
        items.append(Suggestion(f"Quem grava {low}?", table, None, "quem_grava"))
    if dossier.registered_in.menus:
        items.append(Suggestion(f"Onde se cadastra {low}?", table, None, "cadastro"))
    if access.readers_total:
        items.append(Suggestion(f"Quem lê {low}?", table, None, "quem_usa"))
    for routine in dossier.pg_writers[:MAX_TABLE_FUNCTION_QUESTIONS]:
        items.append(Suggestion(f"O que a {short_name(routine)} calcula?", routine, None, "funcao"))
    return _finish(items, asked, MAX_TABLE_SUGGESTIONS)


def suggestions_for_term(
    result: "FieldSearch", *, asked: Iterable[str] | None = None
) -> list[Suggestion]:
    """Uma pergunta por candidato de «Usadas em cálculo» (até 4)."""
    items: list[Suggestion] = []
    for hit in result.hits:
        if hit.group != "calculo":
            continue
        table = hit.table_id.partition(":")[2].rpartition(".")[2]
        items.append(
            Suggestion(
                f"Como {hit.label.lower()} ({table}) entra no cálculo?",
                hit.table_id,
                hit.column,
                "campo",
            )
        )
        if len(items) >= MAX_TERM_QUESTIONS:
            break
    return _finish(items, asked, MAX_SUGGESTIONS)


def suggestions_for_menu(
    dossier: "MenuDossier", *, asked: Iterable[str] | None = None
) -> list[Suggestion]:
    """Uma por função com fórmulas (até 4) e «quais campos este menu cadastra?»."""
    items: list[Suggestion] = []
    for function in dossier.functions:
        if function.formulas_total <= 0:
            continue
        items.append(
            Suggestion(
                f"O que a {short_name(function.routine_id)} calcula neste menu?",
                function.routine_id,
                None,
                "funcao",
            )
        )
        if len(items) >= MAX_MENU_QUESTIONS:
            break
    if dossier.tables_written:
        items.append(
            Suggestion("Quais campos este menu cadastra?", dossier.menu_id, None, "cadastro")
        )
    return _finish(items, asked, MAX_SUGGESTIONS)


__all__ = [
    "CALL_TOPICS",
    "LABELS",
    "NAME_TOPICS",
    "OTHERS",
    "OVERVIEW_MAX_WORDS",
    "FunctionRow",
    "Overview",
    "Suggestion",
    "Topic",
    "TopicRow",
    "drop_asked",
    "normalize",
    "overview",
    "overview_json",
    "question_key",
    "render_overview",
    "render_suggestions",
    "resolve_topic_key",
    "shape_of",
    "short_label",
    "short_name",
    "suggestion_json",
    "suggestions_for_calc",
    "suggestions_for_field",
    "suggestions_for_menu",
    "suggestions_for_table",
    "suggestions_for_term",
    "topic_of",
    "topic_of_cached",
]
