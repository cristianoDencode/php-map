"""Cofre de acesso do mapeador: usuários, tokens do MCP e sessões do login.

Um SQLite próprio, separado dos grafos. As regras de negócio ficam em módulos pequenos
(`passwords`, `users`, `tokens`, `sessions`); `store` só abre e migra o banco.
"""

from php_map.access.store import Vault, default_path, open_vault

__all__ = ["Vault", "default_path", "open_vault"]
