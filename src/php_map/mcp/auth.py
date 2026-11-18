"""Autenticação do transporte `http` do MCP.

Segunda camada, depois do Cloudflare Access: `Authorization: Bearer <token>`, com comparação em
tempo constante. O token nunca aparece em log nem em mensagem de erro. Escopos que não são HTTP
(`lifespan`) passam direto, para o gerenciador de sessões do SDK subir e descer.

Quem decide se o token vale é um `TokenVerifier`. Há o token único do
ambiente (`StaticTokenVerifier`, o legado do celular), os tokens do cofre (`VaultTokenVerifier`)
e `AnyOf`, que aceita se algum aprovar.
"""

import hmac
import json
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from php_map.access.store import Vault

logger = logging.getLogger(__name__)

TOKEN_ENV = "PHP_MAP_MCP_TOKEN"
MIN_TOKEN = 16

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _bearer(scope: Scope) -> bytes | None:
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            scheme, _, token = bytes(value).partition(b" ")
            return token.strip() if scheme.lower() == b"bearer" else None
    return None


class TokenVerifier(Protocol):
    """Decide se o bearer vale. `client` é o IP de quem chamou (vai para o `last_used_ip`)."""

    def __call__(self, token: bytes, client: str | None = None) -> bool: ...


class StaticTokenVerifier:
    """O token único de `PHP_MAP_MCP_TOKEN` (legado do celular)."""

    def __init__(self, token: str) -> None:
        if len(token) < MIN_TOKEN:
            raise ValueError(f"o token do MCP precisa de pelo menos {MIN_TOKEN} caracteres")
        self._token = token.encode("utf-8")

    def __call__(self, token: bytes, client: str | None = None) -> bool:
        return hmac.compare_digest(token, self._token)


class VaultTokenVerifier:
    """Os tokens do cofre de acesso: existentes, não revogados e não vencidos."""

    def __init__(self, vault: "Vault") -> None:
        self._vault = vault

    def __call__(self, token: bytes, client: str | None = None) -> bool:
        from php_map.access.tokens import verify_token

        return verify_token(self._vault, token, ip=client) is not None


class AnyOf:
    """Aceita o token se pelo menos um verificador aceitar."""

    def __init__(self, *verifiers: TokenVerifier) -> None:
        self._verifiers = verifiers

    def __call__(self, token: bytes, client: str | None = None) -> bool:
        return any(v(token, client) for v in self._verifiers)


class BearerAuth:
    """Middleware ASGI: 401 sem o bearer certo. Aceita um `str` (token único) ou um verificador."""

    def __init__(self, app: ASGIApp, token: "str | TokenVerifier") -> None:
        self.app = app
        self._verify: TokenVerifier = (
            StaticTokenVerifier(token) if isinstance(token, str) else token
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        given = _bearer(scope)
        client = scope.get("client") or ("?", 0)
        if given is not None and self._verify(given, client[0]):
            await self.app(scope, receive, send)
            return
        logger.warning("MCP: requisição sem bearer válido de %s", client[0])
        body = json.dumps({"error": "unauthorized"}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="php-map"'),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
