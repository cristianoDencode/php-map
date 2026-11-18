"""Leitor léxico das regras de negócio no corpo das funções da base.

Extrai, do texto de uma função (`pg_get_functiondef`, como `routine_sources.ddl` guarda), as
regras que o PL/pgSQL **declara por escrito**: validações (`RAISE EXCEPTION` guardado por
`IF`/`ELSIF`/`CASE WHEN`), erros e avisos sem guarda, diagnóstico (`NOTICE`/`INFO`/`DEBUG`/`LOG`),
decisões de fluxo, tratamento de exceção (`EXCEPTION WHEN`) e comentários que parecem texto para
humano (não código desativado).

**Léxico, não semântico.** O leitor entende comentários, texto entre aspas e `$tag$ … $tag$`,
mas não o que o código faz. Por isso funciona nas funções de trigger em que o `parse_plpgsql`
falha: nunca toca o parser PL/pgSQL, só o texto. A confiança de toda regra é 1.0 (é o
texto escrito, lido ao pé da letra); a única marca é `truncated`, quando a condição passa de
`MAX_CONDITION` caracteres.

Duas passadas sobre o `ddl` inteiro (linha 1 = `CREATE`, como as arestas `db:<schema>.<nome>`):
1. `_scan_spans`: acha comentários, textos entre aspas simples/duplas e `$tag$ … $tag$`, cada um
   com posição e conteúdo. Gera também um texto "mascarado" (mesmo tamanho, mesmas quebras de
   linha) com o conteúdo desses trechos trocado por espaços, para o léxico de controle não
   confundir uma palavra-chave dentro de comentário ou texto com uma de verdade.
2. `_walk`: sobre o texto mascarado, acha as palavras-chave de controle (`RAISE`, `IF`/`ELSIF`/
   `ELSE`/`END IF`, `CASE`/`WHEN`/`END CASE`, `EXCEPTION`, `LOOP`, `DECLARE`/`BEGIN`/`END`) e
   percorre com uma pilha de blocos abertos, para a condição de cada regra levar a cadeia de até
   3 níveis. Quando precisa do texto de verdade (condição, mensagem do `RAISE`), relê o `ddl`
   original nas mesmas posições (o texto mascarado só serve para achar os marcadores).
"""

import bisect
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

import pglast
from pglast import ast
from pglast.enums import FunctionParameterMode
from pglast.stream import RawStream


def _normalize(text: str) -> str:
    """Minúsculas e sem acento, como `search.index.normalize_text` (cópia local: evitaria um
    import circular, já que `search/index.py` lê `Rule` desta mesma tarefa)."""
    plain = "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )
    return plain.lower()


MAX_CONDITION = 300
MAX_CHAIN = 3

_PARAM_MODE = {
    FunctionParameterMode.FUNC_PARAM_IN: "IN",
    FunctionParameterMode.FUNC_PARAM_OUT: "OUT",
    FunctionParameterMode.FUNC_PARAM_INOUT: "INOUT",
    FunctionParameterMode.FUNC_PARAM_VARIADIC: "VARIADIC",
}

_RAISE_LEVELS = frozenset({"WARNING", "NOTICE", "INFO", "DEBUG", "LOG", "EXCEPTION"})
_DIAGNOSTIC_LEVELS = frozenset({"NOTICE", "INFO", "DEBUG", "LOG"})

# Palavras-chave de controle que o `_walk` reconhece no texto mascarado. A ordem importa: as
# formas compostas (`END IF`, `END CASE`, `END LOOP`) precisam vir antes do `END` solto, senão
# o `END` solto casaria primeiro e sobraria um `IF`/`CASE`/`LOOP` written à parte.
_ATOM_RE = re.compile(
    r"\bEND\s+IF\b|\bEND\s+CASE\b|\bEND\s+LOOP\b|\bEND\b|\bIF\b|\bELSIF\b|\bELSE\b|\bCASE\b"
    r"|\bWHEN\b|\bTHEN\b|\bRAISE\b|\bEXCEPTION\b|\bLOOP\b|\bPERFORM\b|\bEXECUTE\b|\bDECLARE\b"
    r"|\bBEGIN\b|\bWARNING\b|\bNOTICE\b|\bINFO\b|\bDEBUG\b|\bLOG\b|\bUSING\b|\bSQLSTATE\b"
    r"|\bMESSAGE\b|\bERRCODE\b|;|\|\|",
    re.IGNORECASE,
)
_WORD_LETTERS = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+")
_WS_RE = re.compile(r"\s+")
_LEADING_WORD = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)")
_HEADER_WORDS = ("data", "autor", "criado", "alteracao", "modificado", "copyright")
_CODE_WORDS = frozenset(
    {
        "select", "insert", "update", "delete", "if", "then", "else", "end", "return",
        "raise", "perform", "execute", "declare", "begin", "for", "loop", "from", "where",
    }
)  # fmt: skip
_DECLARE_RE = re.compile(r"\bDECLARE\b(.*?)\bBEGIN\b", re.IGNORECASE | re.DOTALL)
_BODY_START = re.compile(r"\bAS\s+(\$[A-Za-z0-9_]*\$)", re.IGNORECASE)
_DOLLAR_TAG = re.compile(r"\$[A-Za-z0-9_]*\$")
# Início de comentário ou texto entre aspas/`$`, para o `_scan_spans` pular o código "normal"
# de uma vez (busca em C), em vez de andar caractere a caractere em Python.
_SPECIAL = re.compile(r"--|/\*|['\"$]")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True, slots=True)
class Rule:
    kind: str  # validation | error | warning | diagnostic | decision | handler | comment
    line: int  # linha no ddl (1 = CREATE)
    end_line: int
    level: str | None = None  # EXCEPTION | WARNING | NOTICE | INFO | DEBUG | LOG (do RAISE)
    condition: str | None = None  # condições que guardam o ponto, ≤ 3 níveis
    message: str | None = None  # formato do RAISE ou o texto do comentário
    params: tuple[str, ...] = ()  # expressões dos `%`
    sqlstate: str | None = None  # RAISE ... USING ERRCODE / EXCEPTION WHEN <condição>
    dynamic: bool = False  # mensagem montada com `||`: `message` guarda os pedaços literais
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class RoutineProfile:
    params: tuple[tuple[str, str, str, str | None], ...]  # (nome, modo, tipo, default)
    returns: str
    language: str
    volatility: str
    security_definer: bool
    counts: dict[str, int]
    declared: tuple[str, ...]  # variáveis do DECLARE


# --------------------------------------------------------------------------- cabeçalho (pglast)


def _function_header(
    ddl: str,
) -> tuple[tuple[tuple[str, str, str, str | None], ...], str, str, str, bool]:
    """Parâmetros, retorno, linguagem, volatilidade e `SECURITY DEFINER`, do `CREATE FUNCTION`.

    Usa `pglast.parse_sql`, que só lê o cabeçalho (o corpo é uma string para esse parser, nunca
    entra no `parse_plpgsql`): funciona mesmo quando o corpo é de uma função de trigger que o
    `parse_plpgsql` não leria.
    """
    try:
        stmts = pglast.parse_sql(ddl)
    except pglast.parser.ParseError:
        return (), "unknown", "unknown", "volatile", False
    stmt = next((s.stmt for s in stmts if isinstance(s.stmt, ast.CreateFunctionStmt)), None)
    if stmt is None:
        return (), "unknown", "unknown", "volatile", False
    params: list[tuple[str, str, str, str | None]] = []
    for p in stmt.parameters or ():
        if not isinstance(p, ast.FunctionParameter):
            continue
        mode = _PARAM_MODE.get(p.mode, "IN") if p.mode is not None else "IN"
        type_name = _type_name(p.argType)
        default = RawStream()(p.defexpr) if p.defexpr is not None else None
        params.append((p.name or "", mode, type_name, default))
    language, volatility, security_definer = "sql", "volatile", False
    for option in stmt.options or ():
        if not isinstance(option, ast.DefElem):
            continue
        if option.defname == "language" and isinstance(option.arg, ast.String):
            language = (option.arg.sval or "sql").lower()
        elif option.defname == "volatility" and isinstance(option.arg, ast.String):
            volatility = (option.arg.sval or "volatile").lower()
        elif option.defname == "security" and isinstance(option.arg, ast.Boolean):
            security_definer = bool(option.arg.boolval)
    returns = "void"
    if stmt.returnType is not None:
        returns = ("setof " if stmt.returnType.setof else "") + _type_name(stmt.returnType)
    return tuple(params), returns, language, volatility, security_definer


def _type_name(type_: ast.TypeName | None) -> str:
    if type_ is None or not type_.names:
        return "unknown"
    parts = [p.sval.lower() for p in type_.names if isinstance(p, ast.String) and p.sval]
    name = parts[-1] if parts else "unknown"
    if len(parts) > 1 and parts[0] != "pg_catalog":
        name = ".".join(parts)
    return name + "[]" * len(type_.arrayBounds or ())


# --------------------------------------------------------------------------- corpo (texto)


def _function_body(ddl: str) -> tuple[int, int] | None:
    """Posições (início, fim) do corpo, entre os `$tag$` do `AS` do cabeçalho."""
    match = _BODY_START.search(ddl)
    if match is None:
        return None
    tag = match.group(1)
    start = match.end()
    end = ddl.rfind(tag)
    if end < start:
        return None
    return start, end


@dataclass(frozen=True, slots=True)
class _Span:
    kind: str  # comment_line | comment_block | string | dquote | dollar
    start: int
    end: int
    inner: str


def _scan_spans(text: str) -> list[_Span]:
    """Comentários, textos entre aspas e `$tag$ … $tag$`, em ordem, sem se sobrepor.

    O código "normal" entre um achado e o próximo é pulado de uma vez pelo `_SPECIAL.search`
    (motor em C), em vez de andar caractere a caractere em Python: medido no e-cidade real, isso
    tirou a maior parte do tempo desta função (a esmagadora maioria do texto não é comentário
    nem aspas)."""
    spans: list[_Span] = []
    n = len(text)
    i = 0
    while True:
        match = _SPECIAL.search(text, i)
        if match is None:
            break
        i = match.start()
        ch = text[i]
        if ch == "-":
            end = text.find("\n", i)
            end = n if end < 0 else end
            spans.append(_Span("comment_line", i, end, text[i + 2 : end]))
            i = end
        elif ch == "/":
            depth, j = 1, i + 2
            while j < n and depth:
                if text[j : j + 2] == "/*":
                    depth += 1
                    j += 2
                elif text[j : j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            spans.append(_Span("comment_block", i, j, text[i + 2 : max(i + 2, j - 2)]))
            i = j
        elif ch == "'":
            escape = i > 0 and text[i - 1] in "eE" and (i == 1 or not text[i - 2].isalnum())
            j = i + 1
            while j < n:
                if escape and text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            spans.append(_Span("string", i, j, text[i + 1 : max(i + 1, j - 1)]))
            i = j
        elif ch == '"':
            j = i + 1
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            spans.append(_Span("dquote", i, j, text[i + 1 : max(i + 1, j - 1)]))
            i = j
        elif ch == "$":
            tag_match = _DOLLAR_TAG.match(text, i)
            if tag_match is None:
                i += 1
                continue
            tag = tag_match.group(0)
            close = text.find(tag, tag_match.end())
            if close < 0:
                i = tag_match.end()
                continue
            spans.append(_Span("dollar", i, close + len(tag), text[tag_match.end() : close]))
            i = close + len(tag)
        else:
            i += 1
    return spans


def _mask(text: str, spans: list[_Span]) -> str:
    """`text`, do mesmo tamanho, com os trechos de `spans` trocados por espaço (preserva `\\n`:
    as linhas continuam valendo direto no texto mascarado).

    Monta por partes, copiando o que está **fora** dos spans de uma vez (fatia direta, sem
    passar por cada caractere); só o que está dentro de um span — tipicamente uma fração pequena
    do texto — é trocado caractere a caractere."""
    parts: list[str] = []
    cursor = 0
    for span in spans:
        parts.append(text[cursor : span.start])
        inner = text[span.start : span.end]
        if "\n" in inner:
            parts.append("".join(c if c == "\n" else " " for c in inner))
        else:
            parts.append(" " * len(inner))
        cursor = span.end
    parts.append(text[cursor:])
    return "".join(parts)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_CONDITION:
        return text, False
    return text[:MAX_CONDITION], True


# --------------------------------------------------------------------------- comentários


def _classify_comment(span: _Span, line_of: Callable[[int], int]) -> tuple[Rule | None, str]:
    """Um `Rule` (`kind="comment"`) ou `None`, e a chave do `counts` quando não vira linha."""
    stripped = span.inner.strip()
    plain = _normalize(stripped)
    words = _WORD_LETTERS.findall(stripped)
    if any(plain.startswith(w) for w in _HEADER_WORDS):
        return None, "header_comment"
    if len(words) <= 3:
        return None, "short_comment"
    name_match = _LEADING_WORD.match(stripped)
    first = name_match.group(1) if name_match else ""
    looks_like_code = (
        first.lower() in _CODE_WORDS or ":=" in stripped or stripped.rstrip().endswith(";")
    )
    if looks_like_code:
        return None, "commented_code"
    line = line_of(span.start)
    end_line = line_of(max(span.start, span.end - 1))
    return Rule(kind="comment", line=line, end_line=end_line, message=stripped), ""


# --------------------------------------------------------------------------- RAISE


def _peek_string(text: str, spans_by_start: dict[int, _Span], pos: int) -> tuple[_Span, int] | None:
    """O próximo `_Span` de texto entre aspas simples a partir de `pos`, pulando espaço/`\\n`."""
    j = pos
    while j < len(text) and text[j].isspace():
        j += 1
    span = spans_by_start.get(j)
    if span is not None and span.kind == "string":
        return span, span.end
    return None


def _decode_string(raw: str) -> str:
    return raw.replace("''", "'")


def _parse_raise(
    text: str,
    spans_by_start: dict[int, _Span],
    pos: int,
    end_of_statement: int,
    line_of: Callable[[int], int],
    start_line: int,
) -> Rule:
    """`pos`: posição em `text` logo depois da palavra `RAISE`. `end_of_statement`: posição do
    `;` que termina a instrução (achado pelo `_walk`)."""
    j = pos
    while j < len(text) and text[j].isspace():
        j += 1
    level = "EXCEPTION"
    word_match = _IDENT_RE.match(text, j)
    if word_match and word_match.group(0).upper() in _RAISE_LEVELS:
        level = word_match.group(0).upper()
        j = word_match.end()
        while j < len(text) and text[j].isspace():
            j += 1
    sqlstate: str | None = None
    message: str | None = None
    dynamic = False
    params: list[str] = []
    word_match = _IDENT_RE.match(text, j)
    if word_match and word_match.group(0).upper() == "SQLSTATE":
        j = word_match.end()
        found = _peek_string(text, spans_by_start, j)
        if found is not None:
            span, j = found
            sqlstate = _decode_string(span.inner)
    elif word_match and word_match.group(0).upper() == "USING":
        pass  # `RAISE [level] USING MESSAGE = '...'`: tratado abaixo, a partir de `j`
    else:
        found = _peek_string(text, spans_by_start, j)
        if found is not None:
            span, j = found
            parts = [_decode_string(span.inner)]
            while True:
                k = j
                while k < len(text) and text[k].isspace():
                    k += 1
                if text[k : k + 2] != "||":
                    break
                dynamic = True
                k += 2
                while k < len(text) and text[k].isspace():
                    k += 1
                next_found = _peek_string(text, spans_by_start, k)
                if next_found is not None:
                    span2, j = next_found
                    parts.append(_decode_string(span2.inner))
                    continue
                # Expressão sem texto literal (variável, chamada…): avança sem interpretar,
                # pulando por cima de textos embutidos, até o próximo `||` de nível 0 ou até o
                # fim do trecho do RAISE (`,`/`;`/`USING`), para continuar achando as peças.
                depth, m = 0, k
                while m < end_of_statement:
                    inner_span = spans_by_start.get(m)
                    if inner_span is not None:
                        m = inner_span.end
                        continue
                    ch = text[m]
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                    elif (
                        depth == 0
                        and text[m : m + 2] == "||"
                        or depth == 0
                        and ch in ",;"
                        or depth == 0
                        and re.match(r"USING\b", text[m:], re.IGNORECASE)
                    ):
                        break
                    m += 1
                j = m
            message = " … ".join(parts) if dynamic else parts[0]

    tail = text[j:end_of_statement]
    if message is not None and not dynamic:
        comma = tail.find(",")
        using_match = re.search(r"\bUSING\b", tail, re.IGNORECASE)
        args_end = using_match.start() if using_match else len(tail)
        if comma >= 0 and comma < args_end:
            args_text = tail[comma + 1 : args_end]
            params = [_clean(p) for p in _split_top_level_commas(args_text) if _clean(p)]
    using_match = re.search(r"\bUSING\b", tail, re.IGNORECASE)
    if using_match:
        using_text = tail[using_match.end() :]
        msg_match = re.search(r"\bMESSAGE\s*=\s*", using_text, re.IGNORECASE)
        if msg_match:
            found = _peek_string(text, spans_by_start, j + using_match.end() + msg_match.end())
            if found is not None:
                message = _decode_string(found[0].inner)
        code_match = re.search(r"\bERRCODE\s*=\s*", using_text, re.IGNORECASE)
        if code_match:
            found = _peek_string(text, spans_by_start, j + using_match.end() + code_match.end())
            if found is not None:
                sqlstate = _decode_string(found[0].inner)

    end_line = line_of(end_of_statement)
    kind = (
        "diagnostic"
        if level in _DIAGNOSTIC_LEVELS
        else "warning"
        if level == "WARNING"
        else "error"
    )
    return Rule(
        kind=kind,
        line=start_line,
        end_line=end_line,
        level=level,
        message=message,
        params=tuple(params),
        sqlstate=sqlstate,
        dynamic=dynamic,
    )


def _split_top_level_commas(text: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


# --------------------------------------------------------------------------- blocos (IF/CASE/…)


@dataclass(slots=True)
class _Frame:
    kind: str  # if | case_stmt | begin | loop
    label: str | None = None
    exception: bool = False


def _chain(stack: list[_Frame]) -> str | None:
    labels = [f.label for f in stack if f.kind in ("if", "case_stmt") and f.label]
    if not labels:
        return None
    return " E ".join(labels[-MAX_CHAIN:])


def _classify_case(atoms: list[tuple[str, int, int]], pos: int) -> tuple[bool, int]:
    """Se o `CASE` em `pos` é de instrução (fecha com `END CASE`) ou de expressão (`END` seco),
    e o índice do seu próprio fechamento, achado pela profundidade de `CASE`/`END*` aninhados."""
    depth = 1
    i = pos + 1
    while i < len(atoms):
        kind = atoms[i][0]
        if kind == "CASE":
            depth += 1
        elif kind in ("END", "END CASE"):
            depth -= 1
            if depth == 0:
                return kind == "END CASE", i
        i += 1
    return True, len(atoms) - 1  # malformado: trata como instrução e para no fim


def _find_then(atoms: list[tuple[str, int, int]], pos: int) -> int:
    """Índice do `THEN` que fecha a condição aberta em `pos` (`IF`/`ELSIF`/`WHEN`), pulando
    por cima de um `CASE` de expressão dentro da própria condição (armadilha conhecida)."""
    i = pos + 1
    while i < len(atoms):
        kind = atoms[i][0]
        if kind == "THEN":
            return i
        if kind == "CASE":
            is_stmt, closer = _classify_case(atoms, i)
            i = closer + 1 if not is_stmt else i + 1
            continue
        i += 1
    return len(atoms) - 1


def scan_routine(ddl: str) -> tuple[list[Rule], RoutineProfile]:
    """As regras e o perfil de uma função, a partir do texto completo (`pg_get_functiondef`)."""
    params, returns, language, volatility, security_definer = _function_header(ddl)
    counts: Counter[str] = Counter()
    body_range = _function_body(ddl)
    if body_range is None:
        return [], RoutineProfile(params, returns, language, volatility, security_definer, {}, ())
    body_start, body_end = body_range
    body = ddl[body_start:body_end]
    spans = _scan_spans(body)
    spans_by_start = {s.start: s for s in spans}
    masked = _mask(body, spans)

    # `re.finditer` roda no motor em C; o laço em Python puro (`enumerate` + comparação por
    # caractere) chegou a ser 1/3 do tempo do leitor nas 1.267 funções reais (medido).
    newlines = [m.start() for m in re.finditer("\n", ddl)]

    def line_of(offset_in_body: int) -> int:
        absolute = body_start + offset_in_body
        return 1 + bisect.bisect_right(newlines, absolute - 1)

    rules: list[Rule] = []
    for span in spans:
        if span.kind not in ("comment_line", "comment_block"):
            continue
        rule, bucket = _classify_comment(span, line_of)
        if rule is not None:
            rules.append(rule)
        elif bucket:
            counts[bucket] += 1

    atoms: list[tuple[str, int, int]] = []
    for match in _ATOM_RE.finditer(masked):
        raw = match.group(0)
        # Caminho rápido: só `END IF`/`END CASE`/`END LOOP` têm espaço no meio (às vezes mais de
        # um, ou quebra de linha); o resto (~91% dos casos reais) é uma palavra ou pontuação só,
        # sem precisar da troca por regex. Medido no e-cidade real: ~57% dos átomos são só `;`/
        # `\|\|`, que nem entram nos `elif` abaixo — por isso o despacho rápido no topo do laço.
        text = raw.upper() if raw.isalnum() or raw in (";", "||") else _WS_RE.sub(" ", raw).upper()
        atoms.append((text, match.start(), match.end()))

    stack: list[_Frame] = []
    pos = 0
    while pos < len(atoms):
        kind, start, end = atoms[pos]
        if kind == ";" or kind == "||":
            pos += 1
        elif kind == "IF":
            then_idx = _find_then(atoms, pos)
            cond_raw, truncated = _truncate(_clean(body[end : atoms[then_idx][1]]))
            frame = _Frame("if", cond_raw)
            rules.append(
                Rule(
                    "decision", line_of(start), line_of(atoms[then_idx][2]),
                    condition=_chain([*stack, frame]), truncated=truncated,
                )
            )  # fmt: skip
            stack.append(frame)
            pos = then_idx + 1
        elif kind == "ELSIF" and stack and stack[-1].kind == "if":
            then_idx = _find_then(atoms, pos)
            cond_raw, truncated = _truncate(_clean(body[end : atoms[then_idx][1]]))
            stack[-1].label = cond_raw
            rules.append(
                Rule(
                    "decision", line_of(start), line_of(atoms[then_idx][2]),
                    condition=_chain(stack), truncated=truncated,
                )
            )  # fmt: skip
            pos = then_idx + 1
        elif kind == "ELSE" and stack and stack[-1].kind == "if":
            previous = stack[-1].label or ""
            stack[-1].label = f"SENÃO de {previous}" if previous else "SENÃO"
            rules.append(Rule("decision", line_of(start), line_of(end), condition=_chain(stack)))
            pos += 1
        elif kind == "END IF" and stack and stack[-1].kind == "if":
            stack.pop()
            pos += 1
        elif kind == "CASE":
            is_stmt, closer = _classify_case(atoms, pos)
            if not is_stmt:
                pos = closer + 1
                continue
            stack.append(_Frame("case_stmt"))
            pos += 1
        elif kind == "WHEN" and stack and stack[-1].kind == "case_stmt":
            then_idx = _find_then(atoms, pos)
            cond_raw, truncated = _truncate(_clean(body[end : atoms[then_idx][1]]))
            stack[-1].label = cond_raw
            rules.append(
                Rule(
                    "decision", line_of(start), line_of(atoms[then_idx][2]),
                    condition=_chain(stack), truncated=truncated,
                )
            )  # fmt: skip
            pos = then_idx + 1
        elif kind == "WHEN" and any(f.kind == "begin" and f.exception for f in stack[-1:]):
            then_idx = _find_then(atoms, pos)
            cond_raw, truncated = _truncate(_clean(body[end : atoms[then_idx][1]]))
            rules.append(
                Rule(
                    "handler", line_of(start), line_of(atoms[then_idx][2]),
                    condition=cond_raw, truncated=truncated,
                )
            )  # fmt: skip
            pos = then_idx + 1
        elif kind == "END CASE" and stack and stack[-1].kind == "case_stmt":
            stack.pop()
            pos += 1
        elif kind == "BEGIN":
            stack.append(_Frame("begin"))
            pos += 1
        elif kind == "EXCEPTION" and stack and stack[-1].kind == "begin":
            stack[-1].exception = True
            pos += 1
        elif kind == "END" and stack and stack[-1].kind == "begin":
            stack.pop()
            pos += 1
        elif kind == "LOOP":
            counts["loops"] += 1
            stack.append(_Frame("loop"))
            pos += 1
        elif kind == "END LOOP" and stack and stack[-1].kind == "loop":
            stack.pop()
            pos += 1
        elif kind == "PERFORM":
            counts["performs"] += 1
            pos += 1
        elif kind == "EXECUTE":
            counts["dynamic_sql"] += 1
            pos += 1
        elif kind == "RAISE":
            semi_idx = pos + 1
            while semi_idx < len(atoms) and atoms[semi_idx][0] != ";":
                semi_idx += 1
            end_of_statement = atoms[semi_idx][1] if semi_idx < len(atoms) else len(body)
            rule = _parse_raise(
                body, spans_by_start, end, end_of_statement, line_of, line_of(start)
            )
            chain = _chain(stack)
            if chain and rule.kind == "error":
                rule = Rule(
                    "validation",
                    rule.line,
                    rule.end_line,
                    rule.level,
                    chain,
                    rule.message,
                    rule.params,
                    rule.sqlstate,
                    rule.dynamic,
                    rule.truncated,
                )
            elif chain:
                rule = Rule(
                    rule.kind,
                    rule.line,
                    rule.end_line,
                    rule.level,
                    chain,
                    rule.message,
                    rule.params,
                    rule.sqlstate,
                    rule.dynamic,
                    rule.truncated,
                )
            rules.append(rule)
            pos = semi_idx + 1
        else:
            pos += 1

    for kind_name, total in Counter(r.kind for r in rules).items():
        counts[kind_name] = total

    declared: list[str] = []
    seen: set[str] = set()
    for declare_match in _DECLARE_RE.finditer(masked):
        for piece in declare_match.group(1).split(";"):
            name_match = _LEADING_WORD.match(piece)
            if name_match:
                name = name_match.group(1)
                key = name.lower()
                if key not in seen:
                    seen.add(key)
                    declared.append(name)

    profile = RoutineProfile(
        params=params,
        returns=returns,
        language=language,
        volatility=volatility,
        security_definer=security_definer,
        counts=dict(counts),
        declared=tuple(declared),
    )
    return rules, profile
