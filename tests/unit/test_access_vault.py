"""Cofre de acesso (senhas, usuários, tokens, sessões) com relógio injetado."""

import sqlite3
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from php_map.access import passwords, sessions, tokens, users
from php_map.access.store import Vault, default_path, open_vault
from php_map.errors import AccessError
from php_map.mcp.auth import AnyOf, StaticTokenVerifier, VaultTokenVerifier

PASSWORD = "uma-senha-longa"


class Clock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs: float) -> None:
        self.moment += timedelta(**kwargs)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def vault(tmp_path: Path, clock: Clock) -> Vault:
    return open_vault(tmp_path / "cofre" / "access.db", clock=clock)


# --------------------------------------------------------------------------- senhas


def test_password_hash_roundtrip_and_format() -> None:
    stored = passwords.hash_password(PASSWORD)
    assert stored.startswith("scrypt$16384$8$1$")
    assert PASSWORD not in stored
    assert passwords.verify_password(PASSWORD, stored)
    assert not passwords.verify_password("outra-senha-longa", stored)
    assert stored != passwords.hash_password(PASSWORD)  # sal aleatório


def test_short_password_is_refused() -> None:
    with pytest.raises(AccessError, match="10 caracteres"):
        passwords.hash_password("curta")


@pytest.mark.parametrize("stored", ["", "lixo", "md5$1$2$3$a$b", "scrypt$x$8$1$AA==$AA=="])
def test_malformed_hash_never_raises(stored: str) -> None:
    assert not passwords.verify_password(PASSWORD, stored)


# --------------------------------------------------------------------------- cofre


def test_vault_is_created_with_restricted_permissions(vault: Vault) -> None:
    assert vault.get_meta("schema_version") == "2"
    if sys.platform != "win32":
        assert stat.S_IMODE(vault.path.stat().st_mode) == 0o600


def test_vault_reopens_and_refuses_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "access.db"
    first = open_vault(path)
    users.add_user(first, "ana@exemplo.com", PASSWORD)
    first.set_meta("schema_version", "99")
    first.close()
    with pytest.raises(AccessError, match="versão mais nova"):
        open_vault(path)


def test_default_path_reads_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PHP_MAP_ACCESS_DB", str(tmp_path / "x.db"))
    assert default_path() == tmp_path / "x.db"
    monkeypatch.delenv("PHP_MAP_ACCESS_DB")
    assert default_path().parts[-3:] == ("out", "access", "access.db")


# --------------------------------------------------------------------------- usuários


def test_add_list_disable_enable(vault: Vault) -> None:
    users.add_user(vault, "Ana@Exemplo.com", PASSWORD)  # guardado em minúsculas
    with pytest.raises(AccessError, match="já tem usuário"):
        users.add_user(vault, "ana@exemplo.com", PASSWORD)  # e-mail único sem diferenciar caixa
    with pytest.raises(AccessError, match="inválido"):
        users.add_user(vault, "a b", PASSWORD)
    assert [(u.username, u.active) for u in users.list_users(vault)] == [("ana@exemplo.com", True)]
    assert users.has_active_users(vault)
    users.add_user(vault, "bia@exemplo.com", PASSWORD)  # o último administrador não se desativa
    users.disable_user(vault, "ana@exemplo.com")
    assert [u.username for u in users.list_users(vault) if u.active] == ["bia@exemplo.com"]
    assert users.authenticate(vault, "ana@exemplo.com", PASSWORD) is None
    users.enable_user(vault, "ana@exemplo.com")
    assert users.authenticate(vault, " ANA@exemplo.com ", PASSWORD) is not None
    with pytest.raises(AccessError, match="não encontrado"):
        users.disable_user(vault, "ninguem")


def test_lockout_after_five_failures_with_injected_clock(vault: Vault, clock: Clock) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    for _ in range(4):
        assert users.authenticate(vault, "ana@exemplo.com", "senha-errada-123") is None
    assert users.authenticate(vault, "ana@exemplo.com", PASSWORD) is not None  # zera a contagem
    for _ in range(5):
        assert users.authenticate(vault, "ana@exemplo.com", "senha-errada-123") is None
    # bloqueada: nem a senha certa entra
    assert users.authenticate(vault, "ana@exemplo.com", PASSWORD) is None
    clock.advance(minutes=14)
    assert users.authenticate(vault, "ana@exemplo.com", PASSWORD) is None
    clock.advance(minutes=2)
    assert users.authenticate(vault, "ana@exemplo.com", PASSWORD) is not None


def test_unknown_user_and_wrong_password_look_the_same(vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    assert users.authenticate(vault, "ninguem", PASSWORD) is None
    assert users.authenticate(vault, "ana@exemplo.com", "errada-errada") is None


def test_set_password_unlocks(vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    for _ in range(5):
        users.authenticate(vault, "ana@exemplo.com", "errada-errada")
    users.set_password(vault, "ana@exemplo.com", "nova-senha-longa")
    assert users.authenticate(vault, "ana@exemplo.com", "nova-senha-longa") is not None
    assert users.authenticate(vault, "ana@exemplo.com", PASSWORD) is None


# --------------------------------------------------------------------------- tokens


def test_token_lifecycle(vault: Vault, clock: Clock) -> None:
    raw, info = tokens.create_token(vault, "meu-notebook", expires="30d")
    assert raw.startswith("ecm_") and len(raw) >= 40
    assert info.prefix == raw[:12] and info.state(clock()) == "ativo"
    assert tokens.verify_token(vault, raw.encode()) is not None
    assert tokens.verify_token(vault, b"ecm_" + b"x" * 43) is None
    assert tokens.verify_token(vault, b"lixo") is None
    assert tokens.verify_token(vault, b"") is None
    clock.advance(days=31)
    assert tokens.verify_token(vault, raw.encode()) is None
    assert tokens.list_tokens(vault)[0].state(clock()) == "vencido"


def test_revoke_by_id_and_prefix(vault: Vault, clock: Clock) -> None:
    first, a = tokens.create_token(vault, "a")
    second, b = tokens.create_token(vault, "b")
    revoked = tokens.revoke_token(vault, a.prefix)
    assert revoked.state(clock()) == "revogado"
    assert tokens.verify_token(vault, first.encode()) is None
    assert tokens.verify_token(vault, second.encode()) is not None
    tokens.revoke_token(vault, str(b.id))
    assert tokens.verify_token(vault, second.encode()) is None
    assert not tokens.has_active_token(vault)
    with pytest.raises(AccessError, match="nenhum token"):
        tokens.revoke_token(vault, "ecm_zzzzzz")
    with pytest.raises(AccessError, match="2 tokens"):
        tokens.revoke_token(vault, "ecm_")


def test_expires_options(vault: Vault) -> None:
    _, never = tokens.create_token(vault, "n", expires="never")
    assert never.expires_at is None
    for bad in ("0d", "abc", "10", "-5d"):
        with pytest.raises(AccessError, match="validade"):
            tokens.create_token(vault, "x", expires=bad)
    with pytest.raises(AccessError, match="nome"):
        tokens.create_token(vault, "  ")


def test_raw_token_is_not_in_the_database_file(vault: Vault) -> None:
    raw, _ = tokens.create_token(vault, "segredo")
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    vault.close()
    blob = vault.path.read_bytes()
    assert raw.encode() not in blob and PASSWORD.encode() not in blob
    rows = sqlite3.connect(vault.path).execute("SELECT token_hash FROM tokens").fetchall()
    assert rows == [(tokens.hash_token(raw),)]


def test_last_used_is_written_at_most_once_per_minute(vault: Vault, clock: Clock) -> None:
    raw, info = tokens.create_token(vault, "t")
    writes: list[str] = []
    original = vault.run

    def spy(sql: str, params: object = ()) -> int:
        if sql.startswith("UPDATE tokens SET last_used_at"):
            writes.append(sql)
        return original(sql, params)  # type: ignore[arg-type]

    vault.run = spy  # type: ignore[method-assign]
    for _ in range(20):
        assert tokens.verify_token(vault, raw, ip="10.0.0.1") is not None
    assert len(writes) == 1
    clock.advance(seconds=61)
    tokens.verify_token(vault, raw, ip="10.0.0.2")
    assert len(writes) == 2
    listed = tokens.list_tokens(vault)[0]
    assert listed.last_used_ip == "10.0.0.2" and listed.last_used_at is not None
    assert listed.id == info.id


def test_token_never_leaks_in_logs(vault: Vault, caplog: pytest.LogCaptureFixture) -> None:
    raw, _ = tokens.create_token(vault, "t")
    with caplog.at_level("DEBUG"):
        tokens.verify_token(vault, raw)
        tokens.verify_token(vault, b"ecm_" + b"y" * 40)
    assert raw not in caplog.text


# --------------------------------------------------------------------------- sessões


def test_session_cookie_roundtrip_expiry_and_revocation(vault: Vault, clock: Clock) -> None:
    user = users.add_user(vault, "ana@exemplo.com", PASSWORD)
    cookie = sessions.create_session(vault, user.id, ip="1.2.3.4", user_agent="pytest")
    info = sessions.verify_session(vault, cookie)
    assert info is not None and info.username == "ana@exemplo.com"
    session_id = cookie.rpartition(".")[0]
    assert session_id.encode() not in vault.path.read_bytes()  # só o hash fica no cofre
    clock.advance(hours=11)
    assert sessions.verify_session(vault, cookie) is not None  # usar renova a inatividade
    clock.advance(hours=13)
    assert sessions.verify_session(vault, cookie) is None  # parada por mais de 12 h
    live = sessions.create_session(vault, user.id)
    for _ in range(8):  # ativa todo dia: a absoluta (7 dias) corta
        clock.advance(hours=23)
        sessions.verify_session(vault, live)
    assert sessions.verify_session(vault, live) is None
    fresh = sessions.create_session(vault, user.id)
    assert sessions.revoke_session(vault, fresh)
    assert sessions.verify_session(vault, fresh) is None
    assert not sessions.revoke_session(vault, fresh)


def test_tampered_cookie_is_refused(vault: Vault) -> None:
    user = users.add_user(vault, "ana@exemplo.com", PASSWORD)
    cookie = sessions.create_session(vault, user.id)
    sid, _, mac = cookie.rpartition(".")
    flipped = mac[:-1] + ("0" if mac[-1] != "0" else "1")
    for bad in (f"{sid}.{flipped}", f"{sid}x.{mac}", sid, "", ".", "a.b", f"{sid}."):
        assert sessions.verify_session(vault, bad) is None
    assert sessions.unsign(vault, sessions.sign(vault, "valor")) == "valor"
    assert sessions.unsign(vault, "valor.00") is None


def test_session_key_is_stable_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "access.db"
    first = open_vault(path)
    signed = sessions.sign(first, "x")
    first.close()
    assert sessions.unsign(open_vault(path), signed) == "x"


def test_disabling_the_user_kills_the_sessions(vault: Vault) -> None:
    user = users.add_user(vault, "ana@exemplo.com", PASSWORD)
    users.add_user(vault, "bia@exemplo.com", PASSWORD)
    cookie = sessions.create_session(vault, user.id)
    users.disable_user(vault, "ana@exemplo.com")
    assert sessions.verify_session(vault, cookie) is None


# --------------------------------------------------------------------------- verificadores


def test_any_of_with_legacy_token(vault: Vault) -> None:
    legacy = "legado-" + "z" * 20
    raw, _ = tokens.create_token(vault, "cofre")
    verify = AnyOf(VaultTokenVerifier(vault), StaticTokenVerifier(legacy))
    assert verify(raw.encode()) and verify(legacy.encode())
    assert not verify(b"outro-token-qualquer") and not verify(b"ecm_" + b"q" * 43)
    with pytest.raises(ValueError, match="16 caracteres"):
        StaticTokenVerifier("curto")


# --------------------------------------------------------------------------- papéis


def test_roles_default_to_admin_and_are_validated(vault: Vault) -> None:
    assert users.add_user(vault, "ana@exemplo.com", PASSWORD).role == "admin"
    assert users.add_user(vault, "bia@exemplo.com", PASSWORD, "leitor").role == "leitor"
    with pytest.raises(AccessError, match="papel inválido"):
        users.add_user(vault, "cris@exemplo.com", PASSWORD, "dono")
    with pytest.raises(AccessError, match="papel inválido"):
        users.set_role(vault, "bia@exemplo.com", "dono")
    assert users.set_role(vault, "bia@exemplo.com", "admin").role == "admin"
    assert {u.username: u.role for u in users.list_users(vault)} == {
        "ana@exemplo.com": "admin",
        "bia@exemplo.com": "admin",
    }


def test_the_first_user_must_be_an_admin(vault: Vault) -> None:
    with pytest.raises(AccessError, match="primeiro usuário"):
        users.add_user(vault, "ana@exemplo.com", PASSWORD, "leitor")
    assert users.list_users(vault) == []


def test_the_last_active_admin_cannot_be_disabled_or_demoted(vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    users.add_user(vault, "bia@exemplo.com", PASSWORD, "leitor")
    with pytest.raises(AccessError, match="último administrador"):
        users.disable_user(vault, "ana@exemplo.com")
    with pytest.raises(AccessError, match="último administrador"):
        users.set_role(vault, "ana@exemplo.com", "leitor")
    assert users.get_user(vault, "ana@exemplo.com") is not None
    users.set_role(
        vault, "bia@exemplo.com", "admin"
    )  # com outro administrador, o primeiro pode sair
    assert users.set_role(vault, "ana@exemplo.com", "leitor").role == "leitor"
    with pytest.raises(AccessError, match="último administrador"):
        users.disable_user(vault, "bia@exemplo.com")
    users.disable_user(vault, "ana@exemplo.com")  # leitor não é admin: desativa à vontade


def test_a_disabled_admin_does_not_count_as_another_admin(vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    users.add_user(vault, "bia@exemplo.com", PASSWORD)
    users.disable_user(vault, "bia@exemplo.com")
    with pytest.raises(AccessError, match="último administrador"):
        users.set_role(vault, "ana@exemplo.com", "leitor")


def test_session_carries_the_current_role(vault: Vault) -> None:
    users.add_user(vault, "ana@exemplo.com", PASSWORD)
    bia = users.add_user(vault, "bia@exemplo.com", PASSWORD, "leitor")
    cookie = sessions.create_session(vault, bia.id)
    info = sessions.verify_session(vault, cookie)
    assert info is not None and info.role == "leitor"
    users.set_role(vault, "bia@exemplo.com", "admin")  # vale já na próxima requisição
    info = sessions.verify_session(vault, cookie)
    assert info is not None and info.role == "admin"


_VERSION_1 = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    failed_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    disabled_at TEXT
);
INSERT INTO meta VALUES ('schema_version', '1');
"""


def test_a_vault_from_before_the_roles_opens_with_everyone_as_admin(tmp_path: Path) -> None:
    path = tmp_path / "cofre" / "access.db"
    path.parent.mkdir()
    old = sqlite3.connect(path)
    old.executescript(_VERSION_1)
    stored = passwords.hash_password(PASSWORD)
    for name in ("ana@exemplo.com", "bia@exemplo.com"):
        old.execute(
            "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
            (name, stored, "2026-01-01T00:00:00Z"),
        )
    old.commit()
    old.close()

    vault = open_vault(path)
    assert vault.get_meta("schema_version") == "2"
    assert {u.username: u.role for u in users.list_users(vault)} == {
        "ana@exemplo.com": "admin",
        "bia@exemplo.com": "admin",
    }
    assert users.authenticate(vault, "ana@exemplo.com", PASSWORD) is not None
    vault.close()
    again = open_vault(path)  # abrir de novo não migra duas vezes
    assert len(users.list_users(again)) == 2
    with pytest.raises(sqlite3.IntegrityError):  # o CHECK da coluna veio junto
        again.run("UPDATE users SET role = 'dono' WHERE username = 'ana@exemplo.com'")


@pytest.mark.parametrize(
    "text",
    ["", "ana", "ana@", "@exemplo.com", "ana@exemplo", "ana@@exemplo.com", "a na@exemplo.com",
     "ana@exemplo..com", "ana@.com", "a" * 65 + "@exemplo.com", "ana@" + "d" * 250 + ".com"],
)  # fmt: skip
def test_check_email_refuses_malformed_addresses(text: str) -> None:
    with pytest.raises(AccessError, match="e-mail inválido"):
        users.check_email(text)


@pytest.mark.parametrize("text", ["ana@exemplo.com", "  Ana.Silva+php@Sub.Exemplo.com.br "])
def test_check_email_normalizes(text: str) -> None:
    assert users.check_email(text) == text.strip().lower()


def test_legacy_user_without_at_sign_still_signs_in_and_is_managed(vault: Vault) -> None:
    """Cofres criados antes do e-mail têm nomes simples: só a criação exige o formato."""
    vault.run(
        "INSERT INTO users(username, password_hash, created_at, role) VALUES (?, ?, ?, 'admin')",
        ("dono", passwords.hash_password(PASSWORD), "2026-01-01T00:00:00Z"),
    )
    assert users.authenticate(vault, "Dono", PASSWORD) is not None
    users.add_user(vault, "bia@exemplo.com", PASSWORD, users.READER)
    users.set_role(vault, "dono", users.ADMIN)
    with pytest.raises(AccessError, match="e-mail inválido"):
        users.add_user(vault, "dono2", PASSWORD, users.READER)
