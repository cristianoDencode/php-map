# Changelog

Todas as mudanças relevantes deste projeto ficam aqui. O formato segue o [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e o projeto usa [versionamento semântico](https://semver.org/lang/pt-BR/).

## [Não lançado]

## [0.1.0] - 2026-11-18

Primeira versão pública.

### Adicionado
- **Scan** de sistemas PHP legados: extração por AST do PHP (container Docker com PHP 7.4), leitura de SQL e PL/pgSQL, catálogo de dados e menus do e-Cidade, e resolução determinística de includes, chamadas, classes, rotas e RPCs. Toda aresta do grafo carrega `source` e `confidence`.
- **Grafo** em SQLite com busca textual (FTS5) e agregados de acoplamento por módulo.
- **Perfis** `ecidade` (PHP procedural, DAOs, Laravel 8) e `zend1` (Zend Framework 1 com Doctrine).
- **Consultas:** `trace` (da tela até as tabelas), `impact` (o que uma tabela alcança), `search`, `boundary` (plano de corte e candidatos à extração), `layers`, `cross` (costura entre sistemas) e `value-origin` ("de onde vem este valor?").
- **Regras de negócio, fórmulas e campos** lidos do texto das funções da base e do código PHP, sem IA, com busca por assunto.
- **Comunicação entre sistemas:** endpoints SOAP, RPC e rotas HTTP, com parâmetros, quem implementa e quem chama.
- **Interface web** (React) sobre uma API GraphQL somente leitura, com login obrigatório por e-mail e senha (primeiro acesso com código de uso único) e papéis `admin` e `leitor`.
- **Servidor MCP** somente leitura (stdio e HTTP), com tokens, túnel genérico e prompts de relatório.
- Comandos `php-map scan | trace | impact | search | boundary | layers | cross | value-origin | index-formulas | ui | mcp | user | token | setup-extractor`.

[Não lançado]: https://github.com/cristianoDencode/php-map/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/cristianoDencode/php-map/releases/tag/v0.1.0
