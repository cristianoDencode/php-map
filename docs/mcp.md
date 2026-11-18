# Servidor MCP

O `php-map` expõe os grafos, **somente leitura**, pelo [Model Context Protocol](https://modelcontextprotocol.io). Qualquer cliente MCP (Claude Code, Cursor, VS Code, entre outros) consulta o mapa por ferramentas, lê a documentação como recursos e usa roteiros prontos (prompts). O servidor nunca escreve nos grafos e nunca os carrega inteiros: cada consulta lê só o que precisa (cerca de 100 MB de pico num sistema de ~35 mil arquivos PHP).

Há duas formas de rodar:

| Forma | Comando | Quando usar |
|---|---|---|
| Processo próprio | `php-map mcp` | Cliente local (stdio) ou servidor remoto simples (HTTP) |
| Dentro da interface | `php-map ui` (serve o MCP em `/mcp`) | Uma porta, um processo, tokens e túnel gerenciados pela página **Acesso** e **MCP e túnel** |

## Instalação

O MCP é uma dependência opcional:

```bash
pip install -e ".[mcp]"
```

Sem ela, `php-map mcp` explica o que instalar e `/mcp` na interface responde 503.

## Origem dos grafos

Aponte o servidor para os grafos de uma destas formas:

- `--projects <projects.json>`: o mesmo arquivo que a interface mantém. Caminho relativo vale primeiro em relação à pasta do arquivo e depois ao diretório atual.
- `--db <arquivo.db>` (repetível): um grafo por opção, sem `projects.json`.

**Grafo mais recente automático.** O campo `db` de um projeto, ou um `--db`, pode ser uma **pasta** de grafos gerados pela interface. O servidor escolhe o `.db` mais novo que já terminou de ser gravado (o que tem o `<nome>.scan_summary.json` ao lado) e reavalia essa escolha a cada consulta; um scan novo aparece sem reiniciar. O `projects.json` também é relido quando o arquivo muda (`mtime`): adicionar ou remover um projeto vale na consulta seguinte. `list_projects` informa `db` (o arquivo escolhido) e `db_from_folder`.

## Ferramentas

Todas são somente leitura e anotadas como tal. Toda resposta traz o `source` e a `confidence` das arestas; ver o recurso `php-map://guia` e a [ontologia](ontologia.md).

### Orientação e navegação

| Ferramenta | Para quê |
|---|---|
| `list_projects` | Sistemas disponíveis, data do mapa, se há módulos, texto de funções e índice de fórmulas |
| `search` | Telas, tabelas, endpoints e itens de menu por termos de negócio; buscar uma tabela ou DAO também acha os fontes que a usam |
| `node` | Detalhe de um nó |
| `neighbors` | Arestas de um nó, com evidência (`arquivo:linha`) |
| `trace` | Do arquivo de entrada (tela, RPC) até as tabelas e funções do banco |
| `impact` | O que é afetado por uma mudança numa tabela, incluindo quem chama o DAO que a toca |
| `layers` | Camadas de um fonte: menu → tela → JS → rota → controller para cima; service, repository, model, DAO e tabela para baixo |
| `source` | Trecho do código-fonte (ver [Leitura do código-fonte](#leitura-do-código-fonte)) |

### Comunicação e fronteira

| Ferramenta | Para quê |
|---|---|
| `communication` | Endpoints SOAP, RPC e rotas HTTP numa lista paginada, com parâmetros, quem implementa e quem chama |
| `endpoint` | Detalhe de um endpoint e o que ele alcança |
| `cross_summary` | O outro lado da costura quando dois projetos estão mapeados |
| `cut_plan` | Por endpoint: leitura (candidato a réplica), comando (candidato a fila) ou indeterminado, com o motivo |
| `module_boundary` | Tabelas próprias que outros escrevem, tabelas de outros que o módulo escreve, acoplamento entrante e sainte |
| `extraction_candidates` | Módulos ordenados pelo custo de corte; é apoio à decisão, não veredito |

### Funções da base de dados

| Ferramenta | Para quê |
|---|---|
| `routine` | Texto de uma função (como lido do banco no scan), quem a usa, tabelas e colunas que lê e grava, regras e consultas. Nome com várias assinaturas que só repassam entre si resolve sozinho |
| `routine_origins` | Por onde cada função é usada (menu, rota, endpoint, arquivo) e as que não têm origem conhecida |
| `routine_rules` | Busca regras (validações, erros, decisões, comentários) em todas as funções, por texto, tipo, nível ou assunto |
| `column_usage` | Funções que leem, gravam, filtram, juntam ou ordenam por uma coluna |
| `routine_card` / `screen_card` | A ficha em português de uma função ou de uma tela do menu, sem IA, pronta para colar num assistente |

### Fórmulas, campos e origem de valores

| Ferramenta | Para quê |
|---|---|
| `formulas` | Análise profunda, por função, campo, tabela ou menu: cada cálculo, as entradas até `tabela.campo` com o rótulo do dicionário e o menu de cadastro, quem grava e quem lê no código PHP |
| `formula_search` | Busca fórmulas por assunto (juros, multa, desconto…) ou termo livre em toda a base |
| `field_search` | Parte de um termo de negócio e acha a coluna pelo rótulo do dicionário de dados |
| `value_origin` | "De onde vem este valor?", em duas chamadas: candidatos e dossiê com a cadeia até a coluna base e a tela que a cadastra |

### Briefings

| Ferramenta | Para quê |
|---|---|
| `briefing_templates` | Modelos disponíveis e seus parâmetros |
| `briefing` | Monta o Markdown de um modelo com o contexto do mapa (ficha da tela, tabela, módulo, busca) |

Os modelos são `como-se-faz`, `impacto-de-mudanca`, `especificar-regras`, `casos-de-teste`, `plano-de-corte-do-modulo`, `treinamento-do-modulo`, `duvida-com-contexto`, `de-onde-vem-o-valor` e `criar-novo-modelo`.

### Paginação e cortes

- `formulas` monta o dossiê inteiro e o divide em páginas de aproximadamente `max_words` palavras, nas fronteiras de seção. A resposta traz `page`, `pages`, `next_page` (siga até vir `null`) e `sections`. O JSON estruturado só vem com `structured=true`.
- `communication`, `routine`, `routine_rules`, `formula_search` e outras aceitam `limit` e `offset`.
- `truncated: true` significa que houve corte por limite. Refine (menos profundidade, filtros) em vez de concluir que algo não existe.
- Cada chamada tem um limite de 60 segundos.

## Recursos

| URI | Conteúdo |
|---|---|
| `php-map://guia` | Guia para a IA que consulta: como começar, como interpretar `source` e `confidence`, limites conhecidos. Embutido no pacote; sempre presente |
| `php-map://docs/ontologia` | Nós, arestas, fontes e confiança ([ontologia.md](ontologia.md)) |
| `php-map://docs/glossario` | Termos do domínio ([glossario-ecidade.md](glossario-ecidade.md)) |
| `php-map://docs/perfis` | Perfis de projeto ([perfis.md](perfis.md)) |
| `php-map://docs/precisao` | Quanto o grafo acerta e os erros conhecidos ([precisao.md](precisao.md)) |
| `php-map://docs/mcp` | Este documento |

Os recursos `php-map://docs/*` vêm da pasta indicada por `--docs-dir`, que deve apontar para o diretório `docs/` deste repositório. Sem `--docs-dir`, só o guia é servido. Arquivo ausente na pasta é ignorado com um aviso no log.

## Prompts

Prompts são roteiros em texto: papel, ferramentas a chamar em ordem (inclusive as páginas) e o formato do relatório. Montar um prompt não chama nenhuma ferramenta; quem chama é a IA do cliente.

| Prompt | Parâmetros | O que pede |
|---|---|---|
| `relatorio-analise-profunda` | projeto, alvo, tipo (opcional) | Relatório de uma função, campo, tabela, menu ou termo de negócio, seguindo `next_page` até o fim |
| `buscar-por-assunto` | projeto, assunto ou termo, módulo (opcional) | Onde se calcula, quais regras existem, o que confirmar |
| `briefing-<modelo>` | projeto e os parâmetros do modelo | Um por modelo de briefing; chama `briefing` e segue as instruções do Markdown devolvido |

No Claude Code, aparecem como `/mcp__php-map__<nome-do-prompt>`. Em outros clientes, no seletor de prompts do próprio cliente.

## Transportes

### stdio (local)

O cliente sobe o servidor como subprocesso; não há porta nem token.

```bash
php-map mcp --projects out/projects.json --docs-dir docs
```

### HTTP (remoto)

```bash
php-map mcp --projects out/projects.json --docs-dir docs --transport http --port 8765
```

O endpoint é `/mcp` (Streamable HTTP, sem estado, resposta JSON). Regras:

- **Autenticação por token no cabeçalho** `Authorization: Bearer <token>`, comparado em tempo constante. O token nunca aparece em log nem em mensagem de erro.
- **Tokens do cofre** (recomendado): ver [Tokens](#tokens). O servidor só sobe com pelo menos um token ativo.
- **Token único por variável**: `PHP_MAP_MCP_TOKEN` (ambiente ou `.env`, mínimo de 16 caracteres) continua aceito, para um servidor remoto simples.
- **`--insecure-no-auth`** dispensa o token, mas só com `--host 127.0.0.1` ou `localhost`.
- **`--allowed-host <nome>`** (repetível) aceita um `Host` público, por exemplo atrás de um túnel. Fora a lista (locais mais os informados), o servidor recusa a requisição, o que protege contra DNS rebinding.
- A porta padrão (8765) é a mesma de `php-map ui`. Para rodar os dois separados no mesmo computador, use `--port` em um deles; ou use o MCP embutido na interface.

## Tokens

Os tokens ficam num cofre SQLite próprio, separado dos grafos (`out/access/access.db`, ou o caminho de `PHP_MAP_ACCESS_DB` / `--access-db`). O cofre guarda só o SHA-256 do token, o nome, o prefixo de exibição, a validade (90 dias por padrão), o último uso com o IP e a revogação. O valor completo aparece **uma única vez**, na criação. A revogação vale na chamada seguinte, sem reiniciar.

```bash
php-map token create --name meu-notebook [--expires 30d|90d|180d|365d|never]
php-map token list
php-map token revoke <id|prefixo>
```

A página **Acesso** da interface faz o mesmo (criar com validade, ver prefixo e último uso, revogar) e já mostra o comando e o trecho de configuração do cliente. Detalhes de segurança em [seguranca-operacao.md](seguranca-operacao.md).

## MCP pela interface

`php-map ui` serve o MCP em `/mcp`, na mesma porta e no mesmo processo, com os tokens do cofre. O catálogo é o dos projetos da tela e acompanha a troca de projeto e os scans novos. `--no-mcp` desliga.

Uma requisição é considerada **remota** quando o `Host` é um host público (`--public-host <nome>`, repetível) ou quando traz `X-Forwarded-For`, `X-Forwarded-Host` ou `Forwarded`. Requisição remota só alcança `/mcp`: qualquer outro caminho (interface, `/graphql`, login, scan) responde 404. O `/mcp` aceita só o bearer do cofre, nunca o cookie do login.

## Túnel

Para acessar o MCP de fora da máquina, a página **MCP e túnel** (`/publicar`) sobe um túnel configurável: um comando que entregue HTTPS, sem amarrar o projeto a um serviço. Há dois modelos prontos:

- **ngrok** (conta gratuita, domínio fixo): `ngrok http {port} --url=https://<seu-dominio>.ngrok-free.app`. Guarde o `NGROK_AUTHTOKEN` no `.env` ou rode `ngrok config add-authtoken`. Não use `--host-header=rewrite`: o servidor precisa do `Host` público para reconhecer o túnel.
- **cloudflared** (sem conta): `cloudflared tunnel --url http://127.0.0.1:{port}`. A URL muda a cada subida e a configuração dos clientes muda junto.

Antes de dizer `no ar`, o programa confere: pelo menos um token ativo; varredura de segredos nos grafos sem achado fora da lista de exceções revisadas; e, pela URL pública, que `POST /mcp` sem token responde 401 e que `GET /graphql` responde 404. O comando roda sem shell; segredos como o authtoken entram só como *nomes* de variável na configuração, nunca como valor, e o log é mascarado. Parar o servidor encerra o túnel e seus processos filhos. Se o túnel cair, o estado vira `caiu`, sem reinício automático. `php-map ui --tunnel` sobe o túnel salvo junto com o servidor.

Serviços gratuitos têm limite de tráfego. As respostas do MCP são pequenas, mas uma listagem grande de `communication` soma.

## Leitura do código-fonte

`source` só funciona quando o projeto tem a pasta do código (`source` no `projects.json`). Mesmo assim, arquivos de configuração e chaves são bloqueados: `*.ini`, `.env*`, `*.pem`, `*.key`, `*.pfx`, `*.p12`, `*.crt`, `libs/db_conn.php`, `config/*.php` e qualquer coisa dentro de `configs/`, `certificado/`, `.git/`, `vendor/` e `node_modules/`.

No MCP servido pela interface, `source` vem **desligado por padrão**. O interruptor *Permitir ler o código-fonte pelo MCP* na página **Acesso** o liga, e ao reiniciar o servidor ele volta a desligado.

## Configuração de clientes

Substitua os valores entre `<…>`. Prefira variável de ambiente para o token em vez de gravá-lo em arquivo versionado.

### Claude Code

Local (stdio):

```bash
claude mcp add php-map -- php-map mcp --projects /caminho/para/projects.json --docs-dir /caminho/para/docs
```

Remoto (HTTP):

```bash
claude mcp add --transport http php-map https://<dominio>/mcp \
  --header "Authorization: Bearer <token>"
```

### Cursor e VS Code

Ambos aceitam um arquivo `mcp.json` (`.cursor/mcp.json` no Cursor; `.vscode/mcp.json` no VS Code, onde a chave é `servers` em vez de `mcpServers`). Exemplo para HTTP com o token numa variável de ambiente:

```json
{
  "mcpServers": {
    "php-map": {
      "url": "https://<dominio>/mcp",
      "headers": { "Authorization": "Bearer ${PHP_MAP_TOKEN}" }
    }
  }
}
```

Para stdio:

```json
{
  "mcpServers": {
    "php-map": {
      "command": "php-map",
      "args": ["mcp", "--projects", "/caminho/para/projects.json", "--docs-dir", "/caminho/para/docs"]
    }
  }
}
```

A sintaxe exata de cada cliente muda com o tempo; consulte a documentação dele. O que não muda é o servidor: um endpoint `/mcp` com bearer, ou um comando stdio.

## Boas práticas de uso

- Comece por `list_projects`, leia `php-map://guia` e siga o fluxo `search` → `trace`/`impact` → `communication`/`endpoint`/`cross_summary`.
- Aresta com `confidence` menor que 1 é hipótese; diga isso ao ler o resultado.
- O grafo é uma foto da data do scan. O texto das funções é o lido naquele dia.
- Grafo escaneado sem o catálogo do banco mostra tudo em `(sem módulo)`, e `module_boundary` e `extraction_candidates` não respondem.
- Os limites conhecidos (JavaScript que chama RPC, SQL dinâmico, nomes de método em variável) estão no guia e em [precisao.md](precisao.md).
