"""Exceções do domínio do mapeador."""


class PhpMapError(Exception):
    """Erro base do php-map."""


class ConfigError(PhpMapError):
    """Configuração inválida (caminho inexistente, parâmetro incoerente etc.)."""


class IngestError(PhpMapError):
    """Falha ao obter os registros do extrator (Docker, imagem, dependências, contrato)."""


class GraphContractError(PhpMapError):
    """Nó ou aresta fora do contrato da ontologia (label, confiança, evidência)."""


class StoreError(PhpMapError):
    """Banco do grafo ausente, corrompido ou de outra versão do schema."""


class QueryError(PhpMapError):
    """Consulta ao grafo sem alvo: entrada ou tabela inexistente (com sugestões)."""


class UsageError(ConfigError):
    """Combinação de argumentos que não faz sentido (ex.: dois bancos do mesmo projeto)."""


class AccessError(PhpMapError):
    """Cofre de acesso (usuários, tokens do MCP, sessões): entrada inválida ou estado impossível."""
