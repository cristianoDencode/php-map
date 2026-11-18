"""Motor dos briefings: valida, monta e guarda os modelos.

`render` monta o briefing em **uma passada** sobre o corpo do modelo: cada marcador é trocado pelo
valor do parâmetro (escapado) ou pelo Markdown do bloco. A saída nunca é relida como marcação, então
um valor digitado com `{{bloco:…}}` aparece como texto e **não** executa nada. Mesmo mapa e mesmos
parâmetros dão o mesmo Markdown (a única data é a do mapa).
"""

import difflib
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from php_map import config
from php_map.briefing import blocks, resolve
from php_map.briefing.model import (
    GRAPH_TYPES,
    OFFICIAL,
    USER,
    Briefing,
    BriefingError,
    Param,
    Problem,
    Template,
    TemplateError,
)
from php_map.briefing.parse import Marker, parse_template, scan_markers
from php_map.errors import PhpMapError
from php_map.secret_scan import mask, scan_text

MODELS_ENV = "PHP_MAP_MODELS_DIR"
DEFAULT_MODELS_DIR = config.PROJECT_ROOT / "modelos"
_WORDS = re.compile(r"\S+")
_CUT_NOTE = "\n\n_[briefing cortado: passou do limite de {limit} palavras do modelo]_\n"


def words(text: str) -> int:
    return len(_WORDS.findall(text))


# ------------------------------------------------------------------ modelos oficiais e do usuário


def official_ids() -> list[str]:
    folder = resources.files("php_map.briefing").joinpath("models")
    return sorted(
        Path(str(item.name)).stem for item in folder.iterdir() if item.name.endswith(".md")
    )


def official_text(template_id: str) -> str:
    return (
        resources.files("php_map.briefing")
        .joinpath("models", f"{template_id}.md")
        .read_text(encoding="utf-8")
    )


def models_dir() -> Path:
    override = os.environ.get(MODELS_ENV, "").strip()
    return Path(override) if override else DEFAULT_MODELS_DIR


def load_official(template_id: str) -> Template:
    return parse_template(official_text(template_id), source=OFFICIAL)


def catalog(directory: Path | None = None) -> list[Template]:
    """Os modelos oficiais e os do usuário (`modelos/*.md`); arquivo inválido é ignorado."""
    found = [load_official(i) for i in official_ids()]
    folder = directory if directory is not None else models_dir()
    taken = {t.id for t in found}
    if folder.is_dir():
        for path in sorted(folder.glob("*.md")):
            try:
                template = parse_template(path.read_text(encoding="utf-8"), source=USER)
            except (TemplateError, OSError):
                continue
            if template.id not in taken and template.status != "oficial":
                found.append(template)
    return found


def get_template(template_id: str, directory: Path | None = None) -> Template:
    for template in catalog(directory):
        if template.id == template_id:
            return template
    raise BriefingError(f"modelo não encontrado: {template_id}")


# --------------------------------------------------------------------------- validação


def validate_markers(template: Template, markers: list[Marker]) -> list[Problem]:
    problems: list[Problem] = []
    used: set[str] = set()
    names = sorted(blocks.BLOCKS)
    for m in markers:
        if m.kind == "param":
            if template.param(m.name) is None:
                problems.append(
                    Problem(m.line, f"o parâmetro `{m.name}` não foi declarado no cabeçalho")
                )
            used.add(m.name)
            continue
        found = blocks.BLOCKS.get(m.name)
        if found is None:
            close = difflib.get_close_matches(m.name, names, n=1)
            problems.append(
                Problem(
                    m.line,
                    f"o bloco `{m.name}` não existe",
                    f"você quis dizer `{close[0]}`?" if close else f"blocos: {', '.join(names)}",
                )
            )
            continue
        given = {key for key, _ in m.args}
        for key, (kind, value) in m.args:
            if found.param(key) is None:
                known = ", ".join(p.name for p in found.params) or "nenhum"
                problems.append(
                    Problem(
                        m.line,
                        f"o bloco `{m.name}` não tem o argumento `{key}`",
                        f"argumentos: {known}",
                    )
                )
            if kind == "param":
                used.add(value)
                if template.param(value) is None:
                    problems.append(
                        Problem(
                            m.line, f"o parâmetro `{value}` (no bloco `{m.name}`) não foi declarado"
                        )
                    )
        for param in found.params:
            if param.required and param.name not in given:
                problems.append(
                    Problem(m.line, f"falta o argumento `{param.name}` no bloco `{m.name}`")
                )
    for declared in template.params:
        if declared.name not in used:
            problems.append(
                Problem(0, f"o parâmetro `{declared.name}` foi declarado e nunca é usado no corpo")
            )
    return problems


def check_template(text: str, *, source: str = USER) -> tuple[Template | None, list[Problem]]:
    """Lê e valida o modelo inteiro (cabeçalho e marcadores), com todos os problemas."""
    try:
        template = parse_template(text, source=source)
    except TemplateError as exc:
        return None, exc.problems
    markers, problems = scan_markers(template.body, template.body_line)
    problems += validate_markers(template, markers)
    return template, problems


# --------------------------------------------------------------------------- parâmetros


def _number(param: Param, value: Any, kind: str) -> int | float:
    try:
        number = float(str(value).replace(",", "."))
    except ValueError as exc:
        raise BriefingError(
            f"{kind} `{param.name}` precisa ser um número (recebi {value!r})"
        ) from exc
    if param.minimum is not None and number < param.minimum:
        raise BriefingError(f"`{param.name}` deve ser ≥ {param.minimum:g}")
    if param.maximum is not None and number > param.maximum:
        raise BriefingError(f"`{param.name}` deve ser ≤ {param.maximum:g}")
    return int(number) if number == int(number) else number


def coerce(ctx: blocks.BriefingContext, param: Param, raw: Any) -> Any:
    """Valida o valor de um parâmetro do modelo (tipo, intervalo, existência no mapa)."""
    value = raw if raw is not None and str(raw).strip() != "" else param.default
    if value is None or str(value).strip() == "":
        if param.required:
            raise BriefingError(f"falta o parâmetro `{param.name}` ({param.label})")
        return None
    if param.type == "numero":
        return _number(param, value, "o parâmetro")
    if param.type == "escolha":
        if str(value) not in param.options:
            raise BriefingError(f"`{param.name}` aceita: {', '.join(param.options)}")
        return str(value)
    if param.type in GRAPH_TYPES:
        try:
            resolve.resolver(param.type)(ctx.conn, str(value))
        except BriefingError as exc:
            raise BriefingError(f"{param.label}: {exc}") from exc
        return str(value).strip()
    return str(value)


def _block_arg(ctx: blocks.BriefingContext, block_param: blocks.BlockParam, value: Any) -> Any:
    if value is None or str(value).strip() == "":
        value = block_param.default
    if value is None:
        return None
    kind = block_param.type
    if kind == "numero":
        param = Param(block_param.name, "numero", block_param.name, minimum=None, maximum=None)
        return _number(param, value, "o argumento")
    if kind == "escolha":
        if block_param.options and str(value) not in block_param.options:
            raise BriefingError(f"`{block_param.name}` aceita: {', '.join(block_param.options)}")
        return str(value)
    if kind in GRAPH_TYPES and kind != "tela":
        return resolve.resolver(kind)(ctx.conn, str(value))
    return str(value)


# --------------------------------------------------------------------------- montagem


def render(template: Template, params: Mapping[str, Any], ctx: blocks.BriefingContext) -> Briefing:
    """Monta o briefing. `BriefingError` com mensagem em português se algo estiver errado."""
    markers, problems = scan_markers(template.body, template.body_line)
    problems += validate_markers(template, markers)
    if problems:
        raise TemplateError(problems)
    values: dict[str, Any] = {}
    for param in template.params:
        values[param.name] = coerce(ctx, param, params.get(param.name))
    pieces: list[str] = []
    used: list[str] = []
    warnings: list[str] = []
    position = 0
    for marker in markers:
        pieces.append(template.body[position : marker.start])
        position = marker.end
        if marker.kind == "param":
            pieces.append(
                blocks.escape_markup(
                    "" if values[marker.name] is None else str(values[marker.name])
                )
            )
            continue
        found = blocks.BLOCKS[marker.name]
        args: dict[str, Any] = {}
        for key, (kind, value) in marker.args:
            raw = values[value] if kind == "param" else value
            block_param = found.param(key)
            assert block_param is not None
            args[key] = _block_arg(ctx, block_param, raw)
        for block_param in found.params:
            if block_param.name not in args:
                args[block_param.name] = block_param.default
        reason = found.check(ctx) if found.check else None
        if reason is not None:
            result = blocks._unavailable(reason)
        else:
            try:
                result = found.render(ctx, args)
            except BriefingError:
                raise
            except PhpMapError as exc:
                raise BriefingError(f"bloco `{marker.name}`: {exc}") from exc
        used.append(marker.name)
        warnings += [f"{marker.name}: {w}" for w in result.warnings]
        pieces.append(result.markdown.rstrip("\n"))
    pieces.append(template.body[position:])
    text = "".join(pieces).strip() + "\n"
    header = (
        f"> Briefing gerado pelo php-map · modelo `{template.id}` v{template.version}"
        f" · mapa de {ctx.scanned_at or 'data desconhecida'}\n"
        f"> Blocos usados: {', '.join(dict.fromkeys(used)) or 'nenhum'}\n\n"
    )
    text = header + text
    if words(text) > template.word_limit:
        text = _cut(text, template.word_limit)
        warnings.append(
            f"o briefing passou do limite de {template.word_limit} palavras e foi cortado"
        )
    hits = list(scan_text(text))
    if hits:
        pattern, value = hits[0]
        raise BriefingError(
            f"o briefing contém um segredo aparente (padrão {pattern}, valor {mask(value)});"
            " não foi liberado"
        )
    return Briefing(text, words(text), tuple(dict.fromkeys(used)), tuple(warnings), ctx.scanned_at)


def _cut(text: str, limit: int) -> str:
    """Corta no último parágrafo inteiro que cabe no limite."""
    kept: list[str] = []
    total = 0
    for paragraph in text.split("\n\n"):
        size = words(paragraph)
        if total + size > limit:
            break
        kept.append(paragraph)
        total += size
    return "\n\n".join(kept).rstrip() + _CUT_NOTE.format(limit=limit)


# --------------------------------------------------------------------------- modelos do usuário


class ModelStore:
    """Os modelos do usuário em `modelos/<id>.md` (versionados no git). Os oficiais ficam no
    pacote e **não** se editam nem se apagam por aqui."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory if directory is not None else models_dir()

    def path(self, template_id: str) -> Path:
        return self.directory / f"{template_id}.md"

    def save(self, text: str) -> Template:
        """Valida e grava como `rascunho` (um modelo importado nunca entra como `oficial`)."""
        template, problems = check_template(text)
        if problems or template is None:
            raise TemplateError(problems)
        if template.id in official_ids():
            raise BriefingError(f"o id `{template.id}` é de um modelo oficial: escolha outro")
        status = "rascunho"
        text = re.sub(r"(?m)^status:.*$", f"status: {status}", text, count=1)
        if not re.search(r"(?m)^status:", text):
            text = text.replace("---\n", f"---\nstatus: {status}\n", 1)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._write(self.path(template.id), text)
        return get_template(template.id, self.directory)

    def mark_reviewed(self, template_id: str, name: str) -> Template:
        who = " ".join(name.split())
        if not who:
            raise BriefingError("informe o nome de quem revisou")
        template = self._user_template(template_id)
        text = self.path(template_id).read_text(encoding="utf-8")
        text = re.sub(r"(?m)^(status|revisado_por|revisado_em):.*\n", "", text)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        text = text.replace(
            "---\n", f"---\nstatus: revisado\nrevisado_por: {who}\nrevisado_em: {stamp}\n", 1
        )
        self._write(self.path(template.id), text)
        return self._user_template(template_id)

    def delete(self, template_id: str) -> None:
        self._user_template(template_id)
        self.path(template_id).unlink()

    def text(self, template_id: str) -> str:
        """O texto do arquivo (modelo do usuário), para editar."""
        self._user_template(template_id)
        return self.path(template_id).read_text(encoding="utf-8")

    def _user_template(self, template_id: str) -> Template:
        if template_id in official_ids():
            raise BriefingError("modelo oficial não se edita nem se apaga: duplique para mexer")
        if not self.path(template_id).is_file():
            raise BriefingError(f"modelo não encontrado: {template_id}")
        return get_template(template_id, self.directory)

    @staticmethod
    def _write(path: Path, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".modelo-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text if text.endswith("\n") else text + "\n")
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
