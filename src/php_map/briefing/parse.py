"""Leitura do arquivo de modelo: cabeçalho restrito e marcadores do corpo.

O cabeçalho é um subconjunto pequeno de YAML (sem a dependência): linhas `chave: valor`, mapas em
linha `{a: 1, b: "x"}`, listas em linha `[a, b]` e a lista `parametros:` com um item por linha
(`- {nome: tela, tipo: tela}`). Nada além disso é aceito.

Os marcadores do corpo são só `{{parametro}}` e `{{bloco:<nome> chave=valor …}}`, com valor
`{{parametro}}`, texto entre aspas ou uma palavra. `{{` e `}}` aninhados (o valor `{{tela}}` dentro
do bloco) são contados; qualquer outra coisa entre chaves é problema de validação.
"""

import re
from dataclasses import dataclass
from typing import Any

from php_map.briefing.model import (
    DEFAULT_WORD_LIMIT,
    MAX_WORD_LIMIT,
    PARAM_TYPES,
    RESPONSE_FORMATS,
    STATUSES,
    USER,
    Param,
    Problem,
    Template,
    TemplateError,
)

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_KEY = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")

# --------------------------------------------------------------------------- escalares e listas


def _strip_comment(text: str) -> str:
    """Tira ` # comentário` do fim, fora de aspas."""
    quote = ""
    for i, char in enumerate(text):
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
    return text.rstrip()


def parse_scalar(raw: str) -> Any:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.lower() in ("true", "sim"):
        return True
    if text.lower() in ("false", "nao", "não"):
        return False
    if text.lower() in ("null", "~", ""):
        return None
    if _NUMBER.match(text):
        return float(text) if "." in text else int(text)
    return text


def split_top(text: str, sep: str = ",") -> list[str]:
    """Divide em `sep` fora de aspas, colchetes e chaves."""
    parts: list[str] = []
    depth, quote, start = 0, "", 0
    for i, char in enumerate(text):
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def parse_value(raw: str) -> Any:
    """Escalar, lista `[a, b]` ou mapa `{a: 1}` em linha."""
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        return [parse_value(p) for p in split_top(text[1:-1])]
    if text.startswith("{") and text.endswith("}"):
        result: dict[str, Any] = {}
        for item in split_top(text[1:-1]):
            key, sep, value = item.partition(":")
            if not sep:
                raise ValueError(f"item sem `chave: valor`: {item!r}")
            result[key.strip()] = parse_value(value)
        return result
    return parse_scalar(text)


# --------------------------------------------------------------------------- cabeçalho


def split_document(text: str) -> tuple[list[tuple[int, str]], str, int]:
    """`(linhas do cabeçalho com o número da linha, corpo, linha em que o corpo começa)`."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise TemplateError(
            [
                Problem(
                    1, "o modelo precisa começar com um cabeçalho `---`", "veja `criar-novo-modelo`"
                )
            ]
        )
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            head = [(n + 1, lines[n]) for n in range(1, index)]
            return head, "\n".join(lines[index + 1 :]), index + 2
    raise TemplateError([Problem(1, "o cabeçalho não termina: falta a segunda linha `---`")])


def parse_header(
    head: list[tuple[int, str]],
) -> tuple[dict[str, Any], dict[str, int], list[Problem]]:
    data: dict[str, Any] = {}
    lines_of: dict[str, int] = {}
    problems: list[Problem] = []
    current: str | None = None
    for number, raw in head:
        line = _strip_comment(raw)
        if not line.strip():
            continue
        item = line.lstrip()
        if item.startswith("- ") and raw[:1] in " \t-" and current is not None:
            try:
                data.setdefault(current, []).append(parse_value(item[2:]))
            except ValueError as exc:
                problems.append(Problem(number, f"item da lista `{current}` inválido: {exc}"))
            continue
        match = _KEY.match(line)
        if match is None or raw[:1] in " \t":
            problems.append(Problem(number, f"linha do cabeçalho não reconhecida: {raw.strip()!r}"))
            continue
        key, value = match.group(1), match.group(2).strip()
        current = key
        lines_of[key] = number
        if value:
            try:
                data[key] = parse_value(value)
            except ValueError as exc:
                problems.append(Problem(number, f"valor de `{key}` inválido: {exc}"))
        else:
            data[key] = []
    return data, lines_of, problems


def _param(raw: Any, line: int, problems: list[Problem]) -> Param | None:
    if not isinstance(raw, dict):
        problems.append(Problem(line, "cada parâmetro precisa ser `{nome: …, tipo: …}`"))
        return None
    name, kind = str(raw.get("nome") or ""), str(raw.get("tipo") or "")
    if not _NAME.match(name):
        problems.append(
            Problem(
                line,
                f"nome de parâmetro inválido: {name!r}",
                "use letras minúsculas, números e `_`",
            )
        )
        return None
    if kind not in PARAM_TYPES:
        problems.append(
            Problem(
                line,
                f"tipo de parâmetro inválido em `{name}`: {kind!r}",
                f"tipos: {', '.join(PARAM_TYPES)}",
            )
        )
        return None
    options = tuple(str(o) for o in raw.get("opcoes") or [])
    if kind == "escolha" and not options:
        problems.append(Problem(line, f"o parâmetro `{name}` é `escolha` e precisa de `opcoes`"))
        return None
    default = raw.get("padrao")
    return Param(
        name=name,
        type=kind,
        label=str(raw.get("rotulo") or name),
        required=bool(raw.get("obrigatorio", default is None)),
        default=default if isinstance(default, (str, int, float)) else None,
        minimum=float(raw["minimo"]) if isinstance(raw.get("minimo"), (int, float)) else None,
        maximum=float(raw["maximo"]) if isinstance(raw.get("maximo"), (int, float)) else None,
        options=options,
    )


def parse_template(text: str, *, source: str = USER) -> Template:
    """Lê e valida a **estrutura** (cabeçalho). Os marcadores do corpo são conferidos pelo motor
    (`engine.check_template`), que conhece os blocos."""
    head, body, body_line = split_document(text)
    data, lines_of, problems = parse_header(head)
    for key in ("id", "titulo", "descricao"):
        if not str(data.get(key) or "").strip():
            problems.append(Problem(lines_of.get(key, 1), f"falta `{key}` no cabeçalho"))
    template_id = str(data.get("id") or "")
    if template_id and not _ID.match(template_id):
        problems.append(
            Problem(
                lines_of.get("id", 1),
                f"`id` inválido: {template_id!r}",
                "use minúsculas, números e `-`",
            )
        )
    status = str(data.get("status") or "rascunho")
    if status not in STATUSES:
        problems.append(
            Problem(
                lines_of.get("status", 1),
                f"`status` inválido: {status!r}",
                f"use {', '.join(STATUSES)}",
            )
        )
    raw_response = data.get("resposta")
    response: dict[str, Any] = (
        raw_response if isinstance(raw_response, dict) else {"formato": "livre"}
    )
    if response.get("formato", "livre") not in RESPONSE_FORMATS:
        problems.append(
            Problem(
                lines_of.get("resposta", 1),
                f"`resposta.formato` inválido: {response.get('formato')!r}",
                "use `livre` ou `json`",
            )
        )
    limit = data.get("limite_palavras", DEFAULT_WORD_LIMIT)
    if not isinstance(limit, int) or not 100 <= limit <= MAX_WORD_LIMIT:
        problems.append(
            Problem(
                lines_of.get("limite_palavras", 1),
                f"`limite_palavras` deve ficar entre 100 e {MAX_WORD_LIMIT}",
            )
        )
        limit = DEFAULT_WORD_LIMIT
    version = data.get("versao", 1)
    if not isinstance(version, int) or version < 1:
        problems.append(Problem(lines_of.get("versao", 1), "`versao` deve ser um inteiro ≥ 1"))
        version = 1
    params: list[Param] = []
    seen: set[str] = set()
    for raw in data.get("parametros") or []:
        parsed = _param(raw, lines_of.get("parametros", 1), problems)
        if parsed is None:
            continue
        if parsed.name in seen:
            problems.append(
                Problem(lines_of.get("parametros", 1), f"parâmetro repetido: `{parsed.name}`")
            )
        seen.add(parsed.name)
        params.append(parsed)
    if not body.strip():
        problems.append(Problem(body_line, "o corpo do modelo está vazio"))
    if problems:
        raise TemplateError(problems)
    known = {
        "id",
        "titulo",
        "descricao",
        "versao",
        "status",
        "autor",
        "parametros",
        "resposta",
        "limite_palavras",
        "revisado_por",
        "revisado_em",
    }
    return Template(
        id=template_id,
        title=str(data["titulo"]).strip(),
        description=str(data["descricao"]).strip(),
        version=int(version),
        status=status,
        author=str(data.get("autor") or "desconhecido"),
        params=tuple(params),
        response=dict(response),
        word_limit=int(limit),
        body=body,
        body_line=body_line,
        source=source,
        reviewed_by=str(data["revisado_por"]) if data.get("revisado_por") else None,
        reviewed_at=str(data["revisado_em"]) if data.get("revisado_em") else None,
        extra={k: v for k, v in data.items() if k not in known},
    )


# --------------------------------------------------------------------------- marcadores


@dataclass(frozen=True, slots=True)
class Marker:
    start: int
    end: int
    line: int
    kind: str  # param | block
    name: str
    args: tuple[tuple[str, tuple[str, str]], ...] = ()  # (chave, ("param"|"literal", valor))


def _tokens(text: str) -> list[str]:
    """Divide em espaços fora de aspas e de `{{ }}`."""
    tokens: list[str] = []
    buf: list[str] = []
    quote, depth, i = "", 0, 0
    while i < len(text):
        char = text[i]
        if quote:
            buf.append(char)
            quote = "" if char == quote else quote
        elif char in "\"'" and (not buf or buf[-1] == "="):
            quote = char
            buf.append(char)
        elif text.startswith("{{", i):
            depth += 1
            buf.append("{{")
            i += 1
        elif text.startswith("}}", i):
            depth -= 1
            buf.append("}}")
            i += 1
        elif char.isspace() and depth == 0:
            if buf:
                tokens.append("".join(buf))
                buf = []
        else:
            buf.append(char)
        i += 1
    if buf:
        tokens.append("".join(buf))
    return tokens


def _parse_arg(raw: str) -> tuple[str, str]:
    if raw.startswith("{{") and raw.endswith("}}"):
        return ("param", raw[2:-2].strip())
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return ("literal", raw[1:-1])
    return ("literal", raw)


def scan_markers(body: str, first_line: int = 1) -> tuple[list[Marker], list[Problem]]:
    markers: list[Marker] = []
    problems: list[Problem] = []
    i = 0
    while True:
        start = body.find("{{", i)
        if start < 0:
            break
        line = first_line + body.count("\n", 0, start)
        depth, j = 1, start + 2
        while j < len(body) and depth > 0:
            if body.startswith("{{", j):
                depth += 1
                j += 2
            elif body.startswith("}}", j):
                depth -= 1
                j += 2
            else:
                j += 1
        if depth != 0:
            problems.append(Problem(line, "marcador `{{` sem o `}}` correspondente"))
            break
        content = body[start + 2 : j - 2].strip()
        i = j
        if content.startswith("bloco:"):
            tokens = _tokens(content[len("bloco:") :].strip())
            if not tokens or not _NAME.match(tokens[0]):
                problems.append(
                    Problem(
                        line,
                        f"marcador de bloco inválido: {{{{{content}}}}}",
                        "use `{{bloco:nome chave=valor}}`",
                    )
                )
                continue
            args: list[tuple[str, tuple[str, str]]] = []
            for token in tokens[1:]:
                key, sep, value = token.partition("=")
                if not sep or not _NAME.match(key):
                    problems.append(
                        Problem(
                            line,
                            f"argumento inválido em `{tokens[0]}`: {token!r}",
                            "use `chave=valor`",
                        )
                    )
                    continue
                args.append((key, _parse_arg(value)))
            markers.append(Marker(start, j, line, "block", tokens[0], tuple(args)))
        elif _NAME.match(content):
            markers.append(Marker(start, j, line, "param", content))
        else:
            problems.append(
                Problem(
                    line,
                    f"marcador inválido: {{{{{content}}}}}",
                    "só `{{parametro}}` e `{{bloco:nome chave=valor}}`",
                )
            )
    return markers, problems
