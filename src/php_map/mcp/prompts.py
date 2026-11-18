"""Prompts MCP de relatório.

Um **prompt** é um roteiro em texto: ao ser pedido (`prompts/get`), o servidor monta a
instrução que leva a IA a chamar as ferramentas certas, na ordem certa e com paginação, e a
responder no formato que o projeto já define em outro lugar. **Nenhum prompt chama ferramenta
nenhuma ao ser montado** — o servidor nunca lê o grafo aqui; quem chama as ferramentas é a IA
que recebe o roteiro.

Três prompts:
- `relatorio-analise-profunda`: roteiro extraído de `mcp/modelo-analise-profunda.md` (só o
  bloco de formato e as regras 1 e 4 — as regras 2/3 e o protocolo `SITUAÇÃO:`/`PEDIDO:` não
  valem no MCP). `extract_report_format` lê o `.md`
  **por cabeçalho**, em tempo de execução: se o modelo mudar de estrutura, a extração levanta
  `ReportFormatError` (e o teste de `test_mcp_prompts.py` quebra).
- `buscar-por-assunto`: roteiro sobre `formula_search`/`routine_rules(topic=…)`.
- `briefing-<id>`, um por modelo `oficial` de `briefing/models/*.md`: como o SDK `mcp` 2.3.0
  deriva os argumentos da **assinatura** da função (`Prompt.from_function` → `func_metadata`),
  e os parâmetros de um modelo só se sabem em tempo de execução (vêm do frontmatter), estes
  prompts **não** usam o decorador `@server.prompt()`: são `Prompt(...)` montados à mão, com
  `arguments` explícito (`PromptArgument` por parâmetro) e uma função `**kwargs` como `fn` — a
  validação de argumento obrigatório ausente já é feita por `Prompt.render` a partir de
  `self.arguments`, não pela assinatura de `fn` (conferido no código do SDK:
  `mcp/server/mcpserver/prompts/base.py`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from importlib import resources
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.prompts.base import Prompt, PromptArgument
from pydantic import Field

from php_map.briefing import engine as briefing_engine
from php_map.briefing.model import Template

PROJETO_DESCRIPTION = "Slug do projeto (veja list_projects)."

# ---------------------------------------------------------------------- modelo-analise-profunda.md

_FENCE_RE = re.compile(r"```\n(.*?)\n```", re.DOTALL)
_RULE_RE = re.compile(r"(\d+)\.\s+(.*)", re.DOTALL)
_FORMATO_MARKER = "RESUMO DE NEGÓCIO"
_WRITE_WHAT_MARKER = "O que escrever em cada seção do detalhe técnico:"
_RULES_MARKER = "\nRegras:"
# As regras 2 e 3 (PEDIDO:/PERGUNTA:) não valem no MCP.
_RULE_NUMBERS = ("1", "4")


class ReportFormatError(ValueError):
    """`modelo-analise-profunda.md` não tem a estrutura esperada (cabeçalhos procurados)."""


def extract_report_format(text: str) -> tuple[str, str, str]:
    """Extrai, por cabeçalho, do texto do `modelo-analise-profunda.md`: o bloco de formato (sem a
    linha `SITUAÇÃO: final`, que é do protocolo de duas vias), a seção «O que escrever em cada
    seção» e as regras 1 e 4 (nunca 2 e 3). Levanta `ReportFormatError` se algum marcador não for
    encontrado — é assim que a extração "quebra" se o modelo mudar de estrutura."""
    fence_match = _FENCE_RE.search(text)
    if fence_match is None:
        raise ReportFormatError("bloco de formato (```...```) não encontrado")
    fence = fence_match.group(1)
    if _FORMATO_MARKER not in fence:
        raise ReportFormatError(f"'{_FORMATO_MARKER}' não está no bloco de formato")
    formato = fence[fence.index(_FORMATO_MARKER) :].strip()

    if _WRITE_WHAT_MARKER not in text or _RULES_MARKER not in text:
        raise ReportFormatError("seção 'O que escrever...' ou 'Regras:' não encontrada")
    write_what = text[
        text.index(_WRITE_WHAT_MARKER) + len(_WRITE_WHAT_MARKER) : text.index(_RULES_MARKER)
    ].strip()

    rules_text = text[text.index(_RULES_MARKER) + len(_RULES_MARKER) :].strip()
    rules: dict[str, str] = {}
    for item in re.split(r"\n(?=\d+\.\s)", rules_text):
        match = _RULE_RE.match(item)
        if match:
            rules[match.group(1)] = match.group(2).strip()
    missing = [n for n in _RULE_NUMBERS if n not in rules]
    if missing:
        raise ReportFormatError(f"regra(s) {', '.join(missing)} não encontrada(s)")
    regras = "\n".join(f"{n}. {rules[n]}" for n in _RULE_NUMBERS)
    return formato, write_what, regras


def read_modelo_analise_profunda() -> str:
    return (
        resources.files("php_map.mcp")
        .joinpath("modelo-analise-profunda.md")
        .read_text(encoding="utf-8")
    )


# --------------------------------------------------------------------------- roteiros fixos


def relatorio_analise_profunda_text(
    projeto: str, alvo: str, tipo: str, *, formato: str, write_what: str, regras: str
) -> str:
    tipo_txt = tipo.strip() or "vazio: decida pela forma do alvo abaixo"
    return f"""Monte, para um analista de negócio que não lê código, o relatório de análise
profunda do item abaixo. Use **só** o que as ferramentas do mapa devolverem: nunca invente
fórmula, menu, tabela nem campo.

Projeto: {projeto}
Alvo: {alvo}
Tipo: {tipo_txt}

Passos:
1. Se o tipo for `termo` (ou o alvo for um termo de negócio, sem nome técnico de função, tabela
   ou campo): chame `field_search(project="{projeto}", term="{alvo}")`, escolha a coluna certa
   entre os candidatos e siga a partir daqui como `campo` (schema.tabela.coluna).
2. Chame `formulas(project="{projeto}", ...)` com o parâmetro do tipo (`routine=` para função,
   `field=` para campo, `table=` para tabela, `menu=` para menu) e o alvo (ou a coluna escolhida
   no passo 1). **Siga `next_page` até ele vir `null`** (todas as páginas, nenhuma fórmula,
   gravador ou leitor fica de fora) — ou use `sections` para pular direto à seção que interessa.
3. Para cada função auxiliar relevante que aparecer em "Funções auxiliares", chame de novo
   `formulas(project="{projeto}", routine=<nome da auxiliar>)` e traga o resultado para dentro
   do relatório.
4. Se a resposta de `formulas` trouxer `resolved` (nome que só repassa entre sobrecargas da
   mesma família), cite a cadeia (`resolved.chain`) no relatório: diga de qual
   assinatura veio o cálculo de verdade.

Responda só com o relatório, no formato abaixo (extraído do modelo do mapeador):

```
{formato}
```

O que escrever em cada seção do detalhe técnico:

{write_what}

Regras:

{regras}
"""


def buscar_por_assunto_text(projeto: str, assunto_ou_termo: str, modulo: str) -> str:
    modulo_txt = modulo.strip()
    modulo_clause = f', module="{modulo_txt}"' if modulo_txt else ""
    return f"""Responda, só com o que as ferramentas do mapa devolverem, onde se calcula e quais
regras valem para "{assunto_ou_termo}" no projeto {projeto}. Nunca invente função, fórmula nem
regra que as ferramentas não tenham devolvido.

Passos:
1. Chame `formula_search(project="{projeto}", topic="{assunto_ou_termo}"{modulo_clause})` (o
   assunto, pela chave ou pelo rótulo em português) **e**
   `formula_search(project="{projeto}", query="{assunto_ou_termo}"{modulo_clause})` (o termo
   livre): junte os dois resultados, sem repetir função.
2. Chame `routine_rules(project="{projeto}", topic="{assunto_ou_termo}")` para as regras de
   negócio (validação, erro, aviso) que falam do mesmo assunto.
3. Para as até 3 funções com mais ocorrências do passo 1, chame
   `formulas(project="{projeto}", routine=<nome da função>)` e confira a fórmula de verdade,
   com a linha.
4. Responda em três partes:
   - **Onde se calcula**: as funções dos passos 1 e 3, com a fórmula já confirmada.
   - **Quais regras**: o que o passo 2 trouxe, com a mensagem e a condição de cada regra.
   - **O que confirmar**: avise sempre que o **assunto é pista** (vem do nome do destino, da
     função chamada ou do rótulo do dicionário — nunca é fato do e-cidade) e diga o que não
     apareceu em nenhuma ferramenta.
"""


# --------------------------------------------------------------------------- briefing-<id>

_BRIEFING_HELPER_TOOLS = "`formulas`, `routine_card` ou `screen_card`"


def briefing_prompt_text(projeto: str, template_id: str, params: dict[str, str]) -> str:
    params_literal = ", ".join(f'"{k}": "{v}"' for k, v in params.items())
    return (
        f'Monte o briefing com `briefing(project="{projeto}", template="{template_id}", '
        f"params={{{params_literal}}})` e siga as instruções do Markdown que ele devolver: é o "
        "papel e a tarefa já prontos para quem vai usar o briefing, com o contexto do mapa "
        "embutido — sem IA. Se a resposta trouxer um aviso de corte (passou do limite de "
        f"palavras do modelo), complete só com {_BRIEFING_HELPER_TOOLS} — as mesmas ferramentas "
        "que o próprio bloco cortado indicar — e nunca invente o que não vier do mapa."
    )


def official_templates() -> list[Template]:
    """Os modelos de `briefing/models/*.md` com `status: oficial` (frontmatter)."""
    all_templates = (briefing_engine.load_official(i) for i in briefing_engine.official_ids())
    return [t for t in all_templates if t.status == "oficial"]


def _briefing_prompt_arguments(template: Template) -> list[PromptArgument]:
    arguments = [PromptArgument(name="projeto", description=PROJETO_DESCRIPTION, required=True)]
    for param in template.params:
        arguments.append(
            PromptArgument(name=param.name, description=param.label, required=param.required)
        )
    return arguments


def _briefing_prompt_fn(template_id: str, param_names: tuple[str, ...]) -> Callable[..., str]:
    def _fn(**kwargs: str) -> str:
        projeto = kwargs.get("projeto", "")
        params = {name: kwargs[name] for name in param_names if kwargs.get(name, "") != ""}
        return briefing_prompt_text(projeto, template_id, params)

    _fn.__name__ = f"briefing_{template_id.replace('-', '_')}"
    return _fn


def _register_briefing_prompts(server: MCPServer) -> None:
    for template in official_templates():
        param_names = tuple(p.name for p in template.params)
        prompt = Prompt(
            name=f"briefing-{template.id}",
            title=None,
            description=f"Roteiro: monta o briefing «{template.title}» ({template.description})",
            arguments=_briefing_prompt_arguments(template),
            fn=_briefing_prompt_fn(template.id, param_names),
            context_kwarg=None,
        )
        server.add_prompt(prompt)


# --------------------------------------------------------------------------- registro


def register_prompts(server: MCPServer) -> None:
    """Registra os prompts de relatório no servidor (chamado por `server.build_server`)."""
    formato, write_what, regras = extract_report_format(read_modelo_analise_profunda())

    @server.prompt(
        name="relatorio-analise-profunda",
        description=(
            "Roteiro: relatório de análise profunda (cálculos, dados de entrada, funções "
            "auxiliares) de uma função, campo, tabela, menu ou termo de negócio — segue as "
            "páginas do dossiê até o fim e responde no formato do mapeador."
        ),
    )
    def relatorio_analise_profunda(
        projeto: Annotated[str, Field(description=PROJETO_DESCRIPTION)],
        alvo: Annotated[
            str,
            Field(
                description=(
                    "Nome da função, schema.tabela.coluna, schema.tabela, id ou trecho do "
                    "menu, ou termo de negócio."
                )
            ),
        ],
        tipo: Annotated[
            str,
            Field(
                description=(
                    "funcao, campo, tabela, menu ou termo; vazio = decida pela forma do alvo."
                )
            ),
        ] = "",
    ) -> str:
        return relatorio_analise_profunda_text(
            projeto, alvo, tipo, formato=formato, write_what=write_what, regras=regras
        )

    @server.prompt(
        name="buscar-por-assunto",
        description=(
            "Roteiro: busca fórmulas e regras por assunto ou termo de negócio em toda a base "
            "e responde onde se calcula, quais regras valem e o que confirmar."
        ),
    )
    def buscar_por_assunto(
        projeto: Annotated[str, Field(description=PROJETO_DESCRIPTION)],
        assunto_ou_termo: Annotated[
            str, Field(description="Assunto (chave ou rótulo, ex.: multa) ou termo livre.")
        ],
        modulo: Annotated[str, Field(description="Limita ao módulo, ex.: caixa.")] = "",
    ) -> str:
        return buscar_por_assunto_text(projeto, assunto_ou_termo, modulo)

    _register_briefing_prompts(server)
