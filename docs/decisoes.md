# Decisões de arquitetura

Registro curto das escolhas que moldam o `php-map`: o contexto que as motivou, o que foi escolhido e o que isso custa. A numeração é própria deste documento. Para a visão do conjunto, veja [arquitetura.md](arquitetura.md); para o contrato dos dados, [ontologia.md](ontologia.md).

## D1. Determinismo antes de IA

**Contexto.** Um ERP legado com dezenas de milhares de arquivos PHP não cabe no contexto de um modelo de linguagem, e a resposta de um modelo sobre "quem grava esta tabela" não é auditável. Quem planeja uma migração precisa saber *por que* uma ligação existe.

**Escolha.** O scan usa apenas análise estática: AST do PHP, parser do PostgreSQL, o dicionário de dados do próprio sistema e convenções explícitas. Nenhuma etapa do scan chama IA. O que a análise estática não resolve é registrado como pendência com o motivo. As interfaces que falam com assistentes de IA (o servidor MCP, as fichas e os briefings) entregam dados já resolvidos e deixam à IA só a redação e a análise.

**Consequência.** O grafo é reproduzível e cada ligação tem evidência (`arquivo:linha`). Em troca, o que depende de execução (SQL montado em tempo de execução, `call_user_func`, includes dinâmicos) fica como pendência, em vez de uma suposição plausível.

## D2. Toda aresta tem fonte e confiança

**Contexto.** As ligações do grafo têm qualidades muito diferentes: uma chamada resolvida pela AST, uma regex sobre uma string SQL e uma classificação por prefixo de nome não merecem o mesmo peso.

**Escolha.** Cada aresta carrega `source` (de onde veio) e `confidence` (de 0 a 1), e o contrato é validado na construção: 1,0 é reservado a fontes determinísticas; regex valida no máximo 0,7; ambiguidade entre *n* candidatos vale 1/*n*; regra de nome fica abaixo de 1,0.

**Consequência.** Consultas, interface e MCP podem filtrar por confiança e sempre mostram a origem. Todo novo resolvedor precisa declarar a que fonte e a que confiança tem direito.

## D3. SQLite como persistência do grafo

**Contexto.** `trace` e `impact` não podem exigir um novo scan de dezenas de milhares de arquivos a cada consulta, e a ferramenta deve funcionar na máquina de quem analisa, sem serviços extras.

**Escolha.** O grafo é gravado em um arquivo SQLite (`nodes`, `edges`, `meta`), com o índice de busca FTS5 e as tabelas de agregados no mesmo arquivo. NetworkX é usado só para montar o grafo e nos testes de equivalência. As consultas da API e do MCP percorrem o grafo por SQL (busca em largura em lotes por nível), sem carregá-lo inteiro.

**Consequência.** Nenhuma dependência de servidor; um grafo é um arquivo que se copia e se versiona. O grafo do e-Cidade ocupa centenas de megabytes, mas o pico de memória do MCP fica em torno de 100 MB. Um banco de grafos ou DuckDB continuam possíveis como exportadores, sem mudar o contrato.

## D4. Índice global de símbolos e resolução com confiança 1/n

**Contexto.** O e-Cidade carrega boa parte das classes por *autoload*, sem `require`, e tem funções de mesmo nome declaradas em vários arquivos (várias versões de `db_query`, cópias legadas de bibliotecas).

**Escolha.** O scan indexa todos os símbolos do projeto. Um nome com um único declarante liga com confiança 1,0; com vários, vence o único que estiver no fechamento de includes do arquivo chamador; depois, regras de convenção do *autoload* (`cl_x` → `classes/db_x_classe.php`, `*.model.php`, `*.service.php`). Se ainda restar ambiguidade, a aresta aponta para o nó do nome com `PHP_AST_AMBIGUOUS`, confiança 1/*n*, os candidatos na evidência e uma pendência. O tipo do receptor de `$x->m()` vem de `new`, de fábricas de DAO, de propriedades, de parâmetros tipados e de injeção de dependência.

**Consequência.** Nada é descartado por ser incerto e nada é afirmado sem aviso. O custo é volume: as pendências por ambiguidade são numerosas (ver [precisao.md](precisao.md)), e `is_infra` marca as funções de infraestrutura para que elas não dominem as métricas.

## D5. O texto das funções da base fica no grafo

**Contexto.** Muita regra de negócio do e-Cidade vive em funções PL/pgSQL. Para responder "o que `fc_calcula` faz?" é preciso ver a função, ligada ao que o grafo já sabe: quem a chama e que tabelas ela lê e grava.

**Escolha.** Com o schema lido do banco, o scan grava o texto original de cada função (e o seu sha1) numa tabela própria do mesmo `.db`, fora dos atributos dos nós, e **depois** de gravar o grafo. Sobre esse texto rodam leitores determinísticos: regras e validações (`RAISE EXCEPTION` sob condição, tratamento de exceção, comentários), campos e consultas por instrução e fórmulas.

**Consequência.** O grafo guarda alguns megabytes de DDL (o texto do banco de referência tem cerca de 8 MB), mas permite fichas em português, busca de regras e análise de cálculo sem consultar o banco de novo. Se uma etapa derivada falhar, o grafo continua válido. O texto pode conter dados sensíveis em comentários; por isso a varredura de segredos o inclui (ver [seguranca-operacao.md](seguranca-operacao.md)).

## D6. Um grafo por projeto, com perfis por sistema

**Contexto.** Mapear um segundo sistema (uma aplicação Zend Framework 1 que se integra ao e-Cidade) exigiu separar o que é do e-Cidade (marcador `libs/db_stdlib.php`, convenções de DAO, exclusões, módulos vindos do dicionário) do que é genérico.

**Escolha.** Tudo o que é específico de um sistema fica num *perfil*: marcador da pasta, extensões, exclusões, funções de infraestrutura, fábricas de DAO, desempate de classe por convenção, origem dos módulos e `search_path`. `ecidade` é o padrão; `zend1` cobre módulos por diretório, controllers/actions, views `.phtml` e entidades Doctrine. O perfil é escolhido explicitamente (a interface o sugere pelos marcadores; a CLI nunca adivinha). Cada projeto tem o próprio grafo e aparece num `projects.json`.

**Consequência.** Suportar um sistema novo é escrever um perfil (e, se preciso, um resolvedor) e não mexer em grafo, consultas ou interfaces. O custo é que o perfil de um sistema específico não é "Zend 1 genérico": ele incorpora o que foi medido no sistema real. Ver [perfis.md](perfis.md).

## D7. Superfície de comunicação e costura entre sistemas por ID de `Endpoint`

**Contexto.** Para estrangular um monólito é preciso saber por onde ele conversa: o que entra, o que sai, com que parâmetros e quem depende de quem. O e-Cidade serve SOAP declarado em XML, ações de RPC despachadas por `switch` e rotas do Laravel; o sistema vizinho chama os métodos SOAP por nome literal.

**Escolha.** O `Endpoint` é um nó de primeira classe, cujo ID nomeia **quem serve**. O servidor liga `SERVES` e o que o endpoint alcança (`QUERIES`, `INSTANTIATES`, `CALLS`); o cliente liga `CALLS_REMOTE` para o mesmo ID, que fica placeholder no grafo dele. Os parâmetros registrados são só os que o código declara (XML, assinatura, ramo do `case`). `php-map cross` faz o *join* dos bancos pelo ID e classifica cada endpoint como casado, chamado sem servidor ou servido sem cliente.

**Consequência.** A cadeia "tela do sistema A → método SOAP → endpoint do sistema B → tabela" é determinística e atravessa os dois grafos sem fundi-los. "Servido sem cliente" **não** significa código morto: portais e outros consumidores chamam por fora. Nomes de método que não são literais viram `Endpoint:…:?` com confiança 0,5.

## D8. Camadas do Laravel: da tela à tabela

**Contexto.** Em telas novas do e-Cidade, a cadeia real passa por vários elos que o grafo inicial não via: a tela carrega um JavaScript, o JavaScript chama uma rota da API, a rota vai a um controller que recebe um service por injeção de dependência, o service usa um repository, o repository instancia um model Eloquent, e o model aponta uma tabela.

**Escolha.** Cada elo ganhou uma ligação determinística: arquivos JavaScript viram nós `File` (só os citados por alguma tela); a nova aresta `CALLS_HTTP` (fonte `JS_URL`) casa URLs literais com as `Route`; a injeção de dependência é tratada como **tipagem** do receptor (a aresta continua `CALLS`); `MAPS_TO` liga model à tabela (1,0 com `$table` literal, 0,8 pela convenção do Laravel). Rotas e models entram no conjunto de arestas percorridas por `trace`, `impact` e pela subida até o menu. O leitor de JavaScript é léxico, sem Node.

**Consequência.** Dá para sair de um service do Laravel e chegar ao menu que o aciona, e descer de um menu até a tabela. Fica de fora o que depende de montagem em tempo de execução (URL concatenada em vários pedaços, `$table` herdado de classe externa) e o acesso por métodos do Eloquent (`->get()`, `->save()`), que não é classificado como leitura ou escrita. O acoplamento entre módulos ignora essas pontes para não inflar a métrica.

## D9. Fórmulas por extração determinística; a IA só redige

**Contexto.** Quem analisa um cálculo — por exemplo, o IPTU — quer ver cada fórmula, os campos com o nome de negócio e o menu em que cada dado-base é cadastrado: tabela → campo → fórmula → menu.

**Escolha.** Um extrator lê o texto da função e registra cada cálculo (destino, expressão, linha) e as entradas, cada uma levada até `tabela.campo` quando possível (parâmetro, variável, campo de registro de laço, função chamada). Só conta como fórmula atribuição com operador aritmético ou chamada de função de conta; `x := 0` e `x := y` não contam, mas alimentam a resolução. O dossiê desce às funções auxiliares até um limite declarado e é paginado. O que não se resolve vai para "não rastreado", com o motivo. Um assunto (juros, multa, correção, desconto…) é atribuído por regras fixas e apresentado como pista, com a origem à vista. Um índice por assunto, gravado no scan, permite buscar fórmulas e regras em toda a base em milissegundos.

**Consequência.** O relatório pode ser escrito em linguagem natural por um assistente, mas os fatos vêm do grafo. Muitas fórmulas ficam no assunto "outros", e o classificador por nome tem falsos positivos conhecidos; por isso a busca também casa pelo destino, rótulo e expressão.

## D10. MCP somente leitura, com cofre de acesso, login e túnel genérico

**Contexto.** Analistas consultam o grafo por meio de assistentes de IA. Clientes MCP (Claude Code, Cursor, VS Code) mandam um cabeçalho `Authorization: Bearer`; não assinam requisições. Os usuários querem acesso de fora da máquina sem amarrar o projeto a um serviço de túnel.

**Escolha.** O servidor MCP é somente leitura e roda por `stdio` (local) ou HTTP em `/mcp`, inclusive montado dentro do `php-map ui`. A autenticação é um **cofre de acesso**: um SQLite próprio, separado dos grafos, com permissão restrita, que guarda usuários (senha com `scrypt`), sessões e tokens (apenas o SHA-256, com validade, último uso e revogação com efeito imediato). A interface exige login sempre; há dois papéis, `admin` e `leitor`. O túnel é um **comando configurável**, executado sem *shell*, e só o `/mcp` sai por ele: a interface, o GraphQL, o scan e a publicação respondem 404 a qualquer pedido remoto. A ferramenta que lê código-fonte nasce desligada. Antes de expor, o programa confere que há token ativo e que a varredura de segredos dos grafos está limpa.

**Consequência.** Não há dependência de um fornecedor de túnel específico, e a revogação de um token vale na próxima chamada. Conectores que só aceitam OAuth não funcionam com o bearer fixo (autenticação OAuth/OIDC está no roteiro, fora do escopo atual). A superfície de ataque é pequena e documentada em [seguranca-operacao.md](seguranca-operacao.md).

## D11. Grafo mais recente automático e respostas paginadas no MCP

**Contexto.** O servidor MCP lia a configuração de projetos uma vez, na subida, e apontava para um `.db` fixo, enquanto a interface grava um arquivo novo a cada scan; os dossiês de análise chegavam a centenas de milhares de caracteres, estourando o contexto do cliente. Nomes de função com várias assinaturas geravam erro, mesmo quando as sobrecargas só repassavam a chamada com valores padrão.

**Escolha.** O catálogo do MCP relê o `projects.json` quando a data de modificação muda, e o `db` de um projeto pode ser uma pasta: vale o `.db` mais novo cujo resumo de scan já esteja ao lado (scan completo), reavaliado a cada consulta. Um resolvedor único trata nomes de função ambíguos: se todas as assinaturas convergem, pelas chamadas entre elas, a uma única assinatura que não repassa, ela é a escolhida, e a resposta diz que foi por repasse. Dossiês são paginados em fronteiras de seção (`page`, `pages`, `next_page`), com a estrutura JSON completa só sob pedido. Os prompts do MCP são **roteiros**: dizem à IA que ferramentas chamar, em que ordem e em que formato responder; nenhuma ferramenta é chamada ao montá-los.

**Consequência.** Um scan novo aparece no MCP sem reiniciá-lo, e respostas grandes cabem em uma conversa. A IA do cliente é quem faz as chamadas, o que mantém o servidor sem estado.

## D12. Um cache de AST por pasta de origem

**Contexto.** A extração do e-Cidade completo leva de 6 a 15 minutos; e as pessoas alternam entre a instalação real e uma cópia de teste, ou entre dois projetos, no mesmo diretório de saída.

**Escolha.** O cache fica em `<out>/ast/<nome>-<hash8>/`, uma pasta por origem, e o `meta.json` guarda a origem, a versão do formato e o hash do extrator. Um scan com `--reuse-ast` reextrai só os arquivos cujo sha1 mudou; mudar o extrator invalida tudo.

**Consequência.** Alternar entre pastas não reextrai nada depois da primeira extração de cada uma; um scan incremental leva alguns minutos, dominados por resolução e SQL. O custo é espaço em disco (centenas de megabytes por pasta mapeada) e o sha1 de todos os arquivos a cada scan, que com o disco frio pode demorar.

## D13. Somente leitura sobre o sistema mapeado

**Contexto.** O sistema legado é a fonte de verdade de uma operação em produção. Nada que a ferramenta faça pode alterá-lo, e ela não deve exigir privilégios que ninguém quer conceder.

**Escolha.** O código-fonte é lido, nunca escrito. O extrator roda num container descartável com o código montado só para leitura. Do banco, a ferramenta lê apenas metadados (dicionário de dados, menus, schema, texto de funções), em transação `REPEATABLE READ READ ONLY`, numa sessão com `default_transaction_read_only = on` conferida antes de ler; nenhum dado de negócio é lido nem gravado. O DSN nunca aparece no resumo, no log nem na interface. Os arquivos que a ferramenta grava recebem permissões restritas (arquivo 600, pasta 700).

**Consequência.** Dá para rodar sobre uma instalação de produção com um usuário de leitura de privilégio mínimo. Por não executar o sistema, a análise não vê comportamento que só existe em execução; esse limite é explícito nas pendências e nos limites de [precisao.md](precisao.md).
