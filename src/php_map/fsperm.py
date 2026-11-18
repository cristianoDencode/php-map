"""Permissões dos arquivos que o programa grava (grafos, cache de AST, configurações).

O grafo guarda a arquitetura e o texto das funções do sistema mapeado: só o usuário que rodou o
programa deve ler. Em POSIX, `apply_umask()` faz tudo que o processo criar nascer `600`/`700`, e
`secure_file`/`secure_dir` corrigem o que já existia (reescrito no lugar) ou foi criado por quem
não herda o umask. No Windows, `chmod` não restringe nada: o aviso sai uma vez e a proteção
fica por conta das permissões da pasta e da criptografia de disco.
"""

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PRIVATE_FILE = 0o600
PRIVATE_DIR = 0o700
PRIVATE_UMASK = 0o077

_warned_windows = False


def is_posix() -> bool:
    return sys.platform != "win32"


def _warn_windows(path: Path) -> None:
    global _warned_windows
    if not _warned_windows:
        _warned_windows = True
        logger.warning(
            "o Windows ignora chmod: proteja %s pelas permissões da pasta (ou criptografe o disco)",
            path,
        )


def apply_umask() -> None:
    """Todo arquivo novo do processo nasce `600` e toda pasta nova `700` (só POSIX).

    O umask é global ao processo: chame uma vez, na entrada da CLI, nunca numa biblioteca.
    """
    if is_posix():
        os.umask(PRIVATE_UMASK)


def _chmod(path: Path, mode: int) -> None:
    if not is_posix():
        _warn_windows(path)
        return
    try:
        os.chmod(path, mode)
    except OSError as exc:  # arquivo de outro dono, sistema de arquivos sem permissão...
        logger.warning("não consegui restringir %s (%s)", path, exc)


def secure_file(path: Path) -> None:
    """`chmod 600`, se o arquivo existir."""
    if path.is_file():
        _chmod(path, PRIVATE_FILE)


def secure_dir(path: Path) -> None:
    """`chmod 700`, se a pasta existir. Não mexe na pasta atual, na pasta do usuário nem na raiz:
    quem escolhe `--db grafo.db` não pode ter o diretório de trabalho trancado por isso."""
    if not path.is_dir() or _is_shared_location(path):
        return
    _chmod(path, PRIVATE_DIR)


def secure_tree(path: Path) -> None:
    """`secure_dir` na pasta e `secure_file` em tudo o que há dentro (cache de AST, `grafos/`)."""
    if not path.is_dir():
        return
    secure_dir(path)
    for child in path.rglob("*"):
        if child.is_symlink():
            continue
        if child.is_dir():
            _chmod(child, PRIVATE_DIR)
        elif child.is_file():
            _chmod(child, PRIVATE_FILE)


def _is_shared_location(path: Path) -> bool:
    resolved = path.resolve()
    if resolved == resolved.parent:  # raiz
        return True
    return resolved in (Path.cwd().resolve(), Path.home().resolve())
