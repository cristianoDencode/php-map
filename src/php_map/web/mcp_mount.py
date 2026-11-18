"""MCP montado na interface, em `/mcp`.

Um processo, uma porta, um túnel. O catálogo é dinâmico: vem dos projetos da interface a cada
consulta, então a troca de projeto e os scans novos aparecem sem reiniciar. A autenticação é o
bearer do cofre (mais o `PHP_MAP_MCP_TOKEN` legado, se houver); **não** vale o cookie de
sessão do login. O `source` fica desligado até o analista ligar na página Acesso, porque o código
do e-cidade tem `libs/db_conn.php` e `.env`.

O SDK valida o `Host` por conta própria; aqui essa proteção fica desligada porque o `HostGate`
(que conhece os hosts públicos do túnel) já recusa o que não vale, antes de chegar ao MCP.
"""

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from pathlib import Path
from typing import Any

from php_map.access.store import Vault
from php_map.config import env_value
from php_map.graph import store
from php_map.mcp.catalog import CatalogProject
from php_map.web.hostgate import MCP_PATHS
from php_map.web.state import AppState

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
NOT_INSTALLED = 'o MCP precisa do extra opcional: pip install -e ".[mcp]"'
DISABLED = "o MCP está desligado nesta interface (sem cofre de acesso ou com --no-mcp)"

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_META_CACHE: dict[tuple[str, int], dict[str, str]] = {}


def _meta(db: Path) -> dict[str, str] | None:
    try:
        key = (str(db), db.stat().st_mtime_ns)
    except OSError:
        return None
    cached = _META_CACHE.get(key)
    if cached is None:
        try:
            cached = store.load_meta(db)
        except Exception:  # banco ruim: o projeto some do catálogo, o resto segue
            logger.warning("MCP: grafo ignorado: %s", db)
            return None
        _META_CACHE[key] = cached
    return cached


def interface_projects(state: AppState) -> list[CatalogProject]:
    """Os projetos da interface com grafo (ou o banco servido, sem projetos cadastrados)."""
    found: list[CatalogProject] = []
    expose = state.mcp_expose_source
    for project in state.projects():
        db = project.graph_path()
        meta = _meta(db) if db is not None else None
        if db is None or meta is None:
            continue
        folder = Path(project.source) if project.source else None
        found.append(
            CatalogProject(
                project.slug,
                project.name,
                project.profile,
                db,
                folder if expose and folder is not None and folder.is_dir() else None,
                meta,
            )
        )
    if found:
        return found
    served = state.current_db()
    meta = _meta(served) if served is not None else None
    if served is None or meta is None:
        return []
    from php_map.analysis.cross import project_label

    slug = project_label(served, meta)[0]
    folder = Path(meta["source"]) if meta.get("source") else None
    source = folder if expose and folder is not None and folder.is_dir() else None
    return [
        CatalogProject(
            slug,
            meta.get("project_name") or slug,
            meta.get("profile", "ecidade"),
            served,
            source,
            meta,
        )
    ]


class _DenyAll:
    def __call__(self, token: bytes, client: str | None = None) -> bool:
        return False


class McpGateway:
    """O app do `/mcp` (com bearer) e o contexto que mantém o gerenciador de sessões do SDK."""

    def __init__(
        self,
        asgi: ASGIApp | None,
        run: Callable[[], AbstractAsyncContextManager[Any]],
        reason: str = NOT_INSTALLED,
    ):
        self.asgi = asgi
        self._run = run
        self.reason = reason

    @classmethod
    def disabled(cls) -> "McpGateway":
        return cls(None, nullcontext, DISABLED)

    @property
    def available(self) -> bool:
        return self.asgi is not None

    def run(self) -> AbstractAsyncContextManager[Any]:
        return self._run()


def build_gateway(state: AppState, vault: Vault | None) -> McpGateway:
    """Monta o MCP; sem o SDK `mcp` instalado, devolve um gateway indisponível (503)."""
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        from php_map.mcp import server as mcp_server
        from php_map.mcp.auth import (
            TOKEN_ENV,
            AnyOf,
            BearerAuth,
            StaticTokenVerifier,
            TokenVerifier,
            VaultTokenVerifier,
        )
        from php_map.mcp.catalog import ProjectCatalog
    except ImportError:
        return McpGateway(None, nullcontext)

    verifiers: list[TokenVerifier] = []
    if vault is not None:
        verifiers.append(VaultTokenVerifier(vault))
    legacy = env_value(TOKEN_ENV)
    if legacy is not None and len(legacy) >= 16:
        verifiers.append(StaticTokenVerifier(legacy))
    verify: TokenVerifier = (
        verifiers[0] if len(verifiers) == 1 else AnyOf(*verifiers) if verifiers else _DenyAll()
    )
    catalog = ProjectCatalog.dynamic(lambda: interface_projects(state))
    server = mcp_server.build_server(catalog, docs_dir=_docs_dir())
    sdk_app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def run() -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    return McpGateway(BearerAuth(sdk_app, verify), run)


def _docs_dir() -> Path | None:
    from php_map.mcp import server as mcp_server

    return mcp_server.default_docs_dir()


class McpRoute:
    """Entrega `/mcp` e `/mcp/` ao gateway **antes** das rotas da interface (a SPA captura tudo)."""

    def __init__(self, app: ASGIApp, gateway: McpGateway) -> None:
        self.app = app
        self.gateway = gateway

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or str(scope.get("path", "")) not in MCP_PATHS:
            await self.app(scope, receive, send)
            return
        if self.gateway.asgi is None:
            body = json.dumps({"error": self.gateway.reason}).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        forwarded = dict(scope, path=MCP_PATH, raw_path=MCP_PATH.encode("ascii"))
        await self.gateway.asgi(forwarded, receive, send)


def public_urls(hosts: Sequence[str]) -> list[str]:
    return [f"https://{h}{MCP_PATH}" for h in hosts]
