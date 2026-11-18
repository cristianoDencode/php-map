# Interface do php-map

React + Vite + TypeScript, Tailwind + componentes no estilo shadcn/ui (Radix), TanStack Query e GraphQL.

A interface só conversa com o servidor `php-map ui`, por `/graphql`.

## Uso
Pré-requisitos: Node.js 20 ou mais novo e um grafo gerado por `php-map scan` (veja o [README](../README.md)).

```bash
cd ui
npm ci               # também gera os tipos (npm run codegen) a partir de ui/schema.graphql
npm run build        # gera ui/dist, que o `php-map ui` serve em http://127.0.0.1:8765/
cd ..
.venv/bin/php-map ui --db out/meu-scan/graph.db --open
```

No Windows, use `.venv\Scripts\php-map` no lugar de `.venv/bin/php-map`.

A interface sempre pede login. Na primeira execução, o terminal imprime a URL e o código de configuração inicial; a tela pede esse código, o seu e-mail e uma senha para criar o administrador.

## Desenvolvimento
```bash
.venv/bin/php-map ui --db out/meu-scan/graph.db   # a API, na porta 8765
cd ui
npm run dev          # http://localhost:5173, com o /graphql encaminhado para 127.0.0.1:8765
```

Para desenvolver sem passar pelo login, suba a API com `--no-auth` (só com `--host 127.0.0.1`).

| Script | O que faz |
|---|---|
| `npm run codegen` | Gera `src/graphql/generated/` (fora do git) a partir de `schema.graphql` |
| `npm run lint` | ESLint |
| `npm run typecheck` | `tsc` estrito |
| `npm test` | Vitest + Testing Library (API falsa em `src/test/utils.tsx`) |
| `npm run build` | Tipos + build de produção em `dist/` (fora do git) |

Quando a API mudar, regenere o schema **na raiz do repositório** e rode o codegen de novo:
```bash
.venv/bin/php-map ui --print-schema > ui/schema.graphql
cd ui && npm run codegen
```
No PowerShell 5.1, o `>` grava em UTF-16: use `cmd /c ".venv\Scripts\php-map ui --print-schema > ui\schema.graphql"`.

## Estrutura
- `src/app/`: rotas, providers (tema, painel "Código", toasts de erro) e layout.
- `src/routes/`: páginas (Início, Busca, detalhe de nó, Regras de negócio `/regras` e `/regras/:id`; Impacto `/impacto`, Trace `/trace` e Explorador `/explorar/:id`; Módulos `/modulos` (mapa, matriz, ranking), ficha `/modulos/:id` e Tabelas compartilhadas `/tabelas`; as de diagrama e de módulos carregadas sob demanda).
- `src/components/graph/`: diagramas (Cytoscape): `GraphCanvas` (layouts, estilos, PNG, `dagre` no web worker acima de 250 nós), `GraphView` (diagrama + painel + legenda + lista com CSV), `Controls` (alvo e filtros).
- `src/components/`: componentes (`ui/` de base, paleta ⌘K, visualizador de código).
- `src/lib/`: API, IDs, rótulos PT-BR (`labels.ts`, fonte única dos nomes dos tipos), realce de sintaxe.
- Cores por tipo de nó: tokens `--node-*` em `src/index.css` (claro e escuro), reusados pelo Cytoscape.
