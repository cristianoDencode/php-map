# Ontologia do grafo

O contrato de dados entre o scan e tudo o que lê o grafo (CLI, API, interface, MCP). O código que o impõe está em [`graph/model.py`](../src/php_map/graph/model.py): uma aresta que viole as regras de confiança ou de evidência é rejeitada na construção. Para o pipeline que produz esses dados, veja [arquitetura.md](arquitetura.md).

O grafo é um multigrafo dirigido gravado em SQLite com três tabelas principais:

```sql
meta  (key, value)                              -- versão do formato, data do scan, origem, contagens
nodes (id, label, attrs)                        -- attrs: JSON
edges (id, src, dst, type, source, confidence, evidence, attrs)
```

## Identidade dos nós

Todo nó tem um `id` estável e legível, `<Rótulo>:<chave>`. Dois scans do mesmo código geram os mesmos IDs.

| Rótulo | Chave | Exemplo |
|---|---|---|
| `File` | Caminho relativo à raiz do projeto, com `/` | `File:emp4_anularempenho001.php` |
| `Function` | Nome em minúsculas (funções PHP não diferenciam caixa) | `Function:db_postmemory` |
| `Class` | Nome completo (FQCN) em minúsculas | `Class:cl_periodo` |
| `Method` | `<classe>::<método>` | `Method:cl_periodo::incluir` |
| `Route` | `<VERBO> <uri>` (Laravel) ou `<módulo>/<controller>/<action>` (Zend 1) | `Route:GET /v4/api/financeiro/empenho/empenhos` |
| `RpcCall` | Nome da função de banco chamada, quando ela não está no schema lido | `RpcCall:fc_calcula` |
| `PgFunction` | `schema.nome(tipos)` | `PgFunction:public.fc_calcula(int4,int4)` |
| `Table` | `schema.nome` | `Table:empenho.empempenho` |
| `Module` | Nome normalizado: minúsculas, sem acento, só `[a-z0-9]` | `Module:empenho` |
| `BusinessAction` | `menu:<id do item>` (vem do menu do sistema) | `BusinessAction:menu:9952` |
| `Endpoint` | `<sistema que serve>:<protocolo>:<nome>` | `Endpoint:ecidade:soap:getDadosEmpresa` |

Observações:

- O ID de uma `Table` usa o schema **real** quando o schema é lido do banco (`Table:empenho.empempenho`). Um nome citado no código que o banco não conhece vira um placeholder no schema `public`.
- O ID de um `Endpoint` nomeia **quem serve**, nunca quem chama. Por isso o mesmo endpoint tem o mesmo ID no grafo do servidor e no do cliente, e o cruzamento entre dois projetos é um simples *join* pelo ID. O nome mantém a caixa do código; `?` indica um nome que não é literal no código (por exemplo, `Endpoint:ecidade:soap:?`). Endpoints de sistemas fora do mapeamento usam `externo` no lugar do sistema (`Endpoint:externo:soap:servicos.exemplo.gov.br/ws/consulta`).
- Um `Module` do dicionário de dados e um do menu que sejam o mesmo módulo (nomes diferentes) são fundidos por uma tabela de equivalência do perfil; o ID usa a forma do menu.
- Nós **placeholder** (`resolved = false`) existem para alvos citados e não encontrados (uma tabela fora do schema, uma classe fora do escaneamento). Quando o nó real aparece, seus atributos prevalecem.

## Atributos dos nós

Comuns: `label`, `name`, `path` (quando houver), `line_start`, `line_end`, `is_infra` (infraestrutura: funções de sessão, auditoria genérica, bibliotecas carregadas por toda tela), `module_hint` (prefixo do nome do arquivo, quando o perfil o define) e `resolved`.

| Nó | Atributos relevantes |
|---|---|
| `File` | `parse_status` (`ok`, `partial`, `parse_error`, `read_error`); `dispatch_actions` (literais dos `case` de um `switch` que despacha ações de RPC); `kind = "js"` para arquivos JavaScript alcançados por alguma tela |
| `Table` | `schema`, `columns`, `kind` (`table`, `view`, `foreign`), `source_file` (`db:<schema>.<nome>` quando lida do banco); do catálogo: `catalog` (descrição, prefixo das colunas, rótulo) e `columns_catalog` (rótulo e descrição por coluna) |
| `PgFunction` | `schema`, `name`, `args`, `language`, `returns`; o texto original fica em tabela própria (`routine_sources`), não nos atributos |
| `Module` | `name` de exibição, `names` (nomes brutos), `aliases`, `menu_id`, `description` |
| `BusinessAction` | `name`, ID e caminho do item de menu (`menu_path`), `active`, arquivo da tela |
| `Route` | Laravel: `verb`, `uri` completa (com os prefixos de todos os grupos), `area` (`api`, `web`), `middleware` e `namespace` acumulados, `params`, `action`, `path`, `line`. Zend 1: `module`, `controller`, `action` |
| `Endpoint` | `system`, `protocol` (`soap`, `rpc`, `http`), `kind` (por exemplo `soap_consulta`, `soap_processamento`, `rpc_action`, `external_soap`), `params` (só o que o código **declara**: nome, obrigatoriedade, tipo e de onde veio) |
| `Class` | Entidades Doctrine: `doctrine_columns` (tipo, chave, geração), lidas das anotações |

## Tipos de aresta

Toda aresta tem `type`, `source`, `confidence` e `evidence`. Arestas repetidas com a mesma chave `(origem, destino, tipo, fonte)` são agregadas: ficam a primeira evidência e os atributos `occurrences` (total) e `sample_lines` (até 5 linhas).

| Tipo | De → Para | Fontes (`source`) | Confiança |
|---|---|---|---|
| `DECLARES` | `File` → `Function`/`Class`; `Class` → `Method` | `PHP_AST` | 1,0 |
| `INCLUDES`, `REQUIRES` | `File` → `File` | `PHP_AST` | 1,0 |
| `CALLS` | código → `Function`/`Method`; `Endpoint` → `Method` | `PHP_AST`, `PHP_AST_AMBIGUOUS`; `WS_XML` no endpoint | 1,0 ou 1/*n* |
| `INSTANTIATES` | código → `Class`; `Endpoint` → `Class` | `PHP_AST`, `PHP_AST_AMBIGUOUS`; `WS_XML` | 1,0 ou 1/*n* |
| `EXTENDS`, `IMPLEMENTS` | `Class` → `Class` | `PHP_AST` | 1,0 |
| `REFERENCES` | `File` → `File`: literal `*.php` em HTML/JS; tela → JavaScript; JavaScript → tela | `STRING_REF` | 0,9 |
| `CALLS_HTTP` | tela ou JavaScript → `Route`: URL literal casada com a rota | `JS_URL` | 0,9 (uma rota) ou 0,9/*n* |
| `ROUTES_TO` | `Route` → `Method` | `LARAVEL_RESOLVER`, `ZEND1_RESOLVER` | 1,0 |
| `RENDERS` | `Method` → `File` (`.phtml`) | `ZEND1_RESOLVER` | 1,0 |
| `MAPS_TO` | `Class` → `Table` (entidade Doctrine, model Eloquent) | `DOCTRINE_ANNOTATION`, `ELOQUENT_MODEL` | 1,0 (tabela declarada) ou 0,8 (deduzida do nome da classe) |
| `SERVES` | `File`/`Method`/`Function` → `Endpoint` | `WS_XML`, `PHP_AST` | 1,0 |
| `CALLS_REMOTE` | código → `Endpoint` (SOAP, curl, serviço externo) | `PHP_AST`, `PHP_AST_AMBIGUOUS` | 1,0, ou 0,5 quando o nome não é literal |
| `CALLS_RPC` | código/`PgFunction` → `PgFunction` (ou `RpcCall`) | `SQL_AST`, `SQL_REGEX` | 1,0 ou 1/*n* (sobrecargas); regex ≤ 0,7 |
| `QUERIES` | código/`PgFunction`/`Endpoint` → `Table` | `SQL_AST`, `SQL_REGEX`, `DOCTRINE_ANNOTATION`, `WS_XML` | 1,0; 0,7 (regex validada); 0,5 (não validada) |
| `INSERTS`, `UPDATES`, `DELETES` | código/`PgFunction` → `Table` (`TRUNCATE` conta como `DELETES`) | `SQL_AST`, `SQL_REGEX`, `DOCTRINE_ANNOTATION` (DQL) | 1,0; 0,7; 0,5 |
| `TRIGGERS` | `Table` → `PgFunction` | `SQL_AST` | 1,0 |
| `CALLS_DYNAMIC` | código → alvo inferido | `HEURISTIC_AI` | 0,0–0,99 |
| `BELONGS_TO` | `File`/`Function`/`Table` → `Module` | `ECIDADE_CATALOG`, `ZEND1_RESOLVER` (1,0); `NAMING_RULE`, `AI_CLASSIFICATION` (< 1,0) | ver acima |
| `IMPLEMENTS_ACTION` | `File`/`Method` → `BusinessAction` | `ECIDADE_CATALOG` | 1,0 |

### Fontes

| `source` | Origem da informação |
|---|---|
| `PHP_AST` | Árvore sintática do PHP, sem ambiguidade |
| `PHP_AST_AMBIGUOUS` | Árvore sintática, mas a resolução do alvo tem *n* candidatos |
| `STRING_REF` | Literal em string de PHP, HTML ou JS que cita outro arquivo |
| `JS_URL` | Literal de URL em JavaScript (ou em `<script>` inline) casado com uma rota |
| `SQL_AST` | SQL parseado pelo `pglast` |
| `SQL_REGEX` | Fallback por expressão regular, validado ou não contra o schema |
| `LARAVEL_RESOLVER` | Resolução de rotas do Laravel (grupos, prefixos, `namespace`) |
| `ZEND1_RESOLVER` | Convenções do Zend 1 (módulo por diretório, controller/action, view) |
| `DOCTRINE_ANNOTATION` | Anotações `@Entity`/`@Table` e DQL |
| `ELOQUENT_MODEL` | `$table` do model Eloquent ou a convenção do Laravel |
| `WS_XML` | XML de requisição de webservice: a declaração do sistema |
| `ECIDADE_CATALOG` | Dicionário de dados e menus do e-Cidade, lidos do banco ou de arquivos |
| `NAMING_RULE` | Regra de nome (prefixo do arquivo, classe DAO → tabela) |
| `HEURISTIC_AI`, `AI_CLASSIFICATION` | Reservadas a resolvedores com IA (fora do scan padrão); sempre com confiança < 1,0 |

## Regras de confiança

- `confidence = 1,0` **somente** para fontes determinísticas e sem ambiguidade: `PHP_AST`, `SQL_AST`, `LARAVEL_RESOLVER`, `ZEND1_RESOLVER`, `WS_XML` e `ECIDADE_CATALOG`. A exceção é `CALLS_RPC` por `SQL_AST` com sobrecargas, que divide por *n*.
- `PHP_AST_AMBIGUOUS` vale `1/n`, com os arquivos candidatos em `evidence.candidates`. A ambiguidade também gera uma pendência.
- `STRING_REF` vale exatamente 0,9. `JS_URL` vale 0,9 ou 0,9/*n*.
- `SQL_REGEX` vale no máximo 0,7: 0,7 se a tabela existe no schema (ou tem classe DAO), 0,5 se não pôde ser validada.
- `DOCTRINE_ANNOTATION` e `ELOQUENT_MODEL` aceitam apenas 1,0 (tabela declarada) ou 0,8 (deduzida do nome da classe).
- `NAMING_RULE` nunca passa de 0,9. O voto do prefixo usa a fração de votos (`min(0,9, fração)`); a regra DAO → tabela usa 0,9 (ou 0,9 / *n* quando a tabela pertence a *n* módulos). Só se aplica ao que o catálogo não classificou.
- Um item de menu inativo mantém a confiança; o estado fica em `active = false`.
- Toda aresta tem `evidence` com `path` (e `line`, quando existir) ou `cache_key`. Evidência do catálogo lido do banco: `{path: "db:<tabela>", key: "<chave>=<valor>"}`.

Consumidores filtram por confiança (`--min-confidence` nos comandos, `minConfidence` na API). A interface e o MCP sempre mostram `source` e `confidence` junto à ligação.

## Evidência

Além de `path` e `line`, a evidência registra como o alvo foi decidido:

| Campo | Significado |
|---|---|
| `declared_in` | Arquivo declarante escolhido quando havia um único candidato |
| `candidates` | Declarantes ou tipos possíveis, em arestas ambíguas |
| `via_include` | Tela cujo tipo de variável foi herdado pelo arquivo incluído |
| `via` | Como o tipo do receptor foi descoberto: `dao_factory:<método>`, `property:<nome>`, `di:constructor`, `di:param`, `di:container` |
| `rule` | Regra aplicada (`prefix:<h>`, `dao:<tabela>`, `eloquent:classname`, `doctrine:classname`) |
| `validated` | Em `SQL_REGEX`: confirmado pelo schema ou pela DAO |

## Camadas

`layers` e os relatórios agrupam os nós em camadas de apresentação, deduzidas do tipo e do nome (não são aresta nem atributo gravado): `menu`, `tela`, `js`, `rota`, `controller`, `service`, `repository`, `model`, `dao`, `tabela` e `outro`. Um caminho `classes/db_*_classe.php` é a camada `dao`; controller, service e repository são reconhecidos pelo sufixo do nome da classe ou pela pasta (`Controllers/`, `Services/`, `Repositories/`). A cadeia navegada vai de **menu → tela → JavaScript → rota → controller → service → repository → model/DAO → tabela**.

As arestas percorridas por `trace`, `impact`, camadas e "menus por fonte" são `INCLUDES`, `REQUIRES`, `CALLS`, `INSTANTIATES`, `REFERENCES`, `CALLS_HTTP`, `ROUTES_TO`, `MAPS_TO`, `CALLS_RPC`, `QUERIES`, `INSERTS`, `UPDATES` e `DELETES`. O acoplamento entre módulos ignora arestas que tocam uma `Route` ou um arquivo JavaScript, que são pontes e não dependência de código.

## Pendências

O que não pôde ser resolvido não vira aresta: é gravado em `unresolved.jsonl`, uma linha por ocorrência (`kind`, `path`, `line`, `snippet`, `candidates`, `hint`, `count`). Os tipos de maior volume (`ambiguous_function`, `ambiguous_class`, `unknown_receiver`) são agregados por `(kind, path, hint)`, guardando a primeira ocorrência e o total.

| `kind` | Quando |
|---|---|
| `dynamic_include`, `unresolved_include` | O alvo do include não é literal, ou o arquivo não existe entre os escaneados |
| `variable_function`, `call_user_func`, `variable_method`, `variable_class`, `dynamic_call` | Chamada cujo alvo só se conhece em execução |
| `ambiguous_function`, `ambiguous_class` | Vários declarantes e nenhuma regra desempata |
| `unknown_receiver` | Chamada `$x->m()` sem tipo inferível para `$x` |
| `di_unbound_interface` | Receptor injetado cujo tipo é interface sem implementação concreta conhecida |
| `dynamic_sql`, `sql_parse_error` | SQL montado em execução ou que o parser recusa |
| `dynamic_endpoint`, `ws_class_missing`, `ws_endpoint_duplicate`, `ws_xml_parse_error` | Problemas ao ligar endpoints de webservice |
| `route_file_missing`, `route_dynamic`, `route_controller_missing` | Rotas do Laravel cujo arquivo, URI ou destino não se resolvem |
| `http_url_dynamic` | URL literal com cara de rota de API que não casa com nenhuma `Route` |
| `eloquent_table_missing`, `doctrine_table_missing`, `dql_entity_unknown` | Model ou entidade aponta para tabela (ou classe) que o schema não tem |
| `class_not_found` | (Zend 1) a convenção de nomes previu um caminho que não está entre os arquivos |

## Como estender

1. **Novo tipo de aresta ou fonte.** Acrescente-o aos enums de [`graph/model.py`](../src/php_map/graph/model.py) e declare a regra de confiança em `validate_edge`. O contrato falha cedo; [`tests/unit/test_graph_model.py`](../tests/unit/test_graph_model.py) cobre as regras.
2. **Novo resolvedor.** Crie o módulo em [`resolve/`](../src/php_map/resolve/) e ligue-o ao pipeline em [`pipeline.py`](../src/php_map/pipeline.py), registrando o estágio (nome e rótulo) para aparecer no progresso e no resumo do scan. Emita arestas só com fonte e confiança coerentes com as regras acima; o que for ambíguo vira pendência.
3. **Novo sistema.** Crie um perfil (ver [perfis.md](perfis.md)); o grafo e as consultas não mudam.
4. **Atualize este documento** e os casos de gabarito (ver [precisao.md](precisao.md)).
