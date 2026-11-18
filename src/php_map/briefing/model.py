"""Tipos dos modelos de briefing para IA.

Um modelo é um arquivo Markdown com um cabeçalho restrito (`parametros`, `resposta`…) e um corpo
com instruções e **marcadores** `{{parametro}}` e `{{bloco:<nome> chave=valor}}`. O sistema
preenche os blocos com o que o mapa sabe, sem IA. Modelo é **dado, não código**: a marcação é
mínima e fechada, então um modelo vindo de uma IA não executa nada.
"""

from dataclasses import dataclass, field
from typing import Any

from php_map.errors import PhpMapError

PARAM_TYPES = (
    "funcao",
    "tela",
    "modulo",
    "tabela",
    "coluna",
    "endpoint",
    "texto",
    "numero",
    "escolha",
)
GRAPH_TYPES = frozenset({"funcao", "tela", "modulo", "tabela", "coluna", "endpoint"})
STATUSES = ("oficial", "rascunho", "revisado")
RESPONSE_FORMATS = ("livre", "json")
DEFAULT_WORD_LIMIT = 60_000
MAX_WORD_LIMIT = 200_000
OFFICIAL, USER = "official", "user"


class BriefingError(PhpMapError):
    """Erro de uso (mensagem em português para quem monta o briefing)."""


@dataclass(frozen=True, slots=True)
class Problem:
    """Um problema de validação do modelo, com a linha do arquivo e, se houver, a sugestão."""

    line: int
    message: str
    suggestion: str | None = None

    def text(self) -> str:
        where = f"linha {self.line}: " if self.line else ""
        hint = f" ({self.suggestion})" if self.suggestion else ""
        return f"{where}{self.message}{hint}"


class TemplateError(BriefingError):
    """O modelo tem problemas; `problems` traz todos, para a pessoa corrigir de uma vez."""

    def __init__(self, problems: list[Problem]) -> None:
        super().__init__("; ".join(p.text() for p in problems[:5]))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    type: str
    label: str
    required: bool = True
    default: str | int | float | None = None
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Template:
    id: str
    title: str
    description: str
    version: int
    status: str
    author: str
    params: tuple[Param, ...]
    response: dict[str, Any]
    word_limit: int
    body: str
    body_line: int  # linha do arquivo em que o corpo começa (1-based)
    source: str = USER  # official | user
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def param(self, name: str) -> Param | None:
        return next((p for p in self.params if p.name == name), None)


@dataclass(frozen=True, slots=True)
class Briefing:
    """O briefing montado, pronto para copiar ou baixar."""

    markdown: str
    words: int
    blocks: tuple[str, ...]
    warnings: tuple[str, ...]
    scanned_at: str
