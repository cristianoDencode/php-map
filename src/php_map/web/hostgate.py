"""Porteiro de `Host` da interface.

Troca o `TrustedHostMiddleware` estático: a lista de hosts aceitos muda com o tempo (o host do
túnel só é conhecido depois que ele sobe). Duas regras:

1. `Host` fora da lista (locais + públicos) → 400, como antes (contra DNS rebinding);
2. requisição **remota** só alcança `/mcp`; o resto → 404 sem corpo útil. É remota quando o `Host`
   é um host público **ou** traz `X-Forwarded-For`, `X-Forwarded-Host` ou `Forwarded`.

Esses cabeçalhos podem ser forjados por quem chega direto em localhost. Isso só torna a
requisição **mais** restrita, nunca menos: eles nunca liberam nada.
"""

import logging
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

FORWARD_HEADERS = (b"x-forwarded-for", b"x-forwarded-host", b"forwarded")
MCP_PATHS = ("/mcp", "/mcp/")

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


def host_name(raw: str) -> str:
    """O nome do `Host` sem a porta (`[::1]:8765` → `[::1]`), em minúsculas."""
    text = raw.strip().lower()
    return text[: text.index("]") + 1] if text.startswith("[") else text.partition(":")[0]


def _headers(scope: Scope) -> dict[bytes, bytes]:
    return {bytes(k).lower(): bytes(v) for k, v in scope.get("headers", [])}


def is_remote(scope: Scope, public_hosts: Sequence[str]) -> bool:
    headers = _headers(scope)
    host = host_name(headers.get(b"host", b"").decode("latin-1"))
    return host in public_hosts or any(name in headers for name in FORWARD_HEADERS)


class HostGate:
    """Middleware ASGI. `allowed` e `public` são funções, para a lista poder mudar."""

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        allowed: Callable[[], Sequence[str]],
        public: Callable[[], Sequence[str]],
    ) -> None:
        self.app = app
        self._allowed = allowed
        self._public = public

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)
        host = host_name(headers.get(b"host", b"").decode("latin-1"))
        if host not in {h.lower() for h in self._allowed()}:
            await _refuse(scope, send, 400, b"Invalid host header")
            return
        path = str(scope.get("path", ""))
        if is_remote(scope, [h.lower() for h in self._public()]) and path not in MCP_PATHS:
            who = headers.get(b"x-forwarded-for", b"?").decode("latin-1").split(",")[0].strip()
            logger.warning("acesso remoto recusado: %s de %s", path[:80], who[:60])
            await _refuse(scope, send, 404, b"")
            return
        await self.app(scope, receive, send)


async def _refuse(scope: Scope, send: Send, status: int, body: bytes) -> None:
    if scope.get("type") == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
