# Como contribuir

Obrigado pelo interesse no **php-map**. Este guia mostra como montar o ambiente, rodar as verificações e propor mudanças. Antes de começar, leia a [visão geral da arquitetura](docs/arquitetura.md) e o [código de conduta](CODE_OF_CONDUCT.md).

## Ambiente

Você precisa de Python 3.11 ou mais novo (o CI testa 3.12 e 3.14), Docker (só para o extrator PHP) e Node.js 20 ou mais novo (só para a interface).

```bash
git clone https://github.com/cristianoDencode/php-map.git
cd php-map

python3 -m venv .venv                      # no Ubuntu/Debian: sudo apt install python3-venv
.venv/bin/pip install -e ".[dev,mcp]"      # no Windows: .venv\Scripts\pip install -e ".[dev,mcp]"
.venv/bin/php-map setup-extractor --build-image   # imagem Docker do extrator (PHP 7.4 + composer)

cd ui && npm ci && cd ..                   # só se for mexer na interface
```

Os exemplos usam os caminhos do Linux. No Windows, troque `.venv/bin/` por `.venv\Scripts\`.

## Verificações

Rode tudo isto antes de abrir um pull request. É o mesmo que o [CI](.github/workflows/ci.yml) roda.

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/mypy
.venv/bin/pytest -q -m "not docker and not remote_db"      # rotina, sem Docker nem banco remoto
.venv/bin/pytest -q -m docker                              # com o extrator real (precisa da imagem)

cd ui
npm run lint && npm run typecheck && npm test && npm run build
```

Marcadores de teste:

| Marcador | Quando usar |
|---|---|
| `docker` | O teste roda o extrator PHP num container. Pula sozinho se a imagem não existe |
| `remote_db` | O teste lê o banco real de um e-Cidade (variável `PHP_MAP_CATALOG_DSN`). Nunca roda no CI |

Teste novo usa **fixtures pequenas e inventadas** (veja `tests/fixtures/zend1-mini/`), nunca uma instalação inteira de um sistema real nem trechos copiados dele.

## Estilo de código

- Python com tipos estritos (`mypy --strict`), `ruff` para lint e formatação, linhas de até 100 colunas.
- Identificadores de código em inglês; mensagens para o usuário, comentários e documentação em português.
- Toda aresta do grafo carrega `source` e `confidence`. `confidence = 1.0` só para fonte determinística e sem ambiguidade. Veja a [ontologia](docs/ontologia.md).
- Determinismo primeiro: o mesmo código-fonte tem de produzir o mesmo grafo. Heurística entra com confiança menor que 1.0 e com a evidência (`arquivo:linha`).
- O sistema mapeado é **somente leitura**: nenhuma mudança escreve no código ou no banco dele.

## Encoding do e-Cidade

Os fontes do e-Cidade estão em **ISO-8859-1**. O extrator converte para UTF-8 antes de emitir os registros, e as fixtures preservam os bytes originais. Por isso:

- se uma fixture usa ISO-8859-1 de propósito (para testar a leitura), não reformate nem "conserte" os acentos dela;
- configure o editor para **não** reconverter arquivos que você não alterou. No VS Code, desligue `files.autoGuessEncoding` ao abrir fixtures;
- arquivos do próprio projeto (código, docs, testes) são sempre UTF-8 com quebra de linha LF.

## Commits e pull requests

- Uma mudança por pull request, com testes junto.
- Mensagens de commit no formato `tipo: resumo curto em português`, com `tipo` entre `feat`, `fix`, `docs`, `refactor`, `test` e `chore`. Exemplo: `fix: não duplica a aresta de include com modification()`.
- Descreva no pull request o que mudou, por quê e como você verificou. Se mexeu no extrator, no resolvedor ou no grafo, confira o resultado num sistema seu e informe o que mudou; nenhuma ligação que antes estava certa pode passar a errada sem justificativa. Veja [docs/precisao.md](docs/precisao.md).
- Nunca inclua dados reais: nem `.env`, nem grafos (`*.db`), nem trechos de código de um sistema que você não pode publicar.

## Como propor um perfil novo

Um *perfil* ensina o php-map a ler um tipo de sistema (hoje, `ecidade` e `zend1`). Para propor outro:

1. Abra uma issue descrevendo o framework, os marcadores da pasta (arquivos que só existem nele) e um exemplo mínimo.
2. Siga o passo a passo de [docs/perfis.md](docs/perfis.md): o perfil em `src/php_map/profiles/`, a resolução do que é específico em `src/php_map/resolve/` e as fixtures **sintéticas** (escritas à mão, sem código de terceiros) em `tests/fixtures/`.
3. Inclua testes de unidade para cada regra do perfil e um teste de ponta a ponta com o scan da fixture.

## Dúvidas

Abra uma issue com a pergunta. Para vulnerabilidades, **não** use issues públicas: siga o [SECURITY.md](SECURITY.md).
