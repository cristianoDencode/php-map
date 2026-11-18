# Segurança e operação

Este documento descreve o que o `php-map` guarda, como protege o que guarda e o que quem opera a ferramenta precisa fazer por conta própria. Para reportar uma vulnerabilidade, veja [`SECURITY.md`](../SECURITY.md).

## Modelo de ameaça em uma página

O `php-map` roda na máquina de quem mapeia o sistema. Ele lê o código-fonte e, opcionalmente, metadados do banco; grava grafos e caches em disco; e serve uma interface e um servidor MCP. Os riscos que importam:

1. **Vazamento do grafo.** O grafo é um retrato da arquitetura e do texto do sistema mapeado. Quem o lê entende o sistema por dentro.
2. **Segredos escritos no código.** Comentários e literais do código podem conter senhas, tokens ou chaves, e o grafo guarda esses textos para a busca.
3. **Acesso indevido à interface ou ao MCP**, sobretudo quando expostos por um túnel.
4. **Alteração do sistema mapeado ou do banco dele.** A ferramenta é desenhada para nunca fazer isso.

Fora do escopo: proteger o sistema mapeado em si e proteger a máquina de quem já tem acesso de administrador a ela.

## O que o grafo contém e o que não contém

**Contém:**

- A estrutura do código: arquivos, classes, funções, métodos, chamadas, includes, rotas, endpoints e as arestas entre eles, com `arquivo:linha` como evidência.
- Textos do código usados na busca: comentários, literais com frase, HTML e mensagens de JavaScript.
- O schema do banco: tabelas, colunas, funções, triggers.
- O **texto das funções da base** (PL/pgSQL e SQL), como o banco o devolve, quando o scan lê o schema do banco.
- O dicionário de dados (rótulos e descrições de tabelas e colunas) e os menus do sistema, quando o scan lê o catálogo.

**Não contém:**

- Linhas de tabelas de negócio: nenhum dado de contribuinte, servidor, fornecedor ou financeiro. O scan lê só tabelas de sistema (dicionário e menus) e metadados do catálogo do PostgreSQL.
- A string de conexão. O DSN é lido do ambiente ou do `.env`, nunca é gravado no grafo, no resumo do scan, no log nem na interface.
- Credenciais de usuários da ferramenta: ficam num cofre à parte (ver abaixo).

Atenção ao segundo item da lista de riscos: **o fato de o grafo não ter dados de produção não o torna inofensivo.** Trate-o como material sensível do projeto mapeado.

## Somente leitura sobre o sistema mapeado

- **Código-fonte.** O scan só lê arquivos. O extrator PHP roda num container descartável que monta a pasta do sistema em leitura; nada é escrito nela.
- **Banco de dados.** As leituras (catálogo e schema) acontecem numa transação `READ ONLY` com `default_transaction_read_only=on`, conferida antes de ler. Use, sempre que possível, um usuário do PostgreSQL com permissão apenas de `SELECT` nas tabelas de sistema e no catálogo.
- **Grafos servidos.** A interface e o MCP abrem os grafos em modo somente leitura. A única escrita do MCP, além do log, é o cofre de acesso (tokens), em outro arquivo.
- **Leitura de código pelo MCP.** A ferramenta `source` é opcional, bloqueia arquivos de configuração e chaves e, no MCP servido pela interface, vem desligada (ver [mcp.md](mcp.md#leitura-do-código-fonte)).

## Varredura de segredos

`python -m php_map.secret_scan <banco.db>` varre o grafo em busca de padrões de credencial: atribuição de valor literal a nomes como `senha`, `password`, `token` ou `api_key`, DSN com senha, chaves privadas, JWT, chaves da AWS e strings de alta entropia perto dessas palavras. O valor **nunca é impresso**: só os quatro primeiros caracteres e `***`, mais um identificador estável do achado. Sai com código 3 se houver achado fora da lista de exceções.

Um achado já revisado por uma pessoa entra na lista de exceções (`src/php_map/secret_allowlist.txt`), uma linha por achado, com justificativa obrigatória. A varredura roda automaticamente antes de abrir o túnel do MCP e antes de mostrar um briefing para um assistente de IA.

## Arquivos em disco

Todo arquivo que o programa grava (grafos, cache de AST, cofre, configurações) nasce com permissão `600` e toda pasta nova com `700`, por um `umask` aplicado na partida. Arquivos já existentes e reescritos no lugar são corrigidos.

No **Windows**, `chmod` não restringe nada. O programa avisa uma vez e a proteção fica por conta das permissões da pasta (ACL do NTFS) e da criptografia de disco.

Recomendações:

- **Criptografe o disco** onde ficam `out/` e o repositório (LUKS, BitLocker, FileVault). É a defesa mais barata contra perda ou furto da máquina.
- Mantenha `out/` e o `.env` fora de backups sem criptografia e fora de pastas sincronizadas com serviços de nuvem.
- Não versione `out/`, grafos (`*.db`), `.env` nem o cofre. O `.gitignore` do repositório já os exclui; confira o seu.

## Cofre de acesso, login e papéis

O cofre é um SQLite separado dos grafos (`out/access/access.db`, ou `PHP_MAP_ACCESS_DB` / `--access-db`). Ele guarda usuários, sessões e tokens do MCP. Assim, trocar de grafo ou refazer um scan não afeta o acesso.

| Item | Como é protegido |
|---|---|
| Senhas | `scrypt` da biblioteca padrão, sal aleatório por senha, parâmetros no próprio hash (dá para endurecer depois sem invalidar as senhas). Mínimo de 10 caracteres |
| Tokens do MCP | 32 bytes aleatórios com o prefixo `ecm_`; só o SHA-256 fica no cofre. O valor é mostrado uma única vez. Validade padrão de 90 dias, revogação imediata, registro de último uso e IP |
| Sessões | Identificador aleatório guardado só como hash; o cookie leva o identificador e um HMAC-SHA256 com chave gerada no primeiro uso, e um cookie adulterado é descartado antes de qualquer consulta |
| Cookie | `HttpOnly`, `SameSite=Strict` e `Secure` quando o pedido chega por HTTPS. Sessão expira com 12 horas de inatividade ou 7 dias no máximo |
| Tentativas | Cinco senhas erradas bloqueiam o usuário por 15 minutos; há também um limite por IP. A mensagem de erro é a mesma para usuário inexistente e senha errada |

**Login obrigatório.** A interface nunca funciona sem login. Na primeira execução, sem usuário no cofre, o `php-map ui` imprime no terminal um **código de configuração** de uso único (só em memória, novo a cada reinício). A página mostra apenas a tela de configuração inicial, onde o código, um **e-mail** e uma senha criam o primeiro administrador (o e-mail é o login de todos os usuários; ele é guardado em minúsculas e o login não diferencia maiúsculas), e a API responde 401 até lá. Se o cofre não abrir, o servidor recusa subir. A opção `--no-auth` existe só para desenvolvimento: exige `--host 127.0.0.1`, avisa em vermelho e não serve o MCP.

**Papéis.**

| Papel | Pode |
|---|---|
| `admin` | Tudo: consultar, rodar scans, trocar a pasta do sistema, gerenciar projetos, tokens, usuários, MCP e túnel |
| `leitor` | Todas as consultas, sem mudar nada |

O servidor recusa toda mutação da API de um `leitor`; a interface esconde as ações restritas. Uma mutação nova nasce restrita ao administrador por padrão. O primeiro usuário é sempre `admin`, os seguintes são `leitor` salvo `--role`, e o último administrador ativo não pode ser desativado nem rebaixado. Papéis pela linha de comando: `php-map user add <e-mail> --role admin|leitor` e `php-map user role <e-mail> <papel>`. Não há envio de e-mail nem "esqueci a senha": quem perdeu a senha pede ao administrador (tela Acesso ou `php-map user passwd <e-mail>`). Cofres antigos, com usuários que não são e-mail, continuam entrando; só a criação de usuário exige o formato.

`--read-only` em `php-map ui` desliga também as ações que mudam estado (rodar scan, escolher pasta), mas **não** desliga o login.

## Rede: host local, HostGate e túnel

- **Interface só local.** A interface atende `127.0.0.1` / `localhost`. Um porteiro de `Host` (HostGate) recusa qualquer outro `Host` que não esteja na lista de locais e públicos configurados, o que protege contra DNS rebinding.
- **Só o `/mcp` sai pelo túnel.** Uma requisição é *remota* quando o `Host` é público (`--public-host`) ou quando traz `X-Forwarded-For`, `X-Forwarded-Host` ou `Forwarded`. Requisição remota para qualquer caminho que não seja `/mcp` recebe 404. Esses cabeçalhos podem ser forjados por quem chega direto em localhost, mas isso só deixa a requisição *mais* restrita, nunca menos.
- **O `/mcp` só aceita o bearer do cofre**, nunca o cookie de login.
- **Antes de abrir o túnel**, o programa exige ao menos um token ativo, roda a varredura de segredos e testa pela URL pública que `POST /mcp` sem token responde 401 e `GET /graphql` responde 404. Só então mostra `no ar`.
- **HTTPS.** O túnel (ngrok, cloudflared) termina o TLS com certificado válido. Sem túnel, não exponha a porta da interface nem do MCP diretamente à rede: o servidor não fala TLS. Se precisar de acesso na rede interna, ponha um proxy reverso com HTTPS na frente e informe o nome com `--public-host` ou `--allowed-host`.
- O túnel é um comando executado **sem shell**; segredos como o authtoken entram só como nomes de variável em `tunnel.json` e o log é mascarado (inclusive qualquer `ecm_…`).

## Backup e retenção

- **Reconstruível:** grafos (`*.db`) e cache de AST se refazem com um novo scan. Não precisam de backup; precisam de proteção enquanto existirem.
- **Insubstituível:** o cofre (`access.db`) e a configuração (`projects.json`, `tunnel.json`, `ui-settings.json`). Faça backup do cofre **criptografado** e com a mesma permissão restrita. Restaurar um cofre antigo ressuscita tokens já revogados e usuários desativados depois da cópia; revise-os após restaurar.
- Apague grafos e caches de projetos que não usa mais. Cada cópia do grafo é uma cópia do retrato do sistema.
- Rode `php-map token list` periodicamente e revogue o que não usa mais. Prefira validade curta.

## O que fazer e o que não fazer

**Faça:**

- Use um usuário do banco só de leitura, com acesso apenas ao que o scan precisa.
- Gere um token por pessoa ou máquina, com nome claro e validade.
- Rode a varredura de segredos depois de cada scan novo se pretende expor o MCP.
- Mantenha o programa e as dependências atualizados; o repositório usa verificação automática de dependências.
- Reporte falhas em privado, como descrito em [`SECURITY.md`](../SECURITY.md).

**Não faça:**

- Não aponte o `scan` para uma base de produção com um usuário que possa escrever.
- Não comite o `.env`, grafos, o cofre nem a saída do `scan`.
- Não exponha a interface (porta 8765) diretamente na internet, nem use `--host 0.0.0.0` sem um proxy autenticado na frente.
- Não use `--insecure-no-auth` nem `--no-auth` fora de uma máquina de desenvolvimento só sua.
- Não compartilhe um token entre várias pessoas nem o cole em canais de chat e tickets.
- Não ligue a leitura de código-fonte pelo MCP sem ter conferido que o sistema mapeado não guarda segredos em arquivos fora da lista de bloqueio.
- Não copie grafos para serviços de terceiros (armazenamento na nuvem, assistentes de IA hospedados) sem decidir conscientemente que o conteúdo pode sair da sua organização. O que um cliente MCP lê vira contexto do assistente que ele usa.

Mais sobre o desenho: [arquitetura.md](arquitetura.md) e [mcp.md](mcp.md).
