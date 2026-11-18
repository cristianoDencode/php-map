"""Usuários do cofre e o papel de cada um (`admin` ou `leitor`); desativar, não apagar.

O cofre sempre tem ao menos um `admin` ativo: o último não pode ser desativado nem rebaixado.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from php_map.access import passwords
from php_map.access.store import Vault, from_text, to_text
from php_map.errors import AccessError

MAX_FAILURES = 5
LOCK_FOR = timedelta(minutes=15)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
MAX_EMAIL = 254
MAX_EMAIL_LOCAL = 64
INVALID_LOGIN = "usuário ou senha inválidos"
ADMIN = "admin"
READER = "leitor"
ROLES = (ADMIN, READER)
LAST_ADMIN = "este é o último administrador ativo: crie ou promova outro antes de continuar"


@dataclass(frozen=True, slots=True)
class User:
    id: int
    username: str
    created_at: str
    last_login_at: str | None
    disabled_at: str | None
    locked_until: str | None
    role: str

    @property
    def active(self) -> bool:
        return self.disabled_at is None


def _user(r: sqlite3.Row) -> User:
    return User(
        id=int(r["id"]),
        username=str(r["username"]),
        created_at=str(r["created_at"]),
        last_login_at=r["last_login_at"],
        disabled_at=r["disabled_at"],
        locked_until=r["locked_until"],
        role=str(r["role"]),
    )


def normalize_email(text: str) -> str:
    """O e-mail como é guardado: sem espaços nas pontas e em minúsculas."""
    return text.strip().lower()


def check_email(text: str) -> str:
    """Devolve o e-mail normalizado ou lança `AccessError` se não tiver a forma `nome@dominio.com`
    (sem espaços, um só `@`, domínio com ponto, até 254 caracteres)."""
    email = normalize_email(text)
    local = email.partition("@")[0]
    if len(email) > MAX_EMAIL or len(local) > MAX_EMAIL_LOCAL or not _EMAIL.match(email):
        raise AccessError("e-mail inválido: use o formato nome@dominio.com")
    return email


def _find(vault: Vault, username: str) -> sqlite3.Row | None:
    """Acha pelo e-mail (ou pelo nome de um usuário antigo), sem diferenciar maiúsculas."""
    rows = vault.query(
        "SELECT * FROM users WHERE lower(username) = ?", (normalize_email(username),)
    )
    return rows[0] if rows else None


def get_user(vault: Vault, username: str) -> User | None:
    row = _find(vault, username)
    return _user(row) if row is not None else None


def check_role(role: str) -> str:
    if role not in ROLES:
        raise AccessError(f"papel inválido '{role}': use {' ou '.join(ROLES)}")
    return role


def _other_active_admins(vault: Vault, user: User) -> bool:
    rows = vault.query(
        "SELECT 1 FROM users WHERE role = ? AND disabled_at IS NULL AND id <> ? LIMIT 1",
        (ADMIN, user.id),
    )
    return bool(rows)


def has_active_admin(vault: Vault) -> bool:
    rows = vault.query(
        "SELECT 1 FROM users WHERE role = ? AND disabled_at IS NULL LIMIT 1", (ADMIN,)
    )
    return bool(rows)


def add_user(vault: Vault, username: str, password: str, role: str = ADMIN) -> User:
    """Cria o usuário, identificado pelo e-mail (guardado em minúsculas; `username` é o nome
    interno da coluna). Sem nenhum administrador ativo só se cria administrador, para a
    instalação nunca ficar sem quem a gerencie. Usuários antigos, que não são e-mail, seguem
    entrando e sendo geridos; só a criação exige o e-mail."""
    check_role(role)
    name = check_email(username)
    if _find(vault, name) is not None:
        raise AccessError(f"o e-mail '{name}' já tem usuário")
    if role != ADMIN and not has_active_admin(vault):
        raise AccessError("o primeiro usuário precisa ser administrador")
    stored = passwords.hash_password(password)
    vault.run(
        "INSERT INTO users(username, password_hash, created_at, role) VALUES (?, ?, ?, ?)",
        (name, stored, to_text(vault.now()), role),
    )
    user = get_user(vault, name)
    assert user is not None
    return user


def _require(vault: Vault, username: str) -> User:
    user = get_user(vault, username)
    if user is None:
        raise AccessError(f"usuário '{username.strip()}' não encontrado")
    return user


def set_password(vault: Vault, username: str, password: str) -> User:
    user = _require(vault, username)
    vault.run(
        "UPDATE users SET password_hash = ?, failed_count = 0, locked_until = NULL WHERE id = ?",
        (passwords.hash_password(password), user.id),
    )
    return _require(vault, username)


def disable_user(vault: Vault, username: str) -> User:
    """Desativa o usuário e derruba as sessões abertas dele. Recusa o último administrador."""
    user = _require(vault, username)
    if user.active and user.role == ADMIN and not _other_active_admins(vault, user):
        raise AccessError(LAST_ADMIN)
    now = to_text(vault.now())
    vault.run("UPDATE users SET disabled_at = ? WHERE id = ?", (now, user.id))
    vault.run(
        "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
        (now, user.id),
    )
    return _require(vault, username)


def enable_user(vault: Vault, username: str) -> User:
    user = _require(vault, username)
    vault.run("UPDATE users SET disabled_at = NULL WHERE id = ?", (user.id,))
    return _require(vault, username)


def set_role(vault: Vault, username: str, role: str) -> User:
    """Troca o papel. Recusa rebaixar o último administrador ativo."""
    check_role(role)
    user = _require(vault, username)
    if (
        role != ADMIN
        and user.role == ADMIN
        and user.active
        and not _other_active_admins(vault, user)
    ):
        raise AccessError(LAST_ADMIN)
    vault.run("UPDATE users SET role = ? WHERE id = ?", (role, user.id))
    return _require(vault, username)


def list_users(vault: Vault) -> list[User]:
    return [_user(r) for r in vault.query("SELECT * FROM users ORDER BY username")]


def has_active_users(vault: Vault) -> bool:
    rows = vault.query("SELECT 1 FROM users WHERE disabled_at IS NULL LIMIT 1")
    return bool(rows)


def authenticate(vault: Vault, username: str, password: str) -> User | None:
    """Confere usuário e senha. Qualquer falha devolve `None` (inexistente, desativado,
    bloqueado ou senha errada são indistinguíveis para quem chama).

    Cinco falhas seguidas bloqueiam a conta por 15 minutos; o relógio vem de `vault.now`.
    """
    row = _find(vault, username)
    if row is None:
        passwords.dummy_verify(password)
        return None
    now = vault.now()
    locked = from_text(row["locked_until"])
    if row["disabled_at"] is not None or (locked is not None and locked > now):
        passwords.dummy_verify(password)
        return None
    if not passwords.verify_password(password, str(row["password_hash"])):
        failed = int(row["failed_count"]) + 1
        if failed >= MAX_FAILURES:
            vault.run(
                "UPDATE users SET failed_count = 0, locked_until = ? WHERE id = ?",
                (to_text(now + LOCK_FOR), row["id"]),
            )
        else:
            vault.run(
                "UPDATE users SET failed_count = ? WHERE id = ?",
                (failed, row["id"]),
            )
        return None
    vault.run(
        "UPDATE users SET failed_count = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
        (to_text(now), row["id"]),
    )
    return get_user(vault, username)
