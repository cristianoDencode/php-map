"""Sessões do login da interface (usadas pela interface web).

O id é aleatório e só o hash dele fica no cofre. O cookie leva `<id>.<hmac>`: o HMAC-SHA256 usa
uma chave de 32 bytes gerada no primeiro uso e guardada em `meta`. A assinatura detecta cookie
forjado ou adulterado antes de qualquer consulta ao banco.
"""

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from php_map.access.store import Vault, from_text, to_text

# Absoluta: 7 dias desde o login. Inatividade: 12 h sem nenhuma requisição.
SESSION_TTL = timedelta(days=7)
IDLE_TTL = timedelta(hours=12)
TOUCH_EVERY = timedelta(minutes=1)
_KEY_META = "session_hmac_key"


@dataclass(frozen=True, slots=True)
class SessionInfo:
    user_id: int
    username: str
    expires_at: str
    role: str


def _key(vault: Vault) -> bytes:
    with vault.lock:
        stored = vault.get_meta(_KEY_META)
        if stored is None:
            stored = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
            vault.set_meta(_KEY_META, stored)
    return base64.b64decode(stored)


def sign(vault: Vault, value: str) -> str:
    mac = hmac.new(_key(vault), value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{value}.{mac}"


def unsign(vault: Vault, signed: str) -> str | None:
    """O valor original, ou `None` se a assinatura não bater (comparação em tempo constante)."""
    value, sep, mac = signed.rpartition(".")
    if not sep or not value:
        return None
    expected = hmac.new(_key(vault), value.encode("utf-8"), hashlib.sha256).hexdigest()
    return value if hmac.compare_digest(mac.encode("utf-8"), expected.encode("ascii")) else None


def _hash_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def create_session(
    vault: Vault,
    user_id: int,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    ttl: timedelta = SESSION_TTL,
) -> str:
    """Abre uma sessão e devolve o **valor do cookie** (id assinado)."""
    session_id = secrets.token_urlsafe(32)
    now = vault.now()
    vault.run(
        "INSERT INTO sessions(id_hash, user_id, created_at, expires_at, last_seen_at, ip,"
        " user_agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            _hash_id(session_id),
            user_id,
            to_text(now),
            to_text(now + ttl),
            to_text(now),
            ip,
            (user_agent or "")[:200] or None,
        ),
    )
    return sign(vault, session_id)


def verify_session(vault: Vault, cookie: str) -> SessionInfo | None:
    """A sessão vale? Assinatura, existência, revogação, validade (absoluta e por inatividade) e
    usuário ativo."""
    session_id = unsign(vault, cookie)
    if session_id is None:
        return None
    rows = vault.query(
        "SELECT s.id_hash, s.user_id, s.expires_at, s.revoked_at, s.last_seen_at, u.username,"
        " u.disabled_at, u.role FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.id_hash = ?",
        (_hash_id(session_id),),
    )
    if not rows:
        return None
    row = rows[0]
    now = vault.now()
    expires = from_text(row["expires_at"])
    if row["revoked_at"] is not None or row["disabled_at"] is not None:
        return None
    if expires is None or expires <= now:
        return None
    seen = from_text(row["last_seen_at"])
    if seen is not None and now - seen >= IDLE_TTL:
        return None
    if seen is None or now - seen >= TOUCH_EVERY:
        vault.run(
            "UPDATE sessions SET last_seen_at = ? WHERE id_hash = ?",
            (to_text(now), row["id_hash"]),
        )
    return SessionInfo(
        int(row["user_id"]), str(row["username"]), str(row["expires_at"]), str(row["role"])
    )


def revoke_session(vault: Vault, cookie: str) -> bool:
    session_id = unsign(vault, cookie)
    if session_id is None:
        return False
    before = vault.query(
        "SELECT 1 FROM sessions WHERE id_hash = ? AND revoked_at IS NULL", (_hash_id(session_id),)
    )
    vault.run(
        "UPDATE sessions SET revoked_at = ? WHERE id_hash = ? AND revoked_at IS NULL",
        (to_text(vault.now()), _hash_id(session_id)),
    )
    return bool(before)
