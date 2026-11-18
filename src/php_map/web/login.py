"""Login da interface.

- `/auth/login`, `/auth/logout`, `/auth/me`, `/auth/first-user` e `/auth/password` em JSON; o
  usuário é identificado pelo e-mail (campo `email`; `username` ainda é aceito);
- `SessionGate`: `/graphql` só responde com uma sessão válida (401 `{"error": "login"}`); os
  arquivos estáticos da UI não têm dado e ficam abertos, a SPA decide a tela por `/auth/me`;
- sem usuário ativo o servidor está em modo **configuração**: `/graphql` responde 401
  `{"error": "setup"}` e o único caminho é `POST /auth/first-user` com o código de configuração
  de uso único que a CLI imprime no terminal (só em memória, nunca em arquivo nem em log).
  A tela de configuração só abre por um host local ou liberado em `--host`: o `HostGate` recusa
  qualquer outro.

A senha e o id da sessão nunca vão para o log. Todo `POST` exige `Origin` local e JSON, como as
mutações do GraphQL. O bloqueio de força bruta é em memória (por usuário e por IP), além do
bloqueio persistente do cofre.
"""

import hmac
import json
import logging
import secrets
import threading
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request, Response

from php_map.access import sessions, users
from php_map.access.store import Vault
from php_map.errors import AccessError
from php_map.web.state import AppState

logger = logging.getLogger(__name__)

COOKIE = "ecm_session"
INVALID = "usuário ou senha inválidos"
PROTECTED_PREFIXES = ("/graphql",)
SETUP_CODE_BYTES = 12
SETUP_BUCKET = "\0setup"  # o código de configuração só conta por IP, nunca por nome de usuário
USER_FAILURES = 5
IP_ATTEMPTS = 20
WINDOW = timedelta(minutes=15)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class Throttle:
    """Tentativas de login em memória: 5 falhas por usuário e 20 tentativas por IP em 15 min."""

    def __init__(self, vault: Vault) -> None:
        self._vault = vault
        self._by_user: dict[str, deque[Any]] = defaultdict(deque)
        self._by_ip: dict[str, deque[Any]] = defaultdict(deque)

    def _trim(self, hits: deque[Any]) -> None:
        cutoff = self._vault.now() - WINDOW
        while hits and hits[0] <= cutoff:
            hits.popleft()

    def _wait(self, hits: deque[Any], limit: int) -> int:
        """Minutos até liberar (0 = livre)."""
        self._trim(hits)
        if len(hits) < limit:
            return 0
        remaining = hits[0] + WINDOW - self._vault.now()
        return max(1, int(remaining.total_seconds() // 60) + 1)

    def blocked(self, username: str, ip: str) -> int:
        return max(
            self._wait(self._by_user[username.strip().lower()], USER_FAILURES),
            self._wait(self._by_ip[ip], IP_ATTEMPTS),
        )

    def attempt(self, ip: str) -> None:
        self._by_ip[ip].append(self._vault.now())

    def failure(self, username: str) -> None:
        self._by_user[username.strip().lower()].append(self._vault.now())

    def success(self, username: str) -> None:
        self._by_user.pop(username.strip().lower(), None)


class Access:
    """O cofre e o que o login precisa dele; guardado em `app.state.access`.

    Sem usuário ativo gera o código de configuração (`setup_code`): vale uma vez, fica só na
    memória deste processo e some ao criar o primeiro usuário ou ao reiniciar.
    """

    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        self.throttle = Throttle(vault)
        self._setup_lock = threading.Lock()
        self._setup_code: str | None = None
        if self.setup_required():
            self._setup_code = secrets.token_urlsafe(SETUP_CODE_BYTES)

    def setup_required(self) -> bool:
        """Nenhum usuário ativo no cofre: só a tela de configuração inicial funciona."""
        return not users.has_active_users(self.vault)

    @property
    def setup_code(self) -> str | None:
        return self._setup_code if self.setup_required() else None

    def create_first_user(self, code: str, username: str, password: str) -> users.User | None:
        """Cria o primeiro usuário se `code` for o código de configuração; `None` com o código
        errado ou já usado. Os erros da senha (`AccessError`) não gastam o código."""
        with self._setup_lock:
            expected = self.setup_code
            if expected is None or not hmac.compare_digest(
                code.encode("utf-8"), expected.encode("utf-8")
            ):
                return None
            user = users.add_user(self.vault, username, password, users.ADMIN)
            self._setup_code = None
            return user

    def current(self, cookie: str | None) -> sessions.SessionInfo | None:
        return sessions.verify_session(self.vault, cookie) if cookie else None


def _json(status: int, payload: dict[str, Any]) -> Response:
    return Response(json.dumps(payload), status_code=status, media_type="application/json")


def _is_https(request: Request) -> bool:
    return (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
    )


def _set_cookie(response: Response, request: Request, value: str) -> None:
    response.set_cookie(
        COOKIE,
        value,
        max_age=int(sessions.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="strict",
        secure=_is_https(request),
        path="/",
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


async def _body(request: Request) -> dict[str, Any] | None:
    try:
        data = await request.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _login_name(body: dict[str, Any] | None) -> str:
    """O e-mail do corpo: `email` (atual) ou `username` (nome antigo do campo)."""
    if not body:
        return ""
    return str(body.get("email") or body.get("username") or "")


def _guard(request: Request, state: AppState) -> Response | None:
    """`Origin` local e `application/json` (a mesma regra das mutações do GraphQL)."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if request.headers.get("origin") not in state.allowed_origins or content_type != (
        "application/json"
    ):
        return _json(403, {"error": "origem não permitida"})
    return None


def build_router(access: Access, state: AppState) -> APIRouter:
    router = APIRouter(prefix="/auth")
    vault = access.vault

    @router.get("/me")
    def me(request: Request) -> Response:
        session = access.current(request.cookies.get(COOKIE))
        return _json(
            200,
            {
                "loginRequired": True,
                "setupRequired": access.setup_required(),
                "user": (
                    {"username": session.username, "role": session.role}
                    if session is not None
                    else None
                ),
            },
        )

    @router.post("/login")
    async def login(request: Request) -> Response:
        if (refused := _guard(request, state)) is not None:
            return refused
        body = await _body(request)
        username = _login_name(body)
        password = str(body.get("password", "")) if body else ""
        ip = _client_ip(request)
        if not username or not password:
            return _json(401, {"error": INVALID})
        if (wait := access.throttle.blocked(username, ip)) > 0:
            logger.warning("login bloqueado para %s de %s", username[:40], ip)
            return _json(429, {"error": f"muitas tentativas; tente de novo em {wait} min"})
        access.throttle.attempt(ip)
        user = users.authenticate(vault, username, password)
        if user is None:
            access.throttle.failure(username)
            logger.warning("login falhou para %s de %s", username[:40], ip)
            return _json(401, {"error": INVALID})
        access.throttle.success(username)
        logger.info("login ok para %s de %s", user.username, ip)
        cookie = sessions.create_session(
            vault, user.id, ip=ip, user_agent=request.headers.get("user-agent")
        )
        response = Response(status_code=204)
        _set_cookie(response, request, cookie)
        return response

    @router.post("/logout")
    async def logout(request: Request) -> Response:
        if (refused := _guard(request, state)) is not None:
            return refused
        cookie = request.cookies.get(COOKIE)
        if cookie:
            sessions.revoke_session(vault, cookie)
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path="/")
        return response

    @router.post("/first-user")
    async def first_user(request: Request) -> Response:
        if (refused := _guard(request, state)) is not None:
            return refused
        if not access.setup_required():
            return _json(409, {"error": "já existe usuário neste cofre"})
        ip = _client_ip(request)
        if (wait := access.throttle.blocked(SETUP_BUCKET, ip)) > 0:
            logger.warning("configuração inicial bloqueada para %s", ip)
            return _json(429, {"error": f"muitas tentativas; tente de novo em {wait} min"})
        body = await _body(request)
        access.throttle.attempt(ip)
        try:
            user = access.create_first_user(
                str(body.get("setupCode", "")) if body else "",
                _login_name(body),
                str(body.get("password", "")) if body else "",
            )
        except AccessError as exc:
            return _json(400, {"error": str(exc)})
        if user is None:
            logger.warning("configuração inicial: código recusado para %s", ip)
            return _json(403, {"error": "setup-code"})
        logger.info("primeiro usuário criado pela interface: %s", user.username)
        cookie = sessions.create_session(
            vault, user.id, ip=ip, user_agent=request.headers.get("user-agent")
        )
        response = Response(status_code=201)
        _set_cookie(response, request, cookie)
        return response

    @router.post("/password")
    async def password(request: Request) -> Response:
        if (refused := _guard(request, state)) is not None:
            return refused
        session = access.current(request.cookies.get(COOKIE))
        if session is None:
            return _json(401, {"error": "login"})
        body = await _body(request)
        current = str(body.get("current", "")) if body else ""
        new = str(body.get("new", "")) if body else ""
        ip = _client_ip(request)
        if (wait := access.throttle.blocked(session.username, ip)) > 0:
            return _json(429, {"error": f"muitas tentativas; tente de novo em {wait} min"})
        if users.authenticate(vault, session.username, current) is None:
            access.throttle.failure(session.username)
            return _json(400, {"error": "a senha atual não confere"})
        try:
            users.set_password(vault, session.username, new)
        except AccessError as exc:
            return _json(400, {"error": str(exc)})
        logger.info("senha trocada para %s", session.username)
        return Response(status_code=204)

    return router


class SessionGate:
    """Middleware ASGI: `/graphql` exige sessão; sem usuário ativo, responde 401 `setup`."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]], access: Access):
        self.app = app
        self.access = access

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        path = str(scope.get("path", ""))
        if kind not in ("http", "websocket") or not path.startswith(PROTECTED_PREFIXES):
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return
        # Sem usuário ativo nenhuma sessão vale: só a configuração inicial (`/auth/first-user`).
        setup = self.access.setup_required()
        session = None if setup else self.access.current(_cookie_from(scope))
        if session is None:
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            body = json.dumps({"error": "setup" if setup else "login"}).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        state = scope.setdefault("state", {})
        state["user"] = session.username
        state["role"] = session.role
        await self.app(scope, receive, send)


def _cookie_from(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() == b"cookie":
            for part in bytes(value).decode("latin-1").split(";"):
                key, _, val = part.strip().partition("=")
                if key == COOKIE:
                    return val
    return None
