"""Modelos de briefing para IA: o sistema monta o contexto, a IA lê.

- `model`/`parse`: o formato do modelo (cabeçalho restrito e marcadores `{{…}}`);
- `blocks`: os blocos que o sistema preenche sem IA a partir do grafo;
- `resolve`: do que a pessoa digita para o nó do grafo;
- `engine`: validar, montar, catálogo e armazenamento dos modelos do usuário.
"""
