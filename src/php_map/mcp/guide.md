# Guia do servidor MCP `php-map` (para a IA que consulta)

Este servidor expõe, **somente leitura**, os grafos de conhecimento de sistemas legados em PHP,
como o ERP **e-Cidade** (PHP procedural + Laravel + PL/pgSQL) ou aplicações em **Zend Framework 1**.
Cada projeto mapeado tem o seu grafo, e cada grafo
foi montado por análise estática determinística: AST do PHP, SQL das strings e do schema,
catálogo e menus do banco, e XML dos webservices. Use-o para entender o sistema antes de propor
mudanças e para planejar o desacoplamento.

## Como começar
1. `list_projects`: quais sistemas existem e a data do mapa (`scanned_at`). Se o servidor estiver
   configurado com uma **pasta** de grafos, `db` traz o arquivo escolhido (o mais
   novo e completo) e `db_from_folder: true`; o grafo mais recente aparece sem reiniciar o
   servidor.
2. `search`: ache telas, tabelas, endpoints e menus por termos de negócio ("anulação de empenho").
   Buscar o nome de uma tabela ou de um DAO (`cgmtipoempresa`) também acha os **fontes que a
   usam** (campo `uses`, trecho `usa: …`), antigos e Laravel, depois da tabela e do DAO em si.
3. `trace` (de uma tela/arquivo até o banco) ou `impact` (de uma tabela até quem a usa). No
   `impact`, os arquivos afetados incluem os dos métodos alcançados, e `dao_users` lista quem
   chama o DAO (`cl_*`) que toca a tabela, com a operação e o método (`incluir`).
4. `communication`, `endpoint` e `cross_summary`: por onde os sistemas conversam (SOAP, RPC,
   rotas HTTP), com parâmetros e o outro lado da costura entre projetos.
5. `node` e `neighbors`: detalhe de um nó e das arestas dele, com a evidência (arquivo e linha).
6. `source`: o trecho do código, só quando o servidor tem a pasta do projeto.
7. `cut_plan`, `module_boundary` e `extraction_candidates`: o que cortar e por onde (abaixo).

## Fronteira e plano de corte (desacoplamento)
- `cut_plan`: para cada endpoint, a **natureza** no sistema que o serve: `leitura` (candidato a
  réplica local), `comando` (candidato a fila/outbox) ou `indeterminado` (`sem_servidor`,
  `sem_alcance`, `alvo_dinamico`, `externo`). Tabelas de infraestrutura (auditoria `db_acount*`)
  não fazem de um endpoint um comando. `max_writers_direct`/`max_writers_via_dao` dizem quantos
  caminhos escrevem nas tabelas alcançadas: muitos escritores pedem captura no banco (CDC ou
  trigger), poucos permitem outbox na aplicação.
- Limite conhecido: escrita feita por objeto vindo de fábrica ou de classe ambígua pode não
  aparecer, e o endpoint sai `leitura` (backlog P39 no recurso de precisão). Confira os comandos
  críticos no código antes de decidir.
- `module_boundary` e `extraction_candidates`: tabelas próprias que outros escrevem, tabelas de
  outros que o módulo escreve, acoplamento entrante (Ca) e sainte (Ce) e
  `custo_corte = ca + ce + 2 × escreve_em_outros + 2 × tabelas_proprias_escritas_por_outros`.
  É **apoio à decisão**: menor custo = mais fácil de extrair, mas mostre os componentes ao
  usuário. Só funciona em grafo escaneado com o catálogo (com módulos).

## O que uma função da base faz
- `routine(project, routine, offset, limit)`: o **texto** de uma função da base (o
  `pg_get_functiondef` que o scan leu, em páginas de linhas, `offset` 1 = `CREATE`), com `used_by`
  (quantos chamam: PHP, outras funções, triggers) e `tables` (o que lê e grava, com a confiança de
  cada aresta). `routine` aceita o id completo, `schema.nome` ou só o nome; com várias assinaturas
  (`lpad`, `fc_executa_ddl`) a resposta traz `candidates`, sem texto — salvo quando as outras só
  **repassam** para uma (`CALLS_RPC` interna à família, ex. `fc_issqn` de 8 e 9
  argumentos repassando para a de 11), caso em que resolve sozinho e a resposta traz `resolved`
  (`by: "repasse"`, `chain`, `others`).
- O texto é **da data do scan** (`read_at`): a base pode ter mudado depois. Para ver o que uma
  função faz use `routine`; para saber quem a usa, `impact` ou `neighbors`.
- `routine` traz também `origin`: **por onde a função é usada**. `status` (`com_origem`, `so_arquivo`, `gerada`, `sem_chamador`, `cadeia_sem_origem`) e `reason`; `entries` com o menu (`kind` menu, com o `menu_path`), a rota, o endpoint ou o arquivo mais próximos (até 5 de cada tipo), a `distance` (1 = chamador direto), o `via` (da origem até a função) e a `confidence` (a menor das arestas do caminho); `module` com a fração dos votos. `mentions` são arquivos cujo texto cita o nome da função: **hipótese (confiança 0,5)**, só para quem não tem origem, fora do acoplamento.
- `routine_origins(project, status, module, generated, limit, offset)`: o relatório. Para "quais funções não têm origem" use `status="sem_chamador"`. `sem_chamador` **não é código morto**: SQL dinâmico, JavaScript e agendamentos não aparecem no grafo. A meta é a classificação completa, não a origem completa. Função de trigger só sobe pela tabela e **por quem escreve nela**.
- Funções `generated` (`fc_auditoria_tabela_*`) são geradas pela auditoria e muito parecidas.
- Grafo sem o texto das funções (escaneado sem a base): a ferramenta diz para rodar o scan novo.
- `routine` também traz `rules` e `profile`, lidos do texto por um leitor léxico (não
  entende o código, só a sintaxe — por isso funciona nas funções de trigger). `rules.items[].kind`:
  `validation` (`RAISE EXCEPTION` guardado por `IF`/`CASE`, com a `condition` — "quando X, erro
  Y" é a regra de negócio), `error`/`warning` (sem guarda), `diagnostic` (`NOTICE`/`INFO`/`DEBUG`/
  `LOG`, fora do padrão de `rules_kind`), `decision` (todo `IF`/`ELSIF`/`CASE WHEN` de instrução,
  mesmo sem `RAISE`), `handler` (`EXCEPTION WHEN`) e `comment` (anotação do autor, não código
  desativado). `condition` guarda a cadeia de até 3 níveis ("a E b E SENÃO de c"); `dynamic=true`
  quando a mensagem foi montada com `||` (os pedaços literais ficam em `message`); `truncated`
  marca condição cortada em 300 caracteres. `profile.counts` tem o total por tipo mais `loops`,
  `performs`, `dynamic_sql`, `commented_code`, `header_comment` e `short_comment`.
- `routine_rules(project, query, kind, level, topic, limit, offset)`: busca regras em **todas** as
  funções por texto da mensagem/condição, nível, tipo e **assunto** (`topic`: chave ou
  rótulo, ex. `multa`) — responde "quais funções validam saldo?" ou "quais regras falam de multa?"
  sem chamar `routine` em cada uma. Cada item traz `topic`. Grafo sem as tabelas (escaneado antes
  da extração das regras): rode o scan novo; `topic` sem o índice de fórmulas: rode
  `php-map index-formulas --db <banco>`.
- `routine` também traz `queries`: cada instrução SQL do corpo (`kind`: `select`,
  `insert`, `update`, `delete`, `dynamic` — `EXECUTE`, sem campos — ou `parse_fallback` — só as
  tabelas, por regex, confiança 0,5), com `tables` e, em `fields`, cada coluna com o `role`
  (`read`, `write`, `filter`, `join`, `sort`) e a `confidence` (1,0, ou `1/n` quando a coluna sem
  qualificação cabe em mais de uma tabela do `FROM`/`JOIN`). Variável do PL/pgSQL e `NEW`/`OLD`
  de trigger nunca viram "coluna".
- `column_usage(project, table, column, role, limit, offset)`: as funções que leem, gravam,
  filtram, juntam ou ordenam por uma coluna de uma tabela — "campo → funções", sem abrir
  `routine` de cada uma. Coluna que o schema não conhece naquela tabela → erro com as colunas
  parecidas; grafo sem as tabelas de consultas SQL: rode o scan novo.

## Ficha pronta para colar numa IA
- `routine_card(project, routine, source_lines=120, format="markdown")`: a ficha em português de
  uma função — para que serve (comentário do autor), onde é usada, parâmetros e retorno (com o
  tipo traduzido), dados que usa (tabela e coluna com o rótulo do dicionário), o que valida (a
  condição e a mensagem lado a lado), o que chama e quem a chama, e o texto original. Tudo **sem
  IA**, só o que o mapa já sabe; `format="json"` devolve a estrutura em vez do Markdown.
- `screen_card(project, entry, depth=4, format="markdown")`: a mesma ideia para uma tela do
  menu — menu, dados gravados e lidos, funções da base chamadas (cada uma com um resumo das suas
  validações) e mensagens de validação/erro.

## Análise profunda de uma função
- `formulas(project, routine, depth=2, max_words=3000)`, `formulas(project, field="schema.tabela.coluna")` ou `formulas(project, table="schema.tabela")`, abaixo: **sem IA**, lê o texto da função e
  devolve `markdown` (uma página do dossiê) e, só com `structured=true`, `dossier` (os mesmos
  dados, estruturados, iguais em toda página). O dossiê tem:
  - **Visão geral** (no início, nunca cortada): quantas fórmulas há na função e nas
    auxiliares, por **assunto** (correção, juros, multa, desconto, inflator, valor histórico,
    estorno…) e por função. O assunto é **pista** (pelo nome do destino ou pela função
    chamada), não fato do e-cidade: confira na fórmula. A resposta traz também `overview` e
    `suggestions` (perguntas sugeridas, com a âncora de cada uma).
  - **Cálculos:** uma linha por fórmula (`linha N: destino = forma legível`, e a técnica — o
    texto como está escrito). Atribuições simples (`x := 0`, `x := y`) não são fórmula.
  - **Funções auxiliares:** as chamadas dentro de uma fórmula (`fc_juros`, `fc_multa`...), até
    `depth` níveis e 10 funções, cada uma com as suas fórmulas.
  - **Dados de entrada e onde são cadastrados:** cada `tabela.campo` com o nome do dicionário e
    o **menu** de cadastro.
  - **Não rastreado:** o que o mapa não resolveu, com o motivo (tabela sem tela de cadastro,
    tela sem item de menu, menu sem caminho — scan sem `--catalog-db` —, função ambígua).
- Use o que está em **Não rastreado** como lacuna, nunca complete com palpite. Se o dossiê diz
  que bateu nos limites (acima de 400 fórmulas, bem mais raro que antes da paginação), diga
  quantas fórmulas e funções ficaram de fora. Nome com várias assinaturas que só repassam para uma
  (ex. `fc_calcula`) resolve sozinho: o dossiê sai da assinatura que faz o trabalho, com
  a linha do repasse no início e `resolved` na resposta (em **toda** página, não só na 1ª). Nome
  que continua ambíguo (sobrecargas independentes, ex. `fc_saldoitensempenho`) devolve erro com as
  candidatas: repita com o id completo de uma delas.
- **Paginado:** o dossiê inteiro (nenhuma fórmula, gravador ou leitor fica fora do
  alcance) é dividido em páginas de ~`max_words` palavras, nas fronteiras de `## `. A resposta traz
  `page`, `pages`, `next_page` (siga até vir `null`) e `sections` (`[{"title", "page"}]`, para
  pular direto à seção que interessa). `overview` e `suggestions` só vêm na página 1. Peça
  `structured=true` só quando precisar do JSON (`dossier`): é a maior parte do tamanho da
  resposta e repete o Markdown. Pedir uma página que não existe é erro, com o total de páginas.
- **Pelo campo** (`field="caixa.arrecad.k00_valor"`, sem `routine`): o `markdown` tem **Onde é
  cadastrado** (menu e tela; a tela de uma classe `cl_x` grava a **tabela inteira**, o mapa não diz
  coluna por coluna — salvo no mapa novo, ver «Quem preenche» abaixo), **Como é calculado** (até 10 funções que gravam o campo, a fórmula do valor
  ou "cópia direta de" outra coluna, as entradas até o menu e "e mais N"), **Quem usa** (funções
  que leem o campo pelo nome e, à parte, as que fazem `select *` na tabela: podem usar o campo,
  o mapa não diz) e **Não rastreado**. Tabela ou coluna inexistente devolve erro claro.
- **Pela tabela** (`table="protocolo.cgmtipoempresa"`; `max_readers=15`, até 500): o
  `markdown` traz os campos, **onde é cadastrada** e a seção **Quem grava e quem lê no código
  PHP** (o mesmo bloco aparece em `access` do dossiê de `field`). Em **Quem grava** vêm todos os
  fontes PHP que gravam em produção — **SQL direto**, **DAO** (`cl_*`: o fonte que chama
  `incluir`/`alterar`/`excluir`) e **model Eloquent** — cada um com camada, operação, linhas (as
  chamadas somadas) e **menu**. Migração fica em «Migrações (fora da contagem)». Model Eloquent
  aparece como **uso** («o mapa não diz se lê ou grava»), nunca como `INSERT`. A gravação pelo DAO
  grava a **tabela inteira**: o mapa não diz coluna por coluna. Fonte sem menu traz o **motivo**
  (nenhum menu chega, ninguém chama, mapa sem catálogo, subida não tentada): é lacuna do mapa, não
  «sem menu de verdade». Em **Quem lê** há a contagem completa por camada e os `max_readers`
  principais (telas com menu primeiro); não afirme que «só estes leem».

### Quem preenche o campo no código PHP
- No mapa que já traz os campos preenchidos, a gravação pelo DAO (`cl_x::incluir`/`alterar`) deixa de ser
  «a tabela inteira»: o dossiê diz **quais campos o código preenche e com que valor**. Na tabela é a
  coluna **Preenche** de «Quem grava» (até 6 campos; o literal sai com o valor, `z03_tipoempresa =
  32`); no campo é a seção **Quem preenche este campo no código PHP**, uma linha por (fonte, valor),
  e o JSON traz `fills` (na tabela, dentro de cada gravador; no campo, `fills` na raiz do dossiê).
- Há **dois jeitos** e eles não valem igual:
  - **atribuição** (`atribuicao`): `$oDao->campo = expr` antes do `incluir`/`alterar`. Prevalece.
  - **formulário** (`formulario`): o campo está no formulário da tela (`db_input('campo', …)` ou
    `<input name="campo">` do arquivo ou de um que ele inclui); o DAO o lê do `$_POST` quando a
    atribuição não preencheu (`atualizacampos()`). Só vale em tela procedural, e a confiança é a
    da gravação × 0,8.
- O **valor é o texto do código**, não o valor resolvido: `$iTpEmpresaSocio` é a variável, não 32.
  O tipo (`valor fixo`, `variável`, `parâmetro`, `chamada`, `expressão`, `nulo`, `campo do
  formulário`) diz a forma da expressão. Não invente o valor que a variável tem.
- Mapa **anterior** à extração dos campos preenchidos (`fills_available` falso): o dossiê diz «refaça o scan para ver
  quem preenche cada campo». Sem nenhuma linha para o campo mas com gravação pelo DAO: o valor pode
  vir do `$_POST` de outro jeito, de um padrão do banco ou de um `alterar` que não o muda — é
  lacuna do mapa, não prova de que ninguém preenche.

## Buscar por assunto
- `formula_search(project, query, topic, module, routine, group_by="routine", limit=20, offset=0)`:
  busca **fórmulas** por assunto e/ou termo livre em **toda a base**, numa chamada, sem ler função
  por função — "quais funções calculam juros?", "onde se calcula desconto do IPTU?". Exige o
  índice de fórmulas (`php-map index-formulas --db <banco>`, ou um scan novo). Pelo menos um
  entre `query` e `topic`; `topic` aceita a chave ou o rótulo em português («correção monetária»).
  `query` casa com o assunto (chave e rótulo), o destino, o rótulo do dicionário da coluna
  gravada, a expressão legível e o nome da função — a maioria das fórmulas é «outros», então a
  busca **não pode depender só do assunto**. `routine` limita a uma função (resolve por repasse
  entre sobrecargas). `group_by="routine"` (padrão) devolve, por função, a contagem,
  até 3 fórmulas de exemplo (`linha: destino = fórmula legível`) e o módulo, maior contagem
  primeiro; `group_by="formula"` devolve as fórmulas em si, paginadas por linha.
- **O assunto é sempre pista** (pelo nome do destino, pela função chamada ou pelo rótulo do
  dicionário), nunca fato do e-cidade: confira a fórmula de verdade com `formulas(routine=…)`
  antes de afirmar algo ao usuário — a resposta de `formula_search` repete esse aviso.
- Quando usar qual: `formula_search` para "quais funções calculam X" (cálculo, `routine_formulas`);
  `routine_rules(topic=…)` para "quais regras falam de X" (validação/mensagem, `routine_rules`);
  `field_search` para "onde está cadastrado o campo X" (rótulo do dicionário, não
  cálculo). Fluxo típico: `formula_search(topic="juros")` → escolha a função no topo →
  `formulas(routine=…)` para o dossiê completo com a linha e a fórmula de verdade.
- `list_projects` traz `formula_index: true|false` por projeto: `false` quer dizer que
  `formula_search`/`routine_rules(topic=…)` não respondem ali ainda (rode o `index-formulas`).

## Partir de um termo de negócio ou de um menu
- `field_search(project, term, limit=10, module=None)`: **sem IA**. Para quando o analista não sabe
  o nome técnico («onde é cadastrado o valor do metro quadrado do terreno?»). Casa o termo com o
  **nome da coluna no dicionário de dados**, no banco inteiro, e devolve `markdown` e `search`.
  As colunas vêm em três grupos: **Usadas em cálculo** (alguma função da base lê a coluna: estão
  na frente, com as funções que leem), **Só cadastradas** (nenhuma função da base lê) e **Sem
  cadastro conhecido**. Cada uma diz o menu em que a tabela é cadastrada (ou por que não achou).
- Termo genérico («valor», «código») não busca: o aviso pede «de quê». Mais de uma coluna casando
  é a regra, não o erro: **a resposta pode depender do caso** (cada município tem a sua
  `fc_iptu_calculavvt_<cidade>_<ano>`). Diga isso, escolha as colunas que importam e peça
  `formulas(field=...)` de cada uma. `e mais N` quer dizer que há mais colunas com placar ≥ 0,6.
- `formulas(project, menu="BusinessAction:menu:1533")` (ou um trecho do caminho do menu):
  o dossiê do menu — arquivos que o implementam, funções da base que ele alcança **com a contagem
  de fórmulas**, e as tabelas que a tela grava com os campos do dicionário. **Chamada dinâmica**
  (`execute` com texto montado) aparece em **Não rastreado**: não é «sem cálculo». Trecho que casa
  com mais de um menu devolve a lista; repita com o id.

## De onde vem um valor
- `value_origin(project, question, routine=None, targets=None, max_words=2000)`, em duas
  chamadas, **sem acesso ao mapa durante a sua própria análise** — você só recebe o texto que
  esta ferramenta devolve:
  1. **Sem `targets`:** interpreta a pergunta (termos da pergunta × rótulos do dicionário de
     dados, só dentro do alcance de `routine`, isto é, ela e as funções que chama) e devolve
     `candidates` (`target`, `label`, `reason`, `score`). Informe `routine` sempre que souber
     qual função calcula o valor: sem ela, os candidatos vêm de uma busca mais ampla e com aviso.
  2. **Com `targets`** (um `target` da fase 1, ou `schema.tabela.coluna` digitado): devolve
     `dossier`, um Markdown curto com a cadeia do valor — da função até a **coluna base** e até
     a **tela** que a cadastra, cada elo com a linha do texto que o sustenta. Um elo com o selo
     **"hipótese — confirmar"** não é fato (é um palpite pelo nome do arquivo, sem chamador
     conhecido no mapa): diga isso na sua resposta, não afirme como certeza.
- Cite sempre a linha de cada elo do `dossier`. Se a cadeia parar antes de uma tela ("não consta
  no mapa"), diga isso — não invente onde o valor é cadastrado.
- `max_words` (500–6000) limita o tamanho do dossiê; o padrão (2000) já cobre a maioria das
  cadeias reais.

## Briefings para IA (modelos)
- `briefing_templates`: os modelos disponíveis (`como-se-faz`, `impacto-de-mudanca`,
  `especificar-regras`, `casos-de-teste`, `plano-de-corte-do-modulo`, `treinamento-do-modulo`,
  `duvida-com-contexto`, `criar-novo-modelo`), cada um com os parâmetros.
- `briefing(project, template, params)`: monta o **Markdown** do modelo com o contexto do mapa
  (ficha da tela, tabela, módulo, busca…), pronto para colar numa IA. O contexto vem do grafo,
  sem IA, e as instruções mandam usar **só** ele.
- O parâmetro de tela aceita o caminho do arquivo, o item de menu (`BusinessAction:menu:…`) ou um
  trecho do caminho de menu; se mais de um casar, a resposta lista os candidatos.

## Como interpretar
- **IDs de nó**: `<Rótulo>:<chave>`, por exemplo `File:emp4_anularempenho001.php`,
  `Table:public.cgm`, `Method:cl_cgm::incluir`, `Endpoint:ecidade:soap:getDadosEmpresa` (o
  sistema no ID é **quem serve** o endpoint).
- **Toda aresta tem `source` e `confidence`.** `1.0` é determinística (AST, XML, catálogo).
  Abaixo de `1.0` é **hipótese** (nome dinâmico, ambiguidade dividida em `1/n`, regra de nome).
  Diga ao usuário quando uma conclusão depende de aresta com confiança menor que 1.
- **Escrita × leitura:** `INSERTS`, `UPDATES` e `DELETES` escrevem; `QUERIES` lê.
- **Tela → JavaScript → rota (e-cidade):** `REFERENCES` liga a tela ao `.js` que ela carrega
  (`File` com `kind = "js"`; `via = db_app::load` quando veio do `db_app::load`) e o JS à tela que
  ele abre. `CALLS_HTTP` (fonte `JS_URL`) liga a tela ou o JS à `Route` do Laravel cuja URI casa
  com a URL literal (`evidence.literal`, `evidence.prefix = "/v4/api"` quando a base veio do
  `PHPSession.requestApi`). `0.9` = uma rota; `0.9/n` = n rotas com a mesma URI (verbos
  diferentes: o verbo do `HttpClient` não é lido). JS de biblioteca (`prototype.js`, jQuery,
  `scripts.js`) é nó `is_infra`, sem leitura.
- **Model Eloquent → tabela (e-cidade):** `MAPS_TO` (Class → Table, fonte `ELOQUENT_MODEL`) liga a
  classe que estende `Illuminate\Database\Eloquent\Model` (direto ou por um `BaseModel` do
  sistema) à tabela. `1.0` = a tabela está no `protected $table` da classe; `0.8` = não há
  `$table` e a tabela foi **deduzida do nome da classe** (`evidence.rule = "eloquent:classname"`,
  plural do Laravel em inglês: `RedeSimImporta` → `rede_sim_importas`), então confira o nome no
  schema antes de concluir. `evidence.connection` aparece quando o model declara `$connection`
  (o destino não muda). Tabela que o banco não tem vira a pendência `eloquent_table_missing`:
  model obsoleto ou convenção errada. Só o **mapeamento** está no grafo: quem lê ou grava pelo
  model (`->get()`, `->save()`, `::create()`) não vira `QUERIES`/`INSERTS`.
- **Camadas de um fonte:** `layers(project, path)` mostra, para um arquivo (ou o id de
  uma `Class:`/`Method:`), os caminhos **para cima** até o menu (`menu → tela → JS → rota →
  controller → este fonte`, `up`, do mais perto ao mais longe) e o que ele alcança **para baixo**
  (`down`: service, repository, model, tabela, cada nó com o `edge_type`, a `confidence` do
  caminho e a `parent`). A `layer` de cada nó (`menu`, `tela`, `js`, `rota`, `controller`,
  `service`, `repository`, `model`, `dao`, `tabela`, `outro`) é **deduzida do nome e do tipo**, não
  é aresta: um `Service` fora da pasta `Services` aparece como `outro`. `up_total`/`down_total` dizem
  quantos havia; `down_by_layer` conta tudo, mesmo o que passou do limite (`max_down`). O caminho
  de um menu tem a confiança da **menor** aresta. A rota mostrada é a primeira que leva ao
  fonte: um controller com várias rotas tem outras iguais. `trace`, `impact` e "Menus por fonte"
  agora atravessam `CALLS_HTTP`, `ROUTES_TO` e `MAPS_TO`, e a origem de uma função da base
  (`routine_origins`) continua depois da rota até a tela e o menu.
  **Camada `dao`:** as classes `cl_*` de `classes/db_*_classe.php` formam uma camada
  própria, entre o model e a tabela, com **um item por classe** (`down_by_layer.dao` e
  `down_total` contam classes, não métodos). O item traz `factory` (`getDao`/`usarDao`: como o
  fonte obteve a classe; `null` se não foi por `getDao`/`usarDao`) e `members` (os métodos que o
  fonte chama, sem o `__construct`, cada um com `edge_type` e `confidence`); o argumento do
  `getDao('x')` é o nome da classe sem `cl_`. `max_down` vai até 500 (padrão 60): com o padrão,
  fontes grandes cortam a lista (confira `down_total` × `len(down)`); peça 500 para vê-la inteira.
- `truncated: true` quer dizer que houve corte por limite: refine (menos `depth`, filtros) em
  vez de concluir que algo não existe.

## Limites conhecidos (não tire conclusão errada)
- Chamadas feitas por **JavaScript** às ações de RPC do e-cidade não aparecem; às rotas do
  Laravel, só as de URL **literal** (`CALLS_HTTP`): URL montada só em variável, `fetch`/`$.ajax`
  sem literal e Vue ficam de fora, e o literal da API sem rota vira a pendência
  `http_url_dynamic`. Por isso `servido_sem_cliente` e rota sem `CALLS_HTTP` **não** significam
  código morto.
- `include` com variável e nome de método em variável viram pendência, com confiança menor.
- O grafo é uma **foto** da data em `scanned_at`; o código pode ter mudado depois.
- Os grafos têm estrutura, schema e textos do código, **não** têm dados de contribuinte.
- A precisão medida e os erros conhecidos estão no recurso `php-map://docs/precisao`.
- Grafo sem módulos (escaneado sem o catálogo do banco) mostra tudo em `(sem módulo)`.

## Prompts de relatório
Além das ferramentas, o servidor expõe **prompts**: roteiros em texto que já chamam as
ferramentas certas, na ordem certa e com a paginação correta, e pedem a resposta no formato que
o projeto define. Nenhum prompt chama ferramenta ao ser montado (`prompts/get`) — quem chama é a
IA que recebe o roteiro, como qualquer outra sessão.
- `relatorio-analise-profunda(projeto, alvo, tipo="")`: o relatório de análise profunda (as
  mesmas 4 seções do detalhe técnico de `formulas`) de uma função, campo, tabela, menu ou termo
  de negócio, seguindo `next_page` até o fim.
- `buscar-por-assunto(projeto, assunto_ou_termo, modulo="")`: busca por assunto/termo em toda a
  base (`formula_search`, `routine_rules(topic=…)`) e responde "onde se calcula /
  quais regras / o que confirmar".
- `briefing-<id>` — um por modelo **oficial** de `briefing/models/*.md` (hoje os 9 modelos são
  oficiais: `briefing-como-se-faz`, `briefing-especificar-regras`, `briefing-casos-de-teste`,
  `briefing-impacto-de-mudanca`, `briefing-plano-de-corte-do-modulo`,
  `briefing-treinamento-do-modulo`, `briefing-duvida-com-contexto`, `briefing-criar-novo-modelo`,
  `briefing-de-onde-vem-o-valor`) — com `projeto` mais os parâmetros
  do modelo (ver `briefing_templates`); chama `briefing(project, template, params)` e segue as
  instruções do Markdown devolvido.
- No Claude Code: `/mcp__php-map__<nome-do-prompt>`. No Claude Desktop e no claude.ai: o
  ícone de prompts (ou `/`) na caixa de mensagem.

## Recursos úteis
`php-map://docs/ontologia` (nós, arestas, fontes e confiança), `php-map://docs/glossario`
(termos do domínio: empenho, liquidação, ISSQN…), `php-map://docs/perfis` (como cada sistema é
lido: e-Cidade, Zend 1), `php-map://docs/precisao` (quanto o grafo acerta e os limites
conhecidos) e `php-map://docs/mcp` (ferramentas, prompts e configuração dos clientes). Eles
vêm da pasta `docs/` do repositório, ou da pasta indicada em `--docs-dir`.
