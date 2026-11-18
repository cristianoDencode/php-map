"""Servidor MCP somente leitura sobre o SDK oficial `mcp` 2.x (`MCPServer`).

As ferramentas são cascas assíncronas sobre `php_map.mcp.tools`: cada consulta (sqlite3,
síncrona) roda numa thread, com a conexão aberta dentro dela, e com limite de tempo (o
Cloudflare corta a resposta depois de cerca de 100 s). Erros do domínio viram erro de
ferramenta com a mensagem em português.
"""

import functools
import logging
from collections.abc import Callable, Sequence
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from php_map.analysis import layers as layers_module
from php_map.briefing import service as briefing_service
from php_map.errors import PhpMapError
from php_map.graph import formula_index as formula_index_module
from php_map.mcp import tools
from php_map.mcp.auth import ASGIApp, BearerAuth, TokenVerifier
from php_map.mcp.catalog import ProjectCatalog
from php_map.mcp.prompts import register_prompts
from php_map.report import cards

logger = logging.getLogger(__name__)

TOOL_TIMEOUT = 60.0  # segundos por chamada
SERVER_NAME = "php-map"
GUIDE_URI = "php-map://guia"

# Recursos lidos de `--docs-dir` (caminhos relativos a essa pasta).
DOCS: dict[str, tuple[str, str]] = {
    "ontologia": ("ontologia.md", "Nós, arestas, fontes e confiança do grafo"),
    "glossario": ("glossario-ecidade.md", "Termos do domínio (empenho, ISSQN, CGM…)"),
    "perfis": ("perfis.md", "Perfis de sistema (e-Cidade, Zend 1) e como criar um novo"),
    "precisao": ("precisao.md", "Quanto o grafo acerta e os limites conhecidos"),
    "mcp": ("mcp.md", "Ferramentas, recursos, prompts e configuração dos clientes"),
}


def default_docs_dir() -> Path | None:
    """`docs/` do repositório (instalação editável) ou a cópia empacotada no wheel."""
    from php_map.config import PROJECT_ROOT

    for candidate in (PROJECT_ROOT / "docs", Path(__file__).resolve().parents[1] / "docs"):
        if candidate.is_dir():
            return candidate
    return None


INSTRUCTIONS = (
    "Grafos de conhecimento, somente leitura, de sistemas legados em PHP (e-Cidade, Zend 1). "
    "Comece por list_projects e leia o recurso php-map://guia para interpretar source e "
    "confidence. "
    "Fluxo típico: search → trace/impact → communication/endpoint/cross_summary. Prompts de "
    "relatório: relatorio-analise-profunda, buscar-por-assunto e um briefing-<id> "
    "por modelo oficial de briefing — roteiros prontos que já chamam as ferramentas certas."
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

Project = Annotated[str, Field(description="Slug do projeto (veja list_projects).")]


async def _run(fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    call = functools.partial(fn, *args, **kwargs)
    try:
        with anyio.fail_after(TOOL_TIMEOUT):
            return await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
    except TimeoutError as exc:
        raise ToolError(
            f"consulta passou de {TOOL_TIMEOUT:.0f} s: reduza depth/max_nodes ou filtre mais"
        ) from exc
    except PhpMapError as exc:
        raise ToolError(str(exc)) from exc


def build_server(catalog: ProjectCatalog, *, docs_dir: Path | None = None) -> MCPServer:
    server: MCPServer = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)
    tool = functools.partial(server.tool, annotations=READ_ONLY)

    @tool(
        description="Projetos (sistemas mapeados) que este servidor consulta, com a data do mapa."
    )
    async def list_projects() -> dict[str, Any]:
        return await _run(tools.list_projects, catalog)

    @tool(
        description=(
            "Busca telas, tabelas, endpoints, rotas e itens de menu por termos de negócio "
            "(sem stemmer: 'anulação' também acha 'anula*')."
        )
    )
    async def search(
        query: Annotated[str, Field(description="Termos de negócio ou nomes técnicos.")],
        project: Project,
        label: Annotated[str | None, Field(description="Filtra o rótulo: File, Table…")] = None,
        module: Annotated[str | None, Field(description="Filtra o módulo.")] = None,
        limit: Annotated[int, Field(ge=1, le=tools.MAX_SEARCH)] = 20,
        any_term: Annotated[bool, Field(description="Basta um termo casar.")] = False,
    ) -> dict[str, Any]:
        return await _run(tools.search, catalog, query, project, label, module, limit, any_term)

    @tool(description="Detalhe de um nó: atributos, módulos e contagem de arestas por tipo.")
    async def node(
        node_id: Annotated[str, Field(description="ID completo, ex.: Table:public.cgm")],
        project: Project,
    ) -> dict[str, Any]:
        return await _run(tools.node, catalog, node_id, project)

    @tool(
        description="Arestas saindo (out) ou chegando (in) num nó, com fonte, confiança, evidência."
    )
    async def neighbors(
        node_id: Annotated[str, Field(description="ID completo do nó.")],
        project: Project,
        direction: Literal["out", "in"] = "out",
        edge_types: Annotated[
            list[str] | None, Field(description="Ex.: ['CALLS', 'QUERIES', 'INSERTS']")
        ] = None,
        min_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0,
        limit: Annotated[int, Field(ge=1, le=tools.MAX_NEIGHBORS)] = 50,
    ) -> dict[str, Any]:
        return await _run(
            tools.neighbors, catalog, node_id, project, direction, edge_types, min_confidence, limit
        )

    @tool(
        description=(
            "Fluxo de um arquivo de entrada (tela, RPC, controller) até o banco: nós, arestas e "
            "tabelas alcançadas. Use o caminho relativo do arquivo ou File:…"
        )
    )
    async def trace(
        entry: Annotated[str, Field(description="Ex.: emp4_anularempenho001.php")],
        project: Project,
        depth: Annotated[int, Field(ge=1, le=10)] = 8,
        min_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0,
        include_infra: bool = False,
        max_nodes: Annotated[int, Field(ge=1, le=tools.MAX_SUBGRAPH)] = 150,
        mermaid: Annotated[bool, Field(description="Inclui o diagrama Mermaid.")] = False,
    ) -> dict[str, Any]:
        return await _run(
            tools.trace,
            catalog,
            entry,
            project,
            depth,
            min_confidence,
            include_infra,
            max_nodes,
            mermaid,
        )

    @tool(
        description=(
            "Quem lê e quem escreve numa tabela, quem a usa pelo DAO (`dao_users`) e o que "
            "depende dela até as telas."
        )
    )
    async def impact(
        table: Annotated[str, Field(description="Nome da tabela (cgm) ou Table:schema.nome")],
        project: Project,
        schema: Annotated[str | None, Field(description="Schema, se o nome se repetir.")] = None,
        depth: Annotated[int, Field(ge=1, le=10)] = 10,
        max_nodes: Annotated[int, Field(ge=1, le=tools.MAX_SUBGRAPH)] = 150,
        mermaid: bool = False,
    ) -> dict[str, Any]:
        return await _run(tools.impact, catalog, table, project, schema, depth, max_nodes, mermaid)

    @tool(
        description=(
            "Superfície de comunicação do projeto: endpoints SOAP/RPC, rotas HTTP e chamadas "
            "para fora, com direção (entra/sai), parâmetros e o outro lado da costura. Paginado."
        )
    )
    async def communication(
        project: Project,
        system: str | None = None,
        protocol: Annotated[str | None, Field(description="soap, rpc, http ou externo")] = None,
        direction: Annotated[str | None, Field(description="entra ou sai")] = None,
        module: str | None = None,
        search: Annotated[str | None, Field(description="Texto no nome do endpoint.")] = None,
        limit: Annotated[int, Field(ge=1, le=tools.MAX_COMMUNICATION)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        return await _run(
            tools.communication,
            catalog,
            project,
            system,
            protocol,
            direction,
            module,
            search,
            limit,
            offset,
        )

    @tool(
        description=(
            "Detalhe de um endpoint ou rota: parâmetros, quem implementa, quem chama (arquivo e "
            "linha), o que alcança e o outro lado da costura entre projetos."
        )
    )
    async def endpoint(
        endpoint_id: Annotated[
            str, Field(description="Ex.: Endpoint:ecidade:soap:getDadosEmpresa")
        ],
        project: Project,
    ) -> dict[str, Any]:
        return await _run(tools.endpoint, catalog, endpoint_id, project)

    @tool(
        description=(
            "Cruzamento entre os projetos pelo ID do Endpoint: casados, chamados sem servidor e "
            "servidos sem cliente, dependências por módulo e tabelas alcançadas do outro lado."
        )
    )
    async def cross_summary(
        status: Annotated[
            str | None,
            Field(description="casado, chamado_sem_servidor ou servido_sem_cliente"),
        ] = None,
        protocol: str | None = None,
        limit: Annotated[int, Field(ge=1, le=tools.MAX_CROSS)] = 50,
        depth: Annotated[int, Field(ge=1, le=10)] = 3,
    ) -> dict[str, Any]:
        return await _run(tools.cross_summary, catalog, status, protocol, limit, depth)

    @tool(
        description=(
            "Trecho do código-fonte (com número das linhas), só se o servidor tiver a pasta do "
            "projeto. Arquivos de configuração e chaves são bloqueados."
        )
    )
    async def source(
        project: Project,
        path: Annotated[str, Field(description="Caminho relativo à raiz do projeto.")],
        line: Annotated[int | None, Field(ge=1)] = None,
        context: Annotated[int, Field(ge=1, le=tools.MAX_CONTEXT)] = 30,
    ) -> dict[str, Any]:
        return await _run(tools.source, catalog, project, path, line, context)

    @tool(
        description=(
            "Texto de uma função da base (o que ela faz), como o scan leu do banco, em páginas "
            "de linhas, com quem a usa e as tabelas que lê e grava. `routine` aceita o id "
            "completo, schema.nome ou só o nome; com várias assinaturas devolve os candidatos — "
            "nome com repasse resolve sozinho."
        )
    )
    async def routine(
        project: Project,
        routine: Annotated[str, Field(description="Ex.: fc_calcula ou public.lpad.")],
        offset: Annotated[int, Field(ge=1, description="Linha inicial (1 = CREATE).")] = 1,
        limit: Annotated[int, Field(ge=1, le=tools.MAX_ROUTINE_LINES)] = 200,
        rules_kind: Annotated[
            str | None,
            Field(description="validation, error, warning, diagnostic, decision, handler, comment"),
        ] = None,
        rules_level: Annotated[
            str | None, Field(description="EXCEPTION, WARNING, NOTICE, INFO, DEBUG ou LOG")
        ] = None,
    ) -> dict[str, Any]:
        return await _run(
            tools.routine, catalog, project, routine, offset, limit, rules_kind, rules_level
        )

    @tool(
        description=(
            "Regras de negócio em todas as funções da base: validações, erros, "
            "avisos, decisões, tratamento de exceção e comentários, por texto da mensagem/"
            "condição, nível, tipo e assunto. Responde por exemplo 'quais funções "
            "validam saldo?' ou 'quais regras falam de multa?' sem ler o texto de cada uma."
        )
    )
    async def routine_rules(
        project: Project,
        query: Annotated[str | None, Field(description="Texto da mensagem ou da condição")] = None,
        kind: Annotated[
            str | None,
            Field(description="validation, error, warning, diagnostic, decision, handler, comment"),
        ] = None,
        level: Annotated[
            str | None, Field(description="EXCEPTION, WARNING, NOTICE, INFO, DEBUG ou LOG")
        ] = None,
        topic: Annotated[
            str | None,
            Field(description="Chave ou rótulo do assunto (ex.: multa); exige index-formulas."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=tools.MAX_RULES)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        return await _run(
            tools.routine_rules, catalog, project, query, kind, level, topic, limit, offset
        )

    @tool(
        description=(
            "Funções que leem, gravam, filtram, juntam ou ordenam por uma coluna de uma tabela, "
            'com a linha e o texto da consulta — "campo → funções". Responde por '
            "exemplo 'quais funções gravam arrecant.k00_valor?'."
        )
    )
    async def column_usage(
        project: Project,
        table: Annotated[str, Field(description="Ex.: arrecant ou public.arrecant.")],
        column: Annotated[str, Field(description="Ex.: k00_valor.")],
        role: Annotated[str | None, Field(description="read, write, filter, join ou sort")] = None,
        limit: Annotated[int, Field(ge=1, le=tools.MAX_QUERIES)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        return await _run(tools.column_usage, catalog, project, table, column, role, limit, offset)

    @tool(
        description=(
            "Ficha em português de uma função da base (sem IA): para que serve, onde "
            "é usada, parâmetros e retorno, dados que usa (com rótulo do dicionário), o que "
            "valida, o que chama e quem a chama, texto original. Pronta para colar numa IA."
        )
    )
    async def routine_card(
        project: Project,
        routine: Annotated[str, Field(description="Ex.: fc_calcula ou public.fc_calcula.")],
        source_lines: Annotated[int, Field(ge=0, le=2000)] = cards.DEFAULT_SOURCE_LINES,
        format: Annotated[str, Field(description="markdown ou json")] = "markdown",
    ) -> dict[str, Any]:
        return await _run(tools.routine_card, catalog, project, routine, source_lines, format)

    @tool(
        description=(
            "Ficha em português de uma tela do menu (sem IA): menu, dados gravados e "
            "lidos com rótulo do dicionário, funções da base chamadas (com um resumo das "
            "validações de cada), mensagens de validação e erro, anotações do autor."
        )
    )
    async def screen_card(
        project: Project,
        entry: Annotated[str, Field(description="Ex.: emp4_anularempenho001.php.")],
        depth: Annotated[int, Field(ge=1, le=10)] = 4,
        format: Annotated[str, Field(description="markdown ou json")] = "markdown",
    ) -> dict[str, Any]:
        return await _run(tools.screen_card, catalog, project, entry, depth, format)

    @tool(
        description=(
            "'De onde vem este valor?' (sem IA), em duas chamadas: sem `targets`, "
            "interpreta a pergunta (termos x rótulos do dicionário, só dentro do alcance de "
            "`routine`) e devolve candidatos; com `targets` (um candidato ou 'schema.tabela."
            "coluna'), devolve a cadeia até a coluna base e até a tela que a cadastra (ou a "
            "hipótese pelo nome), num dossiê curto pronto para colar numa IA sem acesso ao mapa."
        )
    )
    async def value_origin(
        project: Project,
        question: Annotated[str, Field(description="Ex.: de onde vem o valor do m² do terreno?")],
        routine: Annotated[
            str | None, Field(description="Função da base para limitar o alcance (recomendado).")
        ] = None,
        targets: Annotated[
            list[str] | None,
            Field(description="Candidatos da 1ª chamada, ou 'schema.tabela.coluna'."),
        ] = None,
        max_words: Annotated[int, Field(ge=500, le=6000)] = 2000,
    ) -> dict[str, Any]:
        return await _run(
            tools.value_origin, catalog, project, question, routine, targets, max_words
        )

    @tool(
        description=(
            "Análise profunda de uma função da base (sem IA): cada cálculo com a "
            "fórmula e a linha, os dados de entrada levados até tabela.campo (com o nome do "
            "dicionário) e o menu onde cada um é cadastrado, as funções auxiliares chamadas "
            "dentro de uma fórmula (até `depth` níveis) e o que não foi possível rastrear. "
            "Com `field` (schema.tabela.coluna) em vez de `routine`: onde o campo é "
            "cadastrado, as funções que o gravam (fórmula do valor e entradas até o menu) e quem o "
            "usa. Com `menu` (id BusinessAction:menu:N ou trecho do caminho): o que o "
            "item de menu toca, as funções da base com a contagem de fórmulas e as tabelas que a "
            "tela grava. Com `table` (schema.tabela): os campos, onde a tabela é "
            "cadastrada e todo o código PHP que a grava e lê (SQL direto, DAO, Eloquent, "
            "migrações; o dossiê de `field` traz a mesma seção em `access`). Cada "
            "gravação pelo DAO traz os campos que o código PHP preenche (`fills`: atribuição ou "
            "formulário da tela; o valor é o texto do código). Devolve `markdown` "
            "(pronto para ler) e `dossier` (estruturado). "
            "O dossiê de "
            "função abre com a Visão geral (fórmulas por assunto e por função; assunto é pista) e "
            "traz `overview` e `suggestions` (perguntas sugeridas) na página 1. "
            "Nome com repasse resolve sozinho. "
            "Paginado: o dossiê inteiro é dividido em páginas de ~`max_words` "
            "palavras; siga `next_page`. `sections` diz em que página está cada seção. `dossier` "
            "(o JSON estruturado) só vem com `structured=true`."
        )
    )
    async def formulas(
        project: Project,
        routine: Annotated[
            str | None, Field(description="Nome, schema.nome ou id da função.")
        ] = None,
        depth: Annotated[int, Field(ge=0, le=4, description="Níveis de funções auxiliares.")] = 2,
        max_words: Annotated[
            int, Field(ge=300, le=8000, description="Palavras por página.")
        ] = 3000,
        field: Annotated[
            str | None,
            Field(description="Campo: schema.tabela.coluna (um de routine, field, menu ou table)."),
        ] = None,
        menu: Annotated[
            str | None,
            Field(
                description="Menu: id BusinessAction:menu:N ou trecho do caminho (um dos quatro)."
            ),
        ] = None,
        table: Annotated[
            str | None,
            Field(description="Tabela: schema.tabela (um dos quatro)."),
        ] = None,
        max_readers: Annotated[
            int, Field(ge=0, le=500, description="Leitores listados em table e field.")
        ] = 15,
        page: Annotated[
            int, Field(ge=1, description="Página do dossiê paginado; siga next_page.")
        ] = 1,
        structured: Annotated[
            bool, Field(description="Inclui `dossier` (JSON estruturado, igual em toda página).")
        ] = False,
    ) -> dict[str, Any]:
        return await _run(
            tools.formulas,
            catalog,
            project,
            routine,
            depth,
            max_words,
            field,
            menu,
            table,
            max_readers,
            page,
            structured,
        )

    @tool(
        description=(
            "Busca fórmulas por assunto e/ou termo livre em toda a base (sem IA): "
            "'quais funções calculam juros?', 'onde se calcula desconto do IPTU?'. Exige o "
            "índice de `php-map index-formulas`. Pelo menos um entre query e topic; topic "
            "aceita a chave ou o rótulo ('correção monetária'). group_by=routine (padrão): por "
            "função, a contagem, até 3 exemplos e o módulo. group_by=formula: as fórmulas em "
            "si. routine limita a uma função (repasse). Assunto é pista: confira na "
            "fórmula com formulas(routine=...)."
        )
    )
    async def formula_search(
        project: Project,
        query: Annotated[str | None, Field(description="Termo livre (ao menos um com topic).")] = (
            None
        ),
        topic: Annotated[
            str | None, Field(description="Chave ou rótulo do assunto, ex.: juros.")
        ] = None,
        module: Annotated[str | None, Field(description="Limita ao módulo, ex.: iptu.")] = None,
        routine: Annotated[str | None, Field(description="Limita a uma função.")] = None,
        group_by: Annotated[str, Field(description="routine ou formula.")] = "routine",
        limit: Annotated[int, Field(ge=1, le=tools.MAX_FORMULA_SEARCH)] = (
            formula_index_module.DEFAULT_LIMIT
        ),
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        return await _run(
            tools.formula_search,
            catalog,
            project,
            query,
            topic,
            module,
            routine,
            group_by,
            limit,
            offset,
        )

    @tool(
        description=(
            "Busca de campo por termo de negócio, no banco inteiro (sem IA): as "
            "colunas cujo nome no dicionário de dados casa com o termo (ex.: 'valor do metro "
            "quadrado do terreno'), as usadas em cálculo na frente, com as funções da base que "
            "leem a coluna e o menu onde a tabela é cadastrada. Termo genérico demais (só "
            "'valor') não busca. Devolve `markdown` e `search` (estruturado). Escolha uma "
            "coluna e peça `formulas(field=...)` para ver como o valor é calculado."
        )
    )
    async def field_search(
        project: Project,
        term: Annotated[str, Field(description="Termo de negócio, ex.: 'área do terreno'.")],
        limit: Annotated[int, Field(ge=1, le=30)] = 10,
        module: Annotated[str | None, Field(description="Limita ao módulo, ex.: cadastro.")] = None,
    ) -> dict[str, Any]:
        return await _run(tools.field_search, catalog, project, term, limit, module)

    @tool(
        description=(
            "Relatório de origem das funções da base: quantas têm origem (menu, "
            "rota, endpoint ou arquivo), só arquivo, são geradas pela auditoria ou não têm "
            "chamador, com o motivo de cada uma. Filtros de status, módulo e geradas. "
            "`sem_chamador` não é código morto (SQL dinâmico e JavaScript não aparecem)."
        )
    )
    async def routine_origins(
        project: Project,
        status: Annotated[
            str | None,
            Field(description="com_origem, so_arquivo, gerada, sem_chamador ou cadeia_sem_origem"),
        ] = None,
        module: Annotated[str | None, Field(description="Ex.: Module:cadastro")] = None,
        generated: Annotated[bool | None, Field(description="Só as geradas, ou sem elas")] = None,
        limit: Annotated[int, Field(ge=1, le=tools.MAX_ORIGINS)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        return await _run(
            tools.routine_origins, catalog, project, status, module, generated, limit, offset
        )

    @tool(
        description=(
            "Camadas de um fonte: para cima, os caminhos até o menu (menu, tela, "
            "JavaScript, rota, controller); para baixo, o que ele alcança por camada (service, "
            "repository, model, tabela), com a confiança de cada caminho. A camada é deduzida "
            "do nome, não é aresta. `path`: arquivo (caminho, nome ou trecho) ou id de uma "
            "Class:/Method:."
        )
    )
    async def layers(
        project: Project,
        path: Annotated[
            str, Field(description="Ex.: app/Domain/Tributario/ISSQN/Services/RedeSim/X.php")
        ],
        min_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5,
        max_paths: Annotated[int, Field(ge=1, le=layers_module.MAX_UP_PATHS)] = 10,
        max_down: Annotated[int, Field(ge=1, le=layers_module.MAX_DOWN_NODES)] = 60,
    ) -> dict[str, Any]:
        return await _run(tools.layers, catalog, project, path, min_confidence, max_paths, max_down)

    @tool(
        description=(
            "Plano de corte por endpoint: natureza leitura (candidato a réplica), "
            "comando (candidato a fila) ou indeterminado, tabelas lidas e escritas no sistema "
            "que serve, escritores de cada tabela e quem chama."
        )
    )
    async def cut_plan(
        project: Project,
        natureza: Annotated[
            str | None, Field(description="leitura, comando ou indeterminado")
        ] = None,
        protocol: Annotated[str | None, Field(description="soap, rpc ou http")] = None,
        endpoints: Annotated[
            str | None,
            Field(description="all, called, served ou matched (padrão: matched com 2+ projetos)"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=tools.MAX_CROSS)] = 50,
        depth: Annotated[int, Field(ge=1, le=10)] = 6,
    ) -> dict[str, Any]:
        return await _run(
            tools.cut_plan, catalog, project, natureza, protocol, endpoints, limit, depth
        )

    @tool(
        description=(
            "Fronteira de um módulo: tabelas próprias e compartilhadas, tabelas próprias que "
            "outros escrevem, tabelas de outros que ele escreve, acoplamento entrante e sainte, "
            "endpoints e custo_corte."
        )
    )
    async def module_boundary(
        project: Project,
        module: Annotated[str, Field(description="Ex.: contribuinte ou Module:contribuinte")],
    ) -> dict[str, Any]:
        return await _run(tools.module_boundary, catalog, project, module)

    @tool(
        description=(
            "Ranking de módulos candidatos à extração pelo custo_corte (menor primeiro), com os "
            "componentes. Apoio à decisão, não veredito."
        )
    )
    async def extraction_candidates(
        project: Project,
        top: Annotated[int, Field(ge=1, le=100)] = 15,
    ) -> dict[str, Any]:
        return await _run(tools.extraction_candidates, catalog, project, top)

    @tool(
        description=(
            "Modelos de briefing para IA: os modelos com os parâmetros de cada um "
            "e os blocos que o sistema preenche sem IA. Use antes de `briefing`."
        )
    )
    async def briefing_templates() -> dict[str, Any]:
        return await _run(briefing_service.mcp_templates)

    @tool(
        description=(
            "Monta um briefing em Markdown a partir de um modelo e do mapa: instruções para "
            "a IA e o contexto do sistema (ficha da tela, tabela, módulo, busca…), sem IA. "
            "Devolve o Markdown, as palavras, os blocos usados e os avisos de corte."
        )
    )
    async def briefing(
        project: Project,
        template: Annotated[str, Field(description="Id do modelo, ex.: como-se-faz")],
        params: Annotated[
            dict[str, str] | None,
            Field(
                description=(
                    "Valores dos parâmetros do modelo, ex.: {'tela': 'BusinessAction:menu:1576'}"
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        return await _run(briefing_service.mcp_briefing, catalog, project, template, params or {})

    _register_resources(server, docs_dir)
    register_prompts(server)
    return server


def _register_resources(server: MCPServer, docs_dir: Path | None) -> None:
    guide = resources.files("php_map.mcp").joinpath("guide.md").read_text(encoding="utf-8")

    @server.resource(
        GUIDE_URI,
        name="guia",
        description="Como usar estas ferramentas e interpretar source e confidence.",
        mime_type="text/markdown",
    )
    def read_guide() -> str:
        return guide

    if docs_dir is None:
        docs_dir = default_docs_dir()
    if docs_dir is None:
        logger.info("MCP: sem pasta de documentação, só o recurso %s", GUIDE_URI)
        return
    for key, (relative, description) in DOCS.items():
        path = docs_dir / relative
        if not path.is_file():
            logger.warning("MCP: recurso %s ausente (%s)", key, relative)
            continue
        server.resource(
            f"php-map://docs/{key}",
            name=key,
            description=description,
            mime_type="text/markdown",
        )(_reader(path))


def _reader(path: Path) -> Callable[[], str]:
    def read() -> str:
        return path.read_text(encoding="utf-8")

    return read


# --------------------------------------------------------------------------- transportes

LOCAL_HOSTS = ("localhost", "127.0.0.1")
MCP_PATH = "/mcp"


def transport_security(allowed_hosts: Sequence[str]) -> TransportSecuritySettings:
    """Allowlist de `Host` e `Origin` (proteção contra DNS rebinding): os locais e os públicos
    informados (o Cloudflare repassa o `Host` público, ex.: `mcp.exemplo.com.br`)."""
    names = list(dict.fromkeys([*LOCAL_HOSTS, *allowed_hosts]))
    hosts = [h for name in names for h in (name, f"{name}:*")]
    origins = [
        o
        for name in names
        for o in (f"http://{name}", f"https://{name}", f"http://{name}:*", f"https://{name}:*")
    ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def http_app(
    server: MCPServer,
    *,
    host: str,
    allowed_hosts: Sequence[str],
    token: str | TokenVerifier | None,
) -> ASGIApp:
    """O app ASGI do *Streamable HTTP* em `/mcp`, sem estado e com resposta JSON (nenhuma
    ferramenta notifica o cliente; um reinício do servidor não derruba sessão), com o bearer
    por fora. `token` é o token único ou um `TokenVerifier`; `None` só é aceito pelo
    chamador em host local (`--insecure-no-auth`)."""
    app: ASGIApp = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        transport_security=transport_security(allowed_hosts),
        host=host,
    )
    return BearerAuth(app, token) if token is not None else app


def run_stdio(server: MCPServer) -> None:
    anyio.run(server.run_stdio_async)
