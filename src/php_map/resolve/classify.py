"""Classificação determinística: `module_hint` pelo nome do arquivo e `is_infra` por listas."""

import re

from php_map.config import Settings

_MODULE_PREFIX = re.compile(r"^([a-z]{3})\d_", re.IGNORECASE)


def module_hint(path: str) -> str | None:
    """Prefixo de módulo das telas (`emp4_anularempenho001.php` → `emp`); senão `None`."""
    match = _MODULE_PREFIX.match(path.rsplit("/", 1)[-1])
    return match.group(1).lower() if match else None


def is_infra_symbol(name: str, settings: Settings) -> bool:
    """Funções e classes de infraestrutura (`db_query`, `db_utils`...)."""
    return name.lower() in settings.infra_functions


def is_infra_file(path: str, settings: Settings) -> bool:
    """Infraestrutura do e-cidade (`infra_files`) ou terceiros do perfil (`third_party_dirs`:
    `library/Zend/`, `library/Doctrine/`... no perfil `zend1`)."""
    if path in settings.infra_files:
        return True
    return any(path == d or path.startswith(d + "/") for d in settings.third_party_dirs)
