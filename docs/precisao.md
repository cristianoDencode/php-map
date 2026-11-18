# Precisão do mapeamento

O `php-map` afirma ligações entre telas, classes, funções e tabelas. Este documento explica **como se mede quanto elas acertam**, quais são os números atuais e onde estão os limites conhecidos. Para o contrato de `source` e `confidence`, veja [ontologia.md](ontologia.md).

## Como é medida

A medição tem três camadas. O script de medição e o gabarito da camada 1 são mantidos no ambiente de desenvolvimento do projeto: o gabarito usa recortes de um sistema real e, por isso, **não é distribuído neste repositório**. O que o repositório traz é o método, os números e o projeto de exemplo sintético (`tests/fixtures/zend1-mini`), com o próprio gabarito de arestas (`tests/fixtures/expected/zend1_mini_edges.json`) conferido pelos testes. Um gabarito completo sobre um legado fictício está no [roteiro](../README.md#roteiro).

### 1. Gabarito (sempre, ~2 s, sem Docker)

O recorte é resolvido e comparado com gabaritos escritos à mão. O gabarito cobre arestas (tipo, fonte e confiança mínima), pendências esperadas, a busca, a ficha de regras, regras e campos das funções da base, origem de valor, fórmulas, dossiês, assunto das fórmulas, quem grava e lê uma tabela e os campos que o PHP preenche antes de gravar pela DAO. As ligações do perfil Zend 1 têm o gabarito público citado acima.

O gabarito é um **piso de regressão**: não prova que o mapa do sistema inteiro está certo, só que o que já se sabe estar certo continua certo. Uma métrica do gabarito nunca pode piorar sem justificativa escrita.

### 2. Cobertura e ambiguidade no sistema inteiro

Com um cache de extração completo, o script resolve o sistema real e calcula métricas de cobertura:

| Métrica | Definição | Direção boa |
|---|---|---|
| CALLS ambíguas | % das arestas `CALLS` com `PHP_AST_AMBIGUOUS` (1/*n*) | ↓ |
| Receptores tipados | % das chamadas `$x->m()` com o tipo do receptor inferido | ↑ |
| `unknown_receiver` | % das chamadas `$x->m()` sem tipo | ↓ |
| Fábricas resolvidas | Chamadas de fábrica de DAO que viraram `INSTANTIATES` / total de chamadas de fábrica | ↑ |
| Tabelas reais / placeholder | `Table` vindas do schema / tabelas só citadas no código | ↑ reais |
| `CALLS_RPC` ambíguas | % das chamadas a funções da base com sobrecargas que nem a aridade desempata | ↓ |
| SQL por regex | % das arestas de tabela vindas do fallback `SQL_REGEX` | ↓ |
| Regex validado | % das `SQL_REGEX` confirmadas pelo schema ou pela DAO (0,7); o resto fica com 0,5 | ↑ |
| Tabelas com módulo | Tabelas tocadas pelo código com módulo do catálogo / total | ↑ |
| Telas no menu | Telas da raiz com `IMPLEMENTS_ACTION` / total de telas (as que sobram são candidatas a código sem menu) | ↑ |
| Arquivos com módulo | Arquivos reais, não infra, com `BELONGS_TO` de qualquer fonte, quebrados em catálogo × regra de nome | ↑ |

### 3. Amostra revisada à mão

Com amostragem (`--sample N`), o script sorteia N arestas **inferidas** (fábrica de DAO, propriedade, include, ambíguas, regra de nome, Laravel/DI, URL de JavaScript…) e grava uma lista com a linha do código-fonte:



`--sample-kind` escolhe o tipo (`ambiguous`, `catalog`, `di`, `eloquent`, `factory`, `http`, `include`, `inferred`, `naming`, `property`, `sql`, `sql_regex`, `sql_rpc` e outros) e `--seed` torna o sorteio reproduzível. Cada aresta é conferida **no código-fonte**, e o resultado (acertos/amostra) é registrado junto com a mudança que motivou a medição. Um erro encontrado vira ou um caso de gabarito (para nunca mais regredir) ou um item do backlog de limites, abaixo.

## Como propor um caso novo

1. Ache um trecho do **seu** sistema que o mapa interpretou errado (ou certo, mas de forma frágil).
2. Reduza-o a um exemplo **inventado**: mesmos nomes de padrão, nenhum código, nome de cliente ou dado real. Acrescente-o ao projeto de exemplo em `tests/fixtures/` (ou a uma fixture nova).
3. Escreva a **expectativa** ao lado (aresta com tipo, fonte e confiança mínima; ou a pendência esperada). Não ajuste uma expectativa para um teste passar: se ela mudou, a justificativa vai na descrição da mudança.
4. Rode `pytest`. O caso novo deve passar e os demais não podem cair.

Veja também [CONTRIBUTING.md](../CONTRIBUTING.md).

## Números atuais

### Gabarito do ambiente de desenvolvimento (medido em 2026-11-18)

| Grupo | Resultado |
|---|---|
| Arestas do recorte do e-Cidade (tipo + fonte + confiança) | 114/114 |
| Pendências esperadas | 13/13 |
| Busca (hit@5) | 9/9 |
| Ficha de regras de uma tela | 7/7 |
| Regras das funções da base | 8/8 |
| Campos e consultas das funções da base | 6/6 |
| Origem de valor | 2/2 |
| Fórmulas | 9/9 |
| Dossiê de cálculo (entradas e menus) | 15/15 |
| Dossiê de um campo | 35/35 |
| Busca de campo por termo | 27/27 |
| Assunto das fórmulas | 59/59 |
| Quem grava e quem lê a tabela no PHP | 43/43 |
| Campos que o PHP preenche antes de gravar pela DAO | 33/33 |

### Scan do e-Cidade completo (2026-11-06)

Medido sobre a instalação completa, com o schema e o catálogo lidos do banco e a extração PHP já em cache (`scan_summary.json` do scan).

| Medida | Valor |
|---|---|
| Arquivos PHP mapeados | 35.359 (35.314 `ok`, 36 `partial`, 9 `parse_error`) |
| Nós | 167.278 |
| Arestas | 899.119 |
| Tempo total do scan (extração em cache) | ≈ 326 s (resolução do PHP 53 s, SQL 96 s, índice de busca 50 s, schema 13 s, gravação 19 s, agregados 16 s) |
| Funções da base lidas | 1.265 (texto de 7,7 MiB); 23.049 regras; 32.865 campos em 16.193 consultas |
| Rotas do Laravel / models Eloquent / endpoints | 1.337 / 485 / 2.360 |
| Módulos / ações de menu ligadas a uma tela | 73 / 6.557 |

**Cobertura** (última medição completa, 2026-11-06, com schema e catálogo do banco):

| Métrica | Valor |
|---|---|
| CALLS ambíguas | 22,1% |
| Receptores tipados × `unknown_receiver` | 73,9% × 26,1% |
| Fábricas de DAO resolvidas | 5.551 / 5.650 |
| Tabelas reais × placeholder | 4.706 × 1.241 |
| `CALLS_RPC` ambíguas | 6,3% |
| SQL por regex / validado | 11,8% / 98,4% |
| Tabelas com módulo | 3.857 / 5.806 (66,4%) |
| Telas no menu | 5.211 / 12.871 (40,5%) |
| Arquivos com módulo | 15.388 / 35.352 (43,5%): 5.025 pelo catálogo, 10.363 por regra de nome |

**Origem das arestas** (899.119 no total): `PHP_AST` 580.589 · `SQL_AST` 127.137 · `PHP_AST_AMBIGUOUS` 88.407 · `STRING_REF` 56.119 · `SQL_REGEX` 16.859 · `ECIDADE_CATALOG` 15.979 · `NAMING_RULE` 10.363 · `LARAVEL_RESOLVER` 1.247 · `WS_XML` 1.112 · `JS_URL` 822 · `ELOQUENT_MODEL` 485.

**Pendências** (ocorrências, por `kind`): `ambiguous_function` 428.421 · `unknown_receiver` 138.046 · `ambiguous_class` 45.047 · `dynamic_sql` 6.829 · `unresolved_include` 481 · `dynamic_include` 340 · `http_url_dynamic` 200 · `variable_method` 170 · `variable_function` 168 · `variable_class` 121 · `call_user_func` 80 · `dynamic_call` 40 · `route_controller_missing` 38 · `dynamic_endpoint` 21 · `eloquent_table_missing` 18 · `ws_class_missing` 8 · `di_unbound_interface` 4.

A cobertura não foi remedida depois de 2026-11-06. O gabarito, que roda a cada mudança, segue integral (tabela anterior); um scan completo novo atualiza estas tabelas.

## Limites conhecidos

O que o mapa **não** afirma ou afirma com ressalva. Quem consome o grafo (inclusive a IA) deve tratar esses pontos como limites, não como erros isolados.

- **Chamadas ambíguas.** Uma função de infraestrutura declarada em vários arquivos (`modification`, `db_query`, `db_getsession`…) gera centenas de milhares de ocorrências `1/n`. O impacto sobre `trace` é pequeno porque essas funções estão marcadas como infraestrutura, mas elas inflam `CALLS` e o volume de pendências. Cópias legadas de bibliotecas declaram as mesmas classes (`ambiguous_class`).
- **Receptores sem tipo.** Cerca de um quarto das chamadas `$x->m()` não tem receptor tipado: variável local sem origem conhecida, retorno de método ou fábrica que não é de DAO, cadeias `$a->b()->c()`, objeto obtido de fábrica em tempo de execução. O efeito prático é uma escrita no banco que **não aparece** no alcance de uma tela ou endpoint, quando ela passa por um objeto assim. Ausência de aresta não prova ausência de chamada.
- **SQL dinâmico.** SQL montado por concatenação com partes não literais, `EXECUTE` em PL/pgSQL e nomes de tabela em variáveis viram pendência (`dynamic_sql`); só a parte literal é extraída. Arestas por regex valem no máximo 0,7. Funções PL/pgSQL que o parser recusa (tipo composto, tipo com schema explícito) são analisadas por instrução.
- **Includes e despacho dinâmicos.** `include` com caminho montado, `call_user_func`, chamada por nome em variável e despacho de ações de RPC por `switch` (o `trace` entra em todos os `case`, mesmo os que a tela nunca chama).
- **Leitura e escrita pelo ORM.** Para models Eloquent, o mapa liga o model à tabela mas não classifica `->get()` × `->save()`; a DAO `cl_x::incluir/alterar` grava a tabela inteira (o mapa não diz coluna por coluna).
- **Módulos por regra de nome.** Menos da metade dos arquivos tem módulo, e o que vem de regra de nome (confiança < 1,0) pode errar em telas sem menu abertas por uma tela de outro módulo. Prefixos de nome são tendência, não regra.
- **URL de JavaScript.** URL montada em vários pedaços, base em variável ou rota que não existe no código escaneado não casa com uma `Route`.
- **Fórmulas.** O assunto (juros, multa, correção…) é uma pista por regras de nome, com falsos positivos; boa parte das fórmulas fica em "outros". Toda fórmula traz o texto e a linha para conferência.
- **O que só existe em execução.** Dados, configuração por cliente, comportamento condicionado a sessão e a parâmetros de instalação não são vistos por uma análise estática.
