"""Leitor léxico de JavaScript: literais de string, sem Node.

O e-cidade monta as URLs da API por concatenação simples (`baseUrl + '/tributario/…/search/'`)
ou por template (`` `${PHPSession.requestApi}/tributario/…/buscar` ``). Basta, então, achar os
literais `'…'`, `"…"` e `` `…` `` fora dos comentários e das expressões regulares:

- `${…}` de um template vira o curinga de segmento `{}` (`WILDCARD`); o do começo é a base da
  URL e sai do literal;
- `\\x00` marca o código PHP que separa dois trechos de HTML (`<script>` inline de uma tela, ver
  `php-extractor/src/JsLexer.php`): dentro de uma string vira `{}`, fora dela é ignorado;
- `/…/` é expressão regular quando vem depois de operador, abertura ou palavra como `return`.

O mesmo algoritmo existe em PHP (`JsLexer.php`), para o `<script>` inline das telas; a tabela
`tests/fixtures/js/lexer_cases.json` roda nos dois. Funções puras: testáveis sem Docker.
"""

import re
from bisect import bisect_right
from dataclasses import dataclass

from php_map.ingest.records import HttpLiteral, JsRecord, StringRef

WILDCARD = "{}"
PHP_GAP = "\x00"
# Literal `*.php` (o mesmo do extrator, `CollectorVisitor::STRING_REF_RE`).
STRING_REF_RE = re.compile(r"([A-Za-z0-9_./-]+\.php)(\?[^'\"\s]*)?")
# Caminho com cara de rota de API: dois ou mais segmentos, o 1º em minúsculas.
HTTP_PATH_RE = re.compile(r"^/?[a-z0-9-]+(/[A-Za-z0-9_{}.-]*)+/?$")
API_PREFIX_RE = re.compile(r"requestApi|v4/api")
_IDENT = re.compile(r"[A-Za-z0-9_$]+")
_QUERY = re.compile(r"[?#]")
# Depois destes caracteres, `/` abre uma expressão regular (não é divisão).
_REGEX_AFTER = frozenset("(,=:[!&|?{};+-*%<>~^")
_REGEX_WORDS = frozenset(
    {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw"}
)


@dataclass(frozen=True, slots=True)
class JsLiteral:
    text: str
    line: int


@dataclass(frozen=True, slots=True)
class LexResult:
    literals: list[JsLiteral]
    code: str  # o texto sem os comentários (para `has_api_prefix`)


def lex(text: str, start_line: int = 1) -> LexResult:
    """Literais de string de `text` (linha = a de abertura), sem os aninhados em `${…}`."""
    breaks = [m.start() for m in re.finditer("\n", text)]
    literals: list[JsLiteral] = []
    code: list[str] = []
    n = len(text)
    i = 0
    prev = ""  # último caractere significativo fora de string ("a" = operando)
    word = ""  # último identificador
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if c == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            code.append("\n" * text.count("\n", i, n if end < 0 else end))
            i = n if end < 0 else end + 2
            continue
        if c in "'\"`":
            value, end = _template(text, i) if c == "`" else _quoted(text, i)
            literals.append(JsLiteral(value, start_line + bisect_right(breaks, i - 1)))
            code.append(text[i:end])
            i, prev = end, "a"
            continue
        if c == "/" and _regex_allowed(prev, word):
            end = _regex_end(text, i)
            code.append(text[i:end])
            i, prev, word = end, "a", ""
            continue
        match = _IDENT.match(text, i)
        if match:
            word = match.group()
            code.append(word)
            i, prev = match.end(), "a"
            continue
        if not c.isspace() and c != PHP_GAP:
            prev = "a" if c in ")]" else c
            word = ""
        code.append(c)
        i += 1
    return LexResult(literals, "".join(code))


def _regex_allowed(prev: str, word: str) -> bool:
    """`/` abre expressão regular no começo, depois de operador/abertura ou de `return` & cia."""
    return prev == "" or prev in _REGEX_AFTER or (prev == "a" and word in _REGEX_WORDS)


def _quoted(text: str, i: int) -> tuple[str, int]:
    """String `'…'`/`"…"` a partir da aspa em `i`: (valor, posição depois do fim)."""
    quote, j, n = text[i], i + 1, len(text)
    buf: list[str] = []
    while j < n:
        ch = text[j]
        if ch == "\\" and j + 1 < n:
            if text[j + 1] != "\n":  # `\` no fim da linha continua a string
                buf.append(text[j + 1])
            j += 2
            continue
        if ch == quote:
            return "".join(buf), j + 1
        if ch == "\n":  # sem fechamento: termina na linha
            return "".join(buf), j
        buf.append(WILDCARD if ch == PHP_GAP else ch)
        j += 1
    return "".join(buf), n


def _template(text: str, i: int) -> tuple[str, int]:
    """Template `` `…` ``: cada `${…}` vira `{}`; o que há dentro dele não é literal."""
    j, n = i + 1, len(text)
    buf: list[str] = []
    while j < n:
        ch = text[j]
        if ch == "\\" and j + 1 < n:
            buf.append(text[j + 1])
            j += 2
            continue
        if ch == "`":
            return "".join(buf), j + 1
        if ch == "$" and text.startswith("{", j + 1):
            j = _expression_end(text, j + 2)
            buf.append(WILDCARD)
            continue
        buf.append(WILDCARD if ch == PHP_GAP else ch)
        j += 1
    return "".join(buf), n


def _expression_end(text: str, j: int) -> int:
    """Posição depois do `}` que fecha o `${` (chaves e strings aninhadas)."""
    depth, n = 1, len(text)
    while j < n:
        ch = text[j]
        if ch in "'\"":
            j = _quoted(text, j)[1]
            continue
        if ch == "`":
            j = _template(text, j)[1]
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def _regex_end(text: str, i: int) -> int:
    """Fim de `/…/flags` (respeita `\\` e `[…]`); nunca passa da linha."""
    j, n, in_class = i + 1, len(text), False
    while j < n:
        ch = text[j]
        if ch == "\n":
            return j
        if ch == "\\":
            j += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            match = _IDENT.match(text, j + 1)
            return match.end() if match else j + 1
        j += 1
    return n


def http_candidate(text: str) -> str | None:
    """O literal sem o curinga inicial (a base da URL), se tem cara de caminho de API."""
    value = text.strip()
    while value.startswith(WILDCARD):
        value = value[len(WILDCARD) :]
    head = _QUERY.split(value, maxsplit=1)[0]
    return value if head and HTTP_PATH_RE.match(head) else None


def http_literals(literals: list[JsLiteral]) -> list[HttpLiteral]:
    seen: set[tuple[str, int]] = set()
    found: list[HttpLiteral] = []
    for literal in literals:
        value = http_candidate(literal.text)
        if value is not None and (value, literal.line) not in seen:
            seen.add((value, literal.line))
            found.append(HttpLiteral(text=value, line=literal.line))
    return found


def decode(raw: bytes) -> tuple[str, bool]:
    """UTF-8 e, se não decodificar, ISO-8859-1 (`True` = caiu no fallback, como o extrator)."""
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("latin-1"), True


def scan_js(path: str, text: str | bytes) -> JsRecord:
    """Literais `*.php`, URLs com cara de rota e a marca `has_api_prefix` de um arquivo JS."""
    fallback = False
    if isinstance(text, bytes):
        text, fallback = decode(text)
    result = lex(text)
    refs: list[StringRef] = []
    seen: set[tuple[str, int]] = set()
    for literal in result.literals:
        for match in STRING_REF_RE.finditer(literal.text):
            key = (match.group(1), literal.line)
            if key not in seen:
                seen.add(key)
                refs.append(StringRef(target=key[0], line=key[1], origin="js_string"))
    return JsRecord(
        path=path,
        encoding_fallback=fallback,
        string_refs=refs,
        http_literals=http_literals(result.literals),
        has_api_prefix=API_PREFIX_RE.search(result.code) is not None,
    )
