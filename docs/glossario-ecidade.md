# Glossário do e-Cidade

Termos do domínio de gestão pública municipal e do código do e-Cidade que aparecem nos grafos, nas buscas e nas respostas do MCP. A fonte confiável para qualquer termo de negócio é o próprio sistema: o dicionário de dados (`db_sys*`) e os menus (`db_itensmenu`) dizem o que cada tabela, campo e tela significa. Itens marcados *a confirmar* são hipóteses de uso comum que dependem da instalação.

## Ciclo da despesa pública

| Termo | Significado |
|---|---|
| Orçamento / Dotação | Crédito autorizado na lei orçamentária anual (LOA) para uma despesa. "Saldo da dotação" é quanto ainda pode ser empenhado |
| Empenho | Reserva formal da dotação para uma despesa específica (1ª fase da despesa) |
| Liquidação | Verificação de que o bem ou serviço foi entregue; reconhece a obrigação (2ª fase) |
| Pagamento / Ordem de pagamento | Saída financeira (3ª fase) |
| Anulação / Estorno | Reversão parcial ou total de empenho, liquidação ou pagamento |
| Restos a pagar | Empenhos não pagos até o fim do exercício |
| Exercício | Ano fiscal |
| Licitação | Processo de compra pública (pregão, concorrência…) |
| Acordo / Contrato / Aditamento | Contrato firmado e suas alterações (prazo, valor, quantidade) |
| Receita / Arrecadação | Entrada de recursos (tributos, taxas) |
| Dívida ativa | Créditos tributários não pagos, inscritos para cobrança |
| IPTU / ISSQN / ITBI | Impostos municipais sobre imóvel, serviços e transmissão de imóveis |
| Conciliação bancária | Batimento entre extrato bancário e lançamentos contábeis |
| CGM | Cadastro geral do município: pessoas físicas e jurídicas usadas por todos os módulos |

## Módulos e prefixos de arquivo

As telas da raiz do e-Cidade começam por um prefixo de três letras que costuma indicar o módulo. É uma **tendência**, não uma regra: o módulo oficial de uma tela é o do menu (`db_modulos`, `db_itensmenu`). Quando o menu não classifica o arquivo, o `php-map` usa o prefixo como regra de nome, com confiança menor que 1 (fonte `NAMING_RULE`).

| Prefixo | Módulo |
|---|---|
| `con` | Contabilidade |
| `pes` | Pessoal / folha de pagamento |
| `edu` | Educação |
| `cai` | Caixa / tesouraria |
| `orc` | Orçamento |
| `sau` | Saúde |
| `emp` | Empenho (despesa) |
| `iss` | ISSQN |
| `fis` | Fiscalização |
| `com` | Compras |
| `pro` | Protocolo |
| `pat` | Patrimônio |
| `alm` | Almoxarifado |
| `agu` | Água e esgoto |
| `div` | Dívida ativa |
| `far` | Farmácia |
| `mer` | Merenda escolar |
| `arr` | Arrecadação |
| `lab` | Laboratório (saúde) |
| `vei` | Veículos / frota |
| `jur` | Jurídico |
| `lic` | Licitação |
| `aco` | Acordos / contratos |
| `cem` | Cemitério |
| `itb` | ITBI |
| `bib` | Biblioteca |
| `soc` | Assistência social |
| `eso` | eSocial |
| `ouv` | Ouvidoria |
| `tfd` | Tratamento fora do domicílio (saúde) |
| `vac` | Vacinas |
| `sys` | Sistema / configuração |

Outros prefixos (`cad`, `mat`, `rec`, `ate`, `hab`, `pre`, `pla`, `tra`, `amb`, `cus`) existem, mas o módulo a que se ligam fica *a confirmar* na instalação em uso: consulte o menu.

## Convenção de nome das telas

O padrão é `<mod><tipo>_<objeto><seq>.php`, por exemplo `emp4_anularempenho001.php`.

- `<mod>`: prefixo do módulo (três letras; a caixa varia).
- `<tipo>`: um dígito que indica, como tendência, a natureza da tela: `1` cadastros, `2` relatórios, `3` consultas, `4` procedimentos. Não use o dígito sozinho para classificar uma tela; o menu é a fonte.
- `<seq>`: em cadastros, `001` costuma ser incluir, `002` alterar e `003` excluir (o formulário reaproveita o mesmo arquivo de campos com uma opção diferente). Em relatórios, `001` costuma ser o formulário de parâmetros e `002` a impressão.
- Sufixos especiais: `*.RPC.php` (endpoint AJAX/JSON), `*.model.php` (modelo de domínio), `*.classe.js` (classe JavaScript), `func_*.php` (janela de pesquisa ou *lookup*).

## Termos do código

| Termo | Significado |
|---|---|
| Tela | Arquivo PHP da raiz que mistura PHP, HTML e JavaScript e é aberto por um item de menu |
| DAO (`cl_*`) | Classe gerada por tabela em `classes/db_<tabela>_classe.php` (`class cl_<tabela>`), com `incluir`, `alterar`, `excluir` e `sql_query*`. O SQL de gravação vive dentro dela. O `php-map` liga o `new cl_x` à tabela `x` |
| `db_utils::getDao('x')` / `usarDao('x')` | Fábricas que instanciam o DAO `cl_x` por `eval`, invisíveis na AST. O perfil as declara (ver [perfis.md](perfis.md)) |
| `modification('arquivo')` | Função que envolve quase todo `require`/`include`. Aplica patches XML de extensão e, sem patch, devolve o próprio caminho. O `php-map` desembrulha a chamada para achar o arquivo real |
| Autoloader | `libs/db_autoload.php` carrega classes sem `require`: `cl_x` vai para `classes/db_x_classe.php`; as demais, para `X.model.php`, `X.service.php` ou `X.interface.php` |
| RPC | Arquivo `*.RPC.php` que recebe JSON e despacha por `switch ($oParam->exec)` ou `switch ($objJson->method)`. Cada `case` é uma ação |
| `db_postmemory` | Transforma campos do `$_POST` em variáveis globais |
| `db_query` | Função de acesso ao banco, declarada em mais de uma biblioteca; é infraestrutura no grafo |
| `fc_*` | Funções PL/pgSQL do banco que concentram regras de negócio (`fc_calcula`, `fc_juros`, `fc_dotacaosaldo`…). `fc_debug`, `fc_getsession` e `fc_putsession` são infraestrutura |
| Auditoria (`db_acount*`) | Tabelas e funções geradas que registram alterações; aparecem em quase todo DAO e são tratadas como infraestrutura para não poluir o `trace` e o `impact` |
| `forms/db_frm*.php` | Formulários incluídos por várias telas; a tela instancia o DAO e o form o usa |
| Rótulo (`$clrotulo->label`) | Texto de tela de um campo, lido do dicionário em tempo de execução |
| Schema do módulo | As tabelas de negócio ficam em schemas por módulo (`empenho.empempenho`, `protocolo.cgm`); o SQL do PHP as chama sem schema e o `search_path` resolve |

## Dicionário de dados e menus (`db_sys*`, `db_itensmenu`)

O e-Cidade descreve a si mesmo em tabelas de sistema do schema `configuracoes`. O `php-map` lê só estas (somente leitura) para montar módulos, rótulos e menus.

| Tabela | O que guarda |
|---|---|
| `db_sysarquivo` | Tabelas do dicionário: nome, descrição e a *sigla* (prefixo das colunas, ex.: `e60`) |
| `db_syscampo` | Colunas: nome e rótulo de tela |
| `db_sysarqcamp` | Liga tabela a colunas |
| `db_sysmodulo` | Módulos do dicionário |
| `db_sysarqmod` | Liga tabela a módulo |
| `db_modulos` | Módulos do menu |
| `db_itensmenu` | Itens de menu: descrição e o arquivo da tela (`funcao`, às vezes com `?parâmetros`) |
| `db_menu` | A árvore do menu: quem é filho de quem, em cada módulo |

A sigla das colunas ajuda a ler o SQL: `e60_numemp` é a coluna `numemp` da tabela cuja sigla é `e60` (`empempenho`). Poucas siglas são compartilhadas por várias tabelas (por exemplo, as de arrecadação).

Os módulos do dicionário e os do menu às vezes têm nomes diferentes para a mesma coisa (`divida` e `dividaativa`, por exemplo). O perfil mantém uma tabela de equivalência para uni-los; o resumo do scan lista os que sobraram de um lado só, sinal para revisar a tabela.

## Termos do `php-map` que aparecem junto

| Termo | Significado |
|---|---|
| `source` | De onde a aresta veio (AST do PHP, SQL, catálogo, regra de nome…); ver [ontologia.md](ontologia.md) |
| `confidence` | Confiança da aresta: 1.0 é determinística; menos que isso é hipótese |
| Candidato a extração | Módulo com baixo custo de corte, segundo acoplamento e tabelas compartilhadas |
| Costura | O ponto em que dois sistemas se encontram: um endpoint servido por um e chamado pelo outro |

Para as fontes e a precisão do mapeamento, veja [precisao.md](precisao.md).
