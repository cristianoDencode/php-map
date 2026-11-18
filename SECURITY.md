# Política de segurança

## Versões suportadas

O php-map está em fase inicial (`0.x`). Só a versão mais recente recebe correções de segurança.

| Versão | Suportada |
|---|---|
| `0.1.x` | Sim |
| Anteriores | Não |

## Como reportar uma vulnerabilidade

**Não abra uma issue pública.** Use o relato privado do GitHub:

1. Abra a aba **Security** do repositório.
2. Escolha **Report a vulnerability** (GitHub Security Advisories).
3. Descreva o problema, os passos para reproduzir, a versão e o impacto que você enxerga. Não anexe dados reais de um sistema em produção.

Você receberá uma resposta de que o relato foi lido em até **7 dias**. Os mantenedores avaliam, corrigem e combinam com você a divulgação. O prazo habitual é de até 90 dias entre o relato e a divulgação pública, e o seu nome entra nos créditos se você quiser.

## Modelo de ameaça (resumo)

O php-map lê o código-fonte e o dicionário de dados de um sistema legado e grava um grafo local. O que a ferramenta assume e protege:

- **Somente leitura sobre o sistema mapeado.** Nenhum comando altera arquivos ou dados do sistema analisado. O banco, quando usado, é lido só para o dicionário de dados, os menus, o schema e o texto das funções, numa sessão somente leitura.
- **O grafo não tem dados de produção**, mas tem a estrutura do sistema e trechos de código. Trate o arquivo `.db` como informação sensível da organização dona do código.
- **A interface web exige login sempre.** O servidor só atende `localhost` por padrão; o acesso remoto é só pelo servidor MCP (`/mcp`), com token com validade e revogação.
- **Segredos no código:** o scan verifica o grafo à procura de senhas, chaves e certificados antes de qualquer exposição pelo túnel. A leitura de código-fonte pelo MCP vem desligada e bloqueia arquivos de configuração e chaves.
- **Cofre de acesso:** senhas com `scrypt`, tokens guardados só como SHA-256, arquivo com permissão `600`.

Fora do modelo: um atacante com acesso ao sistema de arquivos da máquina que roda o php-map, ou com a senha de um administrador. Para operar com segurança (criptografia de disco, backup, túnel e HTTPS), veja [docs/seguranca-operacao.md](docs/seguranca-operacao.md).

## Boas práticas para quem usa

- Nunca exponha a interface web diretamente na internet.
- Crie tokens do MCP com a menor validade que atenda e revogue os que não usa mais.
- Mantenha o `.env` e os grafos fora do controle de versão (o `.gitignore` do projeto já os ignora).
