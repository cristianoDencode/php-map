"""Abre e migra o SQLite do cofre.

Conexão própria com `check_same_thread=False` e um `RLock`: a interface atende em várias threads.
O processo do MCP abre os grafos em `mode=ro`; o cofre é outro arquivo, aberto em escrita.
"""

import logging
import os
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from php_map.config import PROJECT_ROOT, env_value
from php_map.errors import AccessError

logger = logging.getLogger(__name__)

ACCESS_DB_ENV = "PHP_MAP_ACCESS_DB"
SCHEMA_VERSION = 2
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

Clock = Callable[[], datetime]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    failed_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    disabled_at TEXT,
    role TEXT NOT NULL DEFAULT 'admin' CHECK (role IN ('admin', 'leitor'))
);
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL DEFAULT 'mcp:read',
    created_at TEXT NOT NULL,
    created_by TEXT,
    expires_at TEXT,
    last_used_at TEXT,
    last_used_ip TEXT,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT,
    ip TEXT,
    user_agent TEXT,
    revoked_at TEXT
);
"""


# Cofre da versão 1: a tabela `users` ainda não tem papel, e quem já existia continua admin.
_ADD_ROLE = (
    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'"
    " CHECK (role IN ('admin', 'leitor'))"
)


def to_text(moment: datetime) -> str:
    """Instante em UTC no formato do cofre (comparável como texto)."""
    return moment.astimezone(UTC).strftime(TIME_FORMAT)


def from_text(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=UTC)


def default_path() -> Path:
    """`PHP_MAP_ACCESS_DB` (variável ou `.env`) ou `<raiz>/out/access/access.db`."""
    configured = env_value(ACCESS_DB_ENV)
    if configured:
        return Path(configured).expanduser()
    return PROJECT_ROOT / "out" / "access" / "access.db"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Vault:
    """O cofre aberto. Use `open_vault`; os módulos de domínio recebem esta instância."""

    def __init__(self, conn: sqlite3.Connection, path: Path, clock: Clock = _utc_now) -> None:
        self.path = path
        self.clock = clock
        self._conn = conn
        self._lock = threading.RLock()
        # Último `last_used` gravado por token (no máximo uma escrita por minuto).
        self.touched: dict[int, datetime] = {}

    def now(self) -> datetime:
        return self.clock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def run(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Executa uma escrita e confirma. Devolve o `lastrowid`."""
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cursor.lastrowid or 0

    def get_meta(self, key: str) -> str | None:
        rows = self.query("SELECT value FROM meta WHERE key = ?", (key,))
        return str(rows[0]["value"]) if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self.run("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _migrate(vault: Vault) -> None:
    """Leva o cofre até `SCHEMA_VERSION`. Confere a coluna em vez de confiar só na versão, para
    um cofre que parou no meio da migração terminar de migrar."""
    columns = {str(r["name"]) for r in vault.query("PRAGMA table_info(users)")}
    if "role" not in columns:
        vault.run(_ADD_ROLE)
    if vault.get_meta("schema_version") != str(SCHEMA_VERSION):
        vault.set_meta("schema_version", str(SCHEMA_VERSION))


def _restrict_permissions(path: Path) -> None:
    if sys.platform == "win32":
        logger.warning("cofre de acesso: o Windows ignora chmod 600; proteja %s pelo sistema", path)
        return
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.warning("cofre de acesso: não consegui aplicar chmod 600 em %s (%s)", path, exc)


def open_vault(path: Path | None = None, *, clock: Clock = _utc_now) -> Vault:
    """Abre (e cria, se preciso) o cofre. `path=None` usa `default_path()`."""
    target = path if path is not None else default_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        conn = sqlite3.connect(target, check_same_thread=False, timeout=10)
    except (OSError, sqlite3.Error) as exc:
        raise AccessError(f"não consegui abrir o cofre de acesso em {target}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    vault = Vault(conn, target, clock)
    try:
        with vault.lock:
            conn.executescript(_SCHEMA)
            found = vault.get_meta("schema_version")
            if found is not None and int(found) > SCHEMA_VERSION:
                raise AccessError(
                    f"o cofre {target} é de uma versão mais nova ({found}); atualize o php-map"
                )
            _migrate(vault)
    except sqlite3.Error as exc:
        conn.close()
        raise AccessError(f"cofre de acesso inválido em {target}: {exc}") from exc
    except AccessError:
        conn.close()
        raise
    if not existed:
        _restrict_permissions(target)
    return vault
