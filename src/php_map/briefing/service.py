"""Camada de serviço dos briefings: o que a API da interface e o MCP chamam.

Funções síncronas e sem estado: abrem o banco somente leitura, montam e fecham. Os dicionários
devolvidos são serializáveis (JSON) e em português nas mensagens.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from php_map.briefing import blocks, engine
from php_map.briefing.model import Briefing, Problem, Template


def param_info(param: Any) -> dict[str, Any]:
    return {
        "name": param.name,
        "type": param.type,
        "label": param.label,
        "required": param.required,
        "default": None if param.default is None else str(param.default),
        "minimum": param.minimum,
        "maximum": param.maximum,
        "options": list(param.options),
    }


def template_info(template: Template) -> dict[str, Any]:
    return {
        "id": template.id,
        "title": template.title,
        "description": template.description,
        "version": template.version,
        "status": template.status,
        "author": template.author,
        "source": template.source,
        "params": [param_info(p) for p in template.params],
        "response_format": str(template.response.get("formato", "livre")),
        "word_limit": template.word_limit,
        "reviewed_by": template.reviewed_by,
        "reviewed_at": template.reviewed_at,
    }


def list_templates(directory: Path | None = None) -> list[dict[str, Any]]:
    return [template_info(t) for t in engine.catalog(directory)]


def template_text(template_id: str, directory: Path | None = None) -> str:
    """O arquivo inteiro do modelo (oficial ou do usuário), para ler ou editar."""
    template = engine.get_template(template_id, directory)
    if template.source == "official":
        return engine.official_text(template_id)
    return engine.ModelStore(directory).text(template_id)


def problem_info(problem: Problem) -> dict[str, Any]:
    return {"line": problem.line, "message": problem.message, "suggestion": problem.suggestion}


def check(text: str) -> dict[str, Any]:
    """Valida um texto de modelo sem gravar nada."""
    template, problems = engine.check_template(text)
    return {
        "ok": not problems and template is not None,
        "problems": [problem_info(p) for p in problems],
        "template": template_info(template) if template is not None and not problems else None,
    }


def save(text: str, directory: Path | None = None) -> dict[str, Any]:
    """Valida e grava como rascunho; com problemas, não grava e devolve a lista."""
    result = check(text)
    if not result["ok"]:
        return result
    saved = engine.ModelStore(directory).save(text)
    return {"ok": True, "problems": [], "template": template_info(saved)}


def render(
    dbs: Sequence[Path],
    template_id: str,
    params: Mapping[str, Any],
    directory: Path | None = None,
) -> Briefing:
    template = engine.get_template(template_id, directory)
    with blocks.BriefingContext(list(dbs)) as ctx:
        return engine.render(template, params, ctx)


def briefing_info(briefing: Briefing) -> dict[str, Any]:
    return {
        "markdown": briefing.markdown,
        "words": briefing.words,
        "blocks": list(briefing.blocks),
        "warnings": list(briefing.warnings),
        "scanned_at": briefing.scanned_at,
    }


def block_catalog(dbs: Sequence[Path]) -> list[dict[str, Any]]:
    with blocks.BriefingContext(list(dbs)) as ctx:
        return blocks.catalog(ctx)


# --------------------------------------------------------------------------- MCP


def mcp_templates(directory: Path | None = None) -> dict[str, Any]:
    """`briefing_templates`: os modelos com os parâmetros (para a IA escolher e preencher)."""
    return {"templates": list_templates(directory), "blocks": blocks.catalog(None)}


def mcp_briefing(
    catalog: Any,
    project: str,
    template: str,
    params: Mapping[str, Any],
    directory: Path | None = None,
) -> dict[str, Any]:
    """`briefing`: monta o Markdown do modelo para o projeto (com os outros projetos do catálogo,
    para o plano de corte cruzado)."""
    target = catalog.get(project)
    dbs = [target.db, *[p.db for p in catalog.projects if p.slug != project]]
    return briefing_info(render(dbs, template, params, directory))
