"""Tokens do MCP: opacos, mostrados uma vez, guardados como SHA-256."""

import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from php_map.access.store import Vault, from_text, to_text
from php_map.errors import AccessError

TOKEN_PREFIX = "ecm_"
DISPLAY_PREFIX = 12
SCOPE = "mcp:read"
DEFAULT_EXPIRES = "90d"
TOUCH_EVERY = timedelta(minutes=1)
_EXPIRES = re.compile(r"^(\d{1,4})d$")


@dataclass(frozen=True, slots=True)
class TokenInfo:
    id: int
    name: str
    prefix: str
    scope: str
    created_at: str
    created_by: str | None
    expires_at: str | None
    last_used_at: str | None
    last_used_ip: str | None
    revoked_at: str | None

    def state(self, now: datetime) -> str:
        """`ativo`, `vencido` ou `revogado` (revogado vence os outros)."""
        if self.revoked_at is not None:
            return "revogado"
        expires = from_text(self.expires_at)
        if expires is not None and expires <= now:
            return "vencido"
        return "ativo"


def _info(r: sqlite3.Row) -> TokenInfo:
    return TokenInfo(
        id=int(r["id"]),
        name=str(r["name"]),
        prefix=str(r["prefix"]),
        scope=str(r["scope"]),
        created_at=str(r["created_at"]),
        created_by=r["created_by"],
        expires_at=r["expires_at"],
        last_used_at=r["last_used_at"],
        last_used_ip=r["last_used_ip"],
        revoked_at=r["revoked_at"],
    )


def hash_token(raw: str | bytes) -> str:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    return hashlib.sha256(data).hexdigest()


def parse_expires(value: str) -> timedelta | None:
    """`30d`, `90d`, `365d`… ou `never` (`None`: não vence)."""
    text = value.strip().lower()
    if text == "never":
        return None
    match = _EXPIRES.match(text)
    if match is None or int(match.group(1)) < 1:
        raise AccessError("validade inválida: use um número de dias (ex.: 30d, 90d) ou 'never'")
    return timedelta(days=int(match.group(1)))


def create_token(
    vault: Vault, name: str, *, expires: str = DEFAULT_EXPIRES, created_by: str | None = None
) -> tuple[str, TokenInfo]:
    """Cria o token. O valor bruto só existe no retorno: o cofre guarda o hash."""
    label = name.strip()
    if not label:
        raise AccessError("informe um nome para o token (ex.: meu-notebook)")
    lifetime = parse_expires(expires)
    now = vault.now()
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_id = vault.run(
        "INSERT INTO tokens(name, prefix, token_hash, scope, created_at, created_by, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            label,
            raw[:DISPLAY_PREFIX],
            hash_token(raw),
            SCOPE,
            to_text(now),
            created_by,
            to_text(now + lifetime) if lifetime is not None else None,
        ),
    )
    return raw, _get(vault, token_id)


def _get(vault: Vault, token_id: int) -> TokenInfo:
    rows = vault.query("SELECT * FROM tokens WHERE id = ?", (token_id,))
    if not rows:
        raise AccessError(f"token {token_id} não encontrado")
    return _info(rows[0])


def list_tokens(vault: Vault) -> list[TokenInfo]:
    """Nunca devolve o hash."""
    return [_info(r) for r in vault.query("SELECT * FROM tokens ORDER BY id")]


def has_active_token(vault: Vault) -> bool:
    now = vault.now()
    return any(t.state(now) == "ativo" for t in list_tokens(vault))


def revoke_token(vault: Vault, ref: str) -> TokenInfo:
    """Revoga por id ou por prefixo (de exibição, completo ou parcial)."""
    key = ref.strip()
    tokens = list_tokens(vault)
    if key.isdigit():
        found = [t for t in tokens if t.id == int(key)]
    else:
        found = [t for t in tokens if t.prefix.startswith(key)] if len(key) >= 4 else []
    if not found:
        raise AccessError(f"nenhum token com id ou prefixo '{ref}' (veja `php-map token list`)")
    if len(found) > 1:
        raise AccessError(f"'{ref}' casa com {len(found)} tokens; use o id")
    target = found[0]
    if target.revoked_at is None:
        vault.run(
            "UPDATE tokens SET revoked_at = ? WHERE id = ?", (to_text(vault.now()), target.id)
        )
    return _get(vault, target.id)


def verify_token(vault: Vault, raw: bytes | str, ip: str | None = None) -> TokenInfo | None:
    """O token existe, não foi revogado e não venceu? Nunca levanta por token inválido e nunca
    registra o valor (só o prefixo, em quem chama).

    O `last_used` é gravado no máximo 1× por minuto por token.
    """
    data = raw.encode("utf-8", "replace") if isinstance(raw, str) else raw
    if not data.startswith(TOKEN_PREFIX.encode("ascii")):
        return None
    try:
        rows = vault.query("SELECT * FROM tokens WHERE token_hash = ?", (hash_token(data),))
    except sqlite3.Error:
        return None
    if not rows:
        return None
    info = _info(rows[0])
    now = vault.now()
    if info.state(now) != "ativo":
        return None
    with vault.lock:
        last = vault.touched.get(info.id)
        if last is None or now - last >= TOUCH_EVERY:
            vault.touched[info.id] = now
            vault.run(
                "UPDATE tokens SET last_used_at = ?, last_used_ip = ? WHERE id = ?",
                (to_text(now), ip, info.id),
            )
    return info
