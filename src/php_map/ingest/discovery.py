"""Descoberta dos arquivos PHP a extrair."""

import os
from pathlib import Path

from php_map.config import Settings
from php_map.errors import ConfigError


def list_php_files(source: Path, settings: Settings) -> list[str]:
    """Caminhos relativos a `source` (com `/`), ordenados.

    Diretório cujo **nome** está em `settings.exclude_dirs` é podado em qualquer nível (o
    e-cidade tem `vendor/` e `node_modules/` enormes, em vários lugares). Uma entrada com `/`
    só poda o caminho **exato** a partir da raiz: o perfil `zend1` precisa
    excluir só `library/Zend`, `library/Doctrine`... de terceiros, sem excluir o `library/` de
    cada módulo (`application/modules/<m>/library/`, código próprio por convenção `Lib_`).
    """
    if not source.is_dir():
        raise ConfigError(f"diretório de origem inexistente: {source}")
    extensions = {ext.lower() for ext in settings.php_extensions}
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(source):
        base = Path(dirpath).relative_to(source)
        rel_base = "" if base == Path(".") else base.as_posix()
        dirnames[:] = [
            d
            for d in dirnames
            if d not in settings.exclude_dirs
            and (f"{rel_base}/{d}" if rel_base else d) not in settings.exclude_dirs
        ]
        for name in filenames:
            if Path(name).suffix.lower() in extensions:
                found.append((base / name).as_posix())
    return sorted(found)
