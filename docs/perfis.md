# Perfis de projeto

Um **perfil** reúne tudo o que muda de um sistema legado para outro: como reconhecer a pasta, quais arquivos ler e quais ignorar, em que encoding estão, como o código carrega classes, de onde vem o módulo de cada arquivo e por onde o sistema conversa com o mundo. O núcleo do `php-map` (extração, resolução, grafo, análises) é o mesmo para todos; o perfil só informa o que é específico.

Há dois perfis embutidos:

| Perfil | Sistema | Módulo vem de | Encoding |
|---|---|---|---|
| `ecidade` (padrão) | e-Cidade: PHP procedural, DAOs `cl_*`, Laravel 8 e funções PL/pgSQL | Dicionário de dados e menus do banco | ISO-8859-1 |
| `zend1` | Aplicações PHP com Zend Framework 1 (módulos em `application/modules/`, Doctrine 1) | A pasta do módulo | UTF-8 |

O contrato está em [`src/php_map/profiles/base.py`](../src/php_map/profiles/base.py) (`ProjectProfile`); as instâncias, em [`ecidade.py`](../src/php_map/profiles/ecidade.py) e [`zend1.py`](../src/php_map/profiles/zend1.py).

## Como o perfil é escolhido

- **Linha de comando:** `php-map scan --source <pasta> --profile <nome>`. O padrão é `ecidade`. A CLI nunca adivinha: pasta que não tem o marcador do perfil escolhido é recusada com a mensagem do arquivo que faltou.
- **Interface:** em **Projetos**, o botão *Sugerir o perfil* confere o marcador de cada perfil na pasta informada. Se exatamente um casa, ele é sugerido; se nenhum ou mais de um casa, a pessoa escolhe. A escolha fica gravada no `projects.json` do projeto.
- O perfil também define o `Endpoint` que o sistema serve (campo `endpoint_system`), o que mantém o ID dos endpoints igual nos grafos dos dois lados de uma integração (ver [ontologia.md](ontologia.md)).

## O que um perfil define

| Campo | Para quê |
|---|---|
| `markers` | Arquivos que identificam a pasta. O primeiro é exigido pelo `scan`; os demais só confirmam a sugestão da interface |
| `source_extensions` | Extensões lidas (`.php`; o `zend1` também lê `.phtml`) |
| `exclude_dirs` e `third_party_dirs` | Pastas ignoradas (`vendor`, `node_modules`…) e pastas de terceiros, cujos arquivos entram no grafo marcados como infraestrutura |
| `encoding` | Como decodificar os fontes antes de gerar JSON em UTF-8 |
| `infra_*` | Funções, arquivos, funções de banco e prefixos de tabela que são infraestrutura (sessão, conexão, auditoria) e ficam fora das métricas e do `trace` por padrão |
| `dao_factories`, `dao_write_methods` | Fábricas que instanciam DAOs por `eval` (invisíveis na AST) e os métodos do DAO que gravam o objeto |
| `class_resolvers` | Desempate de classes ambíguas por convenção de nomes |
| `module_source` | `catalog` (módulo do dicionário e do menu do banco) ou `directory` (a pasta é o módulo) |
| `module_aliases` | Equivalência entre nomes de módulo do dicionário e do menu |
| `default_search_path` | `search_path` do PostgreSQL usado para resolver tabelas sem schema quando o schema vem de arquivo |
| `endpoint_system`, `ws_xml`, `remote_calls`, `soap_server_classes`, `rpc_endpoints` | Superfície de comunicação: o que o sistema serve e como chama outros |
| `laravel_routes`, `reads_eloquent`, `reads_doctrine` | Leitores opcionais: rotas do Laravel, models Eloquent, entidades Doctrine |

## Perfil `ecidade`

**Reconhecimento.** A pasta precisa ter `libs/db_stdlib.php`.

**Arquivos lidos.** `.php`, em ISO-8859-1. Ignora `vendor/`, `node_modules/`, `tmp/`, `storage/` e `.git/`. Arquivos JavaScript só entram no grafo quando alguma tela PHP os carrega.

**Infraestrutura.** Funções de acesso e sessão (`db_query`, `db_getsession`, `db_postmemory`, `modification`…), arquivos de bootstrap (`libs/db_stdlib.php`, `libs/db_conecta.php`, `libs/db_autoload.php`…), a função de banco `fc_debug` e as tabelas de auditoria `db_acount*` são infraestrutura. Sem essa marcação, elas dominariam qualquer métrica de centralidade.

**Resolução de classes.** O e-Cidade carrega muitas classes sem `require`, por um autoloader. O perfil reproduz as regras dele em ordem: `cl_x` vai para `classes/db_x_classe.php`; as demais, para `X.model.php`, depois `X.service.php`, depois `X.interface.php`. Quando nenhuma regra decide, a chamada fica ambígua (`PHP_AST_AMBIGUOUS`, confiança `1/n`) e vira pendência.

**DAOs.** Duas fábricas instanciam o DAO por `eval`: `db_utils::getDao('x')` e `$this->usarDao('x', true)`. O perfil as declara para o grafo ligar a chamada à classe `cl_x` e, por ela, à tabela. Os métodos `incluir` e `alterar` são os que gravam os campos preenchidos no objeto.

**Módulos e menus.** O módulo vem do dicionário de dados (`db_sysmodulo`, `db_sysarqmod`) e dos menus (`db_modulos`, `db_itensmenu`, `db_menu`), lidos do banco ao vivo (somente leitura) ou de arquivos SQL. Os módulos do dicionário e do menu às vezes têm nomes diferentes para a mesma coisa; `module_aliases` os une. Onde o catálogo não classifica um arquivo, uma regra de nome (voto do prefixo da tela, ou DAO → tabela) atribui módulo com confiança menor que 1.

**Schema.** Tabelas de negócio ficam em schemas por módulo (`empenho.empempenho`) e o PHP as chama sem schema; o `search_path` resolve. Com `--schema-db`, vale o `search_path` da sessão; com `--schema <arquivos>`, o do perfil.

**Comunicação.** Webservices SOAP declarados em XML (`webservices/consulta_dados/requisicoes/` e `webservices/processamento_dados/requisicoes/`, um endpoint por arquivo, com parâmetros e alvo), arquivos `*.RPC.php` que despacham por `switch ($oParam->exec)` (um endpoint por `case`) e rotas do Laravel declaradas em `routes/`. Chamadas de saída para o sistema parceiro são reconhecidas pelo helper de integração.

## Perfil `zend1`

**Reconhecimento.** A pasta precisa ter `application/configs/application.ini`; `application/Bootstrap.php` confirma.

**Arquivos lidos.** `.php` e `.phtml`, em UTF-8. Exclui `vendor`, `node_modules`, `tmp`, `storage`, `logs`, `docs`, `migrations` e as pastas de terceiros que ficam na raiz de `library/` (`Zend`, `Doctrine`, `FPDF`, `MPDF54`, `Twitter`, `xmlseclibs-master`, `ruckusing-migrations`). A exclusão é só pelo caminho a partir da raiz: o `library/` de cada módulo é código próprio e continua lido.

**Módulo por diretório.** Cada pasta de `application/modules/` é um módulo, com a aresta `BELONGS_TO` de confiança 1.0 (fonte `ZEND1_RESOLVER`): aqui a estrutura *é* o módulo, não uma heurística. A lista de módulos conhecidos evita inventar módulo a partir de prefixos que por acaso têm `_` (`Zend_Controller_…`).

**Resolução de classes.** Quatro convenções, nesta ordem:

1. `<Modulo>_<Resto>` com módulo conhecido: `Model`, `Form` e `Lib` vão para `models/`, `forms/` e `library/`; sufixo `Controller` vai para `controllers/`; senão tenta `models/` e a raiz do módulo.
2. Namespaces de `autoloadernamespaces.*` do `application.ini` (lido da própria pasta escaneada), com reserva para as bibliotecas comuns (`Zend_`, `Doctrine_`…).
3. Classes com namespace PHP (`Financeira\ArquivoAlerta`) vão para `application/entidades/`.
4. `<appnamespace>_Resto` vai para `application/Resto.php`.

**Rotas e telas.** Cada action de controller vira uma `Route` (`<modulo>/<controller>/<action>`, em kebab-case) ligada ao método por `ROUTES_TO`, e o `.phtml` da view é ligado por `RENDERS`.

**Persistência.** O perfil lê as anotações do Doctrine 1 (`@Entity`, `@Table`, `@Column`) e traduz DQL em strings: entidade → tabela vira `MAPS_TO`. SQL em strings é tratado como no `ecidade`.

**Comunicação.** Servidores SOAP publicados por `setClass('X')` viram um endpoint por método público de `X`; chamadas estáticas a um cliente de webservice com o nome do método no primeiro argumento viram `CALLS_REMOTE` para o endpoint do sistema que o serve.

**Sem catálogo.** Esse perfil não tem dicionário de dados nem menus no banco; o módulo vem da pasta e o schema, de `--schema-db` ou de `--schema`.

## Cruzando dois projetos

Quando dois sistemas se integram por SOAP, o `Endpoint` é identificado por `<sistema que serve>:<protocolo>:<nome>`. Como o ID é o mesmo nos dois grafos, `php-map cross` e a página **Comunicação** ligam quem chama a quem implementa sem heurística. Veja a seção de comunicação em [arquitetura.md](arquitetura.md).

## Criar um perfil novo

Um perfil novo é código, não configuração: o contrato é uma classe de dados mais, quando o sistema pede, resolvedores específicos.

1. **Meça o sistema antes de escrever.** Conte arquivos por extensão, ache o marcador mais estável (um arquivo de configuração que só esse tipo de projeto tem), liste as pastas de terceiros e o encoding real dos fontes.
2. **Crie `src/php_map/profiles/<nome>.py`** com uma constante `PROFILE = ProjectProfile(...)`, no molde de [`zend1.py`](../src/php_map/profiles/zend1.py). Preencha pelo menos `name`, `markers`, `source_extensions`, `exclude_dirs` e `encoding`.
3. **Registre** o perfil em `_registry()` de [`base.py`](../src/php_map/profiles/base.py). É esse registro que faz `--profile <nome>` e a sugestão da interface o enxergarem.
4. **Resolução de classes.** Se o framework carrega classes por convenção de nome, implemente `ClassResolver` (`resolve` e `expected_paths`) e liste em `class_resolvers`. Quando a convenção depende de um arquivo da própria pasta (como o `application.ini`), use `class_resolvers_factory`, que recebe a pasta escaneada.
5. **Módulos.** Use `module_source="directory"` se a estrutura de pastas define o módulo; com `"catalog"`, o sistema precisa ter dicionário e menus lidos do banco.
6. **Comunicação.** Declare `endpoint_system`, e conforme o caso `ws_xml`, `remote_calls`, `soap_server_classes` ou `rpc_endpoints`.
7. **Resolvedores próprios** (rotas, views, ORM) ficam em `src/php_map/resolve/`, como `zend1.py`, e são ligados no pipeline pela capacidade do perfil, não pelo nome dele.
8. **Fixtures sintéticas.** Monte uma pasta pequena em `tests/fixtures/<nome>-mini/` com arquivos inventados que exercitam cada regra do perfil (uma classe por convenção, uma rota, uma entidade, um endpoint). Nunca copie código de um sistema real para as fixtures. Escreva o gabarito esperado à mão e não o ajuste só para um teste passar.
9. **Testes.** Siga o padrão de [`tests/unit/test_profiles_zend1.py`](../tests/unit/test_profiles_zend1.py) (campos do perfil e resolvedores), [`tests/unit/test_resolve_zend1.py`](../tests/unit/test_resolve_zend1.py) (resolução sobre a fixture).
10. **Documente** o perfil neste arquivo e rode os gabaritos de precisão ([precisao.md](precisao.md)).

Se o perfil precisar de uma aresta, nó ou fonte novos, isso muda o contrato do grafo: atualize [ontologia.md](ontologia.md) e os testes de ontologia no mesmo pedido de mudança. O guia geral de contribuição está em [`CONTRIBUTING.md`](../CONTRIBUTING.md).
