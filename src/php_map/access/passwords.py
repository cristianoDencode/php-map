"""Senhas do cofre: `scrypt` da biblioteca padrão.

Formato guardado: `scrypt$n$r$p$<sal b64>$<hash b64>`. Os parâmetros ficam no próprio hash,
então dá para endurecê-los depois sem invalidar as senhas já guardadas.
"""

import base64
import hashlib
import hmac
import secrets

from php_map.errors import AccessError

MIN_PASSWORD = 10
_N, _R, _P = 2**14, 8, 1
_MAXMEM = 64 * 1024 * 1024
_SALT_BYTES = 16
_DKLEN = 32


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _derive(password: str, salt: bytes, n: int, r: int, p: int, dklen: int = _DKLEN) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=dklen
    )


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AccessError(f"a senha precisa de pelo menos {MIN_PASSWORD} caracteres")


def hash_password(password: str) -> str:
    check_password_rules(password)
    salt = secrets.token_bytes(_SALT_BYTES)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(_derive(password, salt, _N, _R, _P))}"


def verify_password(password: str, stored: str) -> bool:
    """Compara em tempo constante. Hash malformado → `False` (nunca levanta)."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        derived = _derive(
            password, base64.b64decode(salt_b64), int(n), int(r), int(p), dklen=len(expected)
        )
    except (ValueError, TypeError, OverflowError):
        return False
    return hmac.compare_digest(derived, expected)


def dummy_verify(password: str) -> None:
    """Gasta o mesmo tempo de uma verificação real (usuário inexistente não vaza pelo relógio)."""
    _derive(password, b"\x00" * _SALT_BYTES, _N, _R, _P)
