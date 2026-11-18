"""Servidor da interface: FastAPI + Strawberry.

- `/graphql`: a API (com o GraphiQL no navegador): consultas só leitura e as mutações do scan
  pela interface, protegidas por `Origin` e desligadas com `--read-only`;
- `/`: a interface compilada (`ui/dist`), com fallback para `index.html` (rotas do
  React). Sem `ui/dist`, uma página explica como gerar o build, e o `/graphql` continua no ar;
- com cofre de acesso, `login.py` exige sessão em `/graphql`;
- toda requisição precisa de `Host` local (contra DNS rebinding), e o CORS só libera o
  `npm run dev` do Vite.
"""

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from strawberry.fastapi import GraphQLRouter

from php_map.access.store import Vault
from php_map.config import PROJECT_ROOT
from php_map.errors import ConfigError
from php_map.web.hostgate import HostGate
from php_map.web.login import Access, SessionGate, build_router
from php_map.web.mcp_mount import McpGateway, McpRoute, build_gateway
from php_map.web.schema import Context, schema
from php_map.web.state import AppState
from php_map.web.tunnel import (
    FAILED,
    HealthCheck,
    TunnelConfigStore,
    TunnelJob,
    TunnelManager,
    http_health,
)

logger = logging.getLogger(__name__)

UI_DIST = PROJECT_ROOT / "ui" / "dist"
LOCAL_HOSTS = ("127.0.0.1", "localhost")
DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")

NO_BUILD = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>php-map</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 42rem; margin: 3rem auto">
<h1>php-map</h1>
<p>A interface ainda não foi compilada. Rode:</p>
<pre>cd ui
npm install
npm run build</pre>
<p>A API já está no ar em <a href="/graphql">/graphql</a>.</p>
</body></html>
"""


def _graph_dbs(state: AppState) -> list[Path]:
    """Os grafos que o túnel exporia: os dos projetos (ou o servido, sem projetos)."""
    found = [g for p in state.projects() if (g := p.graph_path()) is not None]
    served = state.current_db()
    return found or ([served] if served is not None else [])


def _autostart(manager: TunnelManager, notify: Callable[[TunnelJob], None] | None) -> None:
    """`ui --tunnel`: sobe o túnel salvo; sem configuração, avisa em vez de derrubar o servidor."""
    try:
        manager.start()
    except ConfigError as exc:
        logger.error("túnel não iniciado: %s", exc)
        if notify is not None:
            notify(TunnelJob(FAILED, None, None, None, str(exc), (), (), None))


def create_app(
    db_path: Path | None,
    ui_dir: Path | None = UI_DIST,
    source: Path | None = None,
    allowed_hosts: Sequence[str] = LOCAL_HOSTS,
    *,
    out_dir: Path | None = None,
    read_only: bool = False,
    port: int = 8765,
    scan_command: Sequence[str] | None = None,
    explicit_db: bool = True,
    vault: Vault | None = None,
    public_hosts: Sequence[str] = (),
    mcp: bool | None = None,
    expose_source: bool = False,
    tunnel: bool = False,
    on_tunnel: Callable[[TunnelJob], None] | None = None,
    tunnel_health: HealthCheck | None = None,
) -> FastAPI:
    """App do `php-map ui`. `db_path` já validado por quem chama (a CLI); `out_dir` é a
    pasta do `--db` original (configurações, projetos, cache de AST e `grafos/`). Com projetos
    cadastrados, `explicit_db=False` deixa valer o projeto ativo salvo; `db_path`
    pode ser `None` quando só há projetos (o servidor sobe e a interface oferece o scan).
    `vault` liga o login: com ao menos um usuário ativo, `/graphql`
    exigem sessão. Sem `vault`, nada muda.
    `mcp` monta o MCP em `/mcp` (padrão: quando há `vault` e o extra `[mcp]` existe),
    com os tokens do cofre; `public_hosts` são os hosts de um túnel, que só alcançam `/mcp`;
    `expose_source` liga a ferramenta `source` do MCP.
    Com cofre, monta o `TunnelManager` (túnel do `/mcp`); `tunnel` sobe o túnel salvo
    junto com o servidor e `on_tunnel` recebe cada mudança de estado dele."""
    state = AppState(
        db_path=db_path,
        arg_source=source,
        out_dir=out_dir,
        read_only=read_only,
        port=port,
        scan_command=scan_command,
        explicit_db=explicit_db,
        public_hosts=[h.strip().lower() for h in public_hosts if h.strip()],
        mcp_expose_source=expose_source,
        vault=vault,
    )
    enabled = vault is not None if mcp is None else mcp
    gateway = build_gateway(state, vault) if enabled else McpGateway.disabled()
    state.mcp_available = gateway.available
    if vault is not None:
        state.tunnel = TunnelManager(
            state,
            vault,
            TunnelConfigStore(vault.path.parent),
            databases=lambda: _graph_dbs(state),
            on_change=on_tunnel,
            health=tunnel_health or http_health,
        )

    async def get_context() -> AsyncIterator[Context]:
        context = Context(state)
        try:
            yield context
        finally:
            context.close()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if gateway.available:
                await stack.enter_async_context(gateway.run())  # sem isto o `/mcp` não atende
            if tunnel and state.tunnel is not None:
                _autostart(state.tunnel, on_tunnel)
            yield
        if state.tunnel is not None:
            state.tunnel.shutdown()  # nenhum túnel nem processo para trás
        state.jobs.shutdown()  # servidor encerrando: nenhum scan nem container para trás

    app = FastAPI(
        title="php-map", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.ecmap = state
    app.include_router(GraphQLRouter(schema, context_getter=get_context), prefix="/graphql")
    # Do mais interno ao mais externo: `/mcp`, sessão do login, CORS do Vite e, por fora, o `Host`.
    app.add_middleware(McpRoute, gateway=gateway)
    if vault is not None:
        access = Access(vault)
        app.state.access = access
        app.include_router(build_router(access, state))
        app.add_middleware(SessionGate, access=access)  # dentro do CORS e do TrustedHost
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEV_ORIGINS),
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
    )
    # Por último: é o mais externo, então recusa o `Host` antes de qualquer outra coisa. Hosts
    # públicos (túnel) só alcançam `/mcp`.
    local_hosts = [h.lower() for h in allowed_hosts]
    app.add_middleware(
        HostGate,
        allowed=lambda: [*local_hosts, *state.public_host_list()],
        public=state.public_host_list,
    )

    index = ui_dir / "index.html" if ui_dir is not None else None
    if ui_dir is None or index is None or not index.is_file():

        @app.get("/", response_class=HTMLResponse)
        def no_build() -> str:
            return NO_BUILD

        return app

    root = ui_dir.resolve()

    @app.get("/{path:path}", response_model=None)
    def static(path: str) -> Response:
        target = (root / path).resolve()
        if path and target.is_relative_to(root) and target.is_file():
            return FileResponse(target)
        return FileResponse(root / "index.html")  # rota do React (`/no/…`, `/busca`)

    return app
