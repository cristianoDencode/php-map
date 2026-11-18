## O que muda

<!-- Resumo curto e o motivo. Relacione a issue, se houver (Fixes #123). -->

## Como foi verificado

<!-- Comandos rodados e resultado. -->

- [ ] `ruff check`, `ruff format --check` e `mypy` sem erros
- [ ] `pytest -q -m "not docker and not remote_db"` passa (e `-m docker`, se mexi no extrator)
- [ ] Interface: `npm run lint`, `npm run typecheck`, `npm test` e `npm run build` passam (se mexi em `ui/`)
- [ ] Mexi no extrator, no resolvedor ou no grafo: conferi o resultado num sistema meu e descrevi o que mudou (veja [docs/precisao.md](../docs/precisao.md))
- [ ] Testes novos cobrem a mudança, com fixtures pequenas
- [ ] Documentação atualizada (README, `docs/`, `CHANGELOG.md`)
- [ ] Não incluí `.env`, grafos (`*.db`) nem código de um sistema que não posso publicar
