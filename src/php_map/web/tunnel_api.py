"""API do túnel: configuração, estado e controle.

As consultas só leem. As mutações passam pela `_guard` (`Origin` local, JSON, `--read-only` desliga)
e, com login exigido, pela sessão. Nenhuma resposta traz o valor de variável de ambiente: a
configuração só tem os NOMES, e a página vê só se cada um está definido. O log do túnel já vem
mascarado de `tunnel.mask_line`.
"""

import time
from collections.abc import Callable
from typing import Any, TypeVar, cast

import strawberry

from php_map.config import env_value
from php_map.errors import ConfigError, QueryError
from php_map.web import tunnel as tunnel_module
from php_map.web.mcp_mount import MCP_PATH

F = TypeVar("F", bound=Callable[..., Any])
NO_TUNNEL = "o túnel precisa do cofre de acesso (suba a interface com `php-map ui`)"


def described(text: str) -> Callable[[F], F]:
    return cast(Callable[[F], F], strawberry.field(description=text))


def _ctx(info: strawberry.Info) -> Any:
    return info.context


def _guard(info: strawberry.Info) -> Any:
    from php_map.web.schema import _guard as guard  # evita o ciclo de importação

    return guard(info)


def _manager(ctx: Any) -> tunnel_module.TunnelManager:
    manager = ctx.state.tunnel
    if manager is None:
        raise QueryError(NO_TUNNEL)
    return cast(tunnel_module.TunnelManager, manager)


@strawberry.type
class TunnelEnvVar:
    name: str
    defined: bool  # o valor nunca sai do servidor


@strawberry.type
class TunnelExample:
    id: str
    label: str
    command: list[str]
    public_url: str | None
    url_regex: str
    env_names: list[str]


@strawberry.type
class TunnelConfigInfo:
    configured: bool
    command: list[str]
    public_url: str | None
    url_regex: str
    env: list[TunnelEnvVar]
    examples: list[TunnelExample]


@strawberry.type
class TunnelFinding:
    kind: str
    where: str
    masked: str
    hash: str


@strawberry.type
class TunnelStatus:
    state: str  # parado | iniciando | no-ar | caiu | falhou
    stage: str | None
    stage_label: str | None
    url: str | None
    public_host: str | None
    error: str | None
    log: list[str]
    findings: list[TunnelFinding]
    elapsed_seconds: int
    can_start: bool
    blocked_reason: str | None


def _config_info(ctx: Any) -> TunnelConfigInfo:
    manager = ctx.state.tunnel
    config = manager.store.load() if manager is not None else None
    examples = [
        TunnelExample(
            id=e["id"],
            label=e["label"],
            command=list(e["command"]),
            public_url=e["public_url"],
            url_regex=e["url_regex"],
            env_names=list(e["env_names"]),
        )
        for e in tunnel_module.EXAMPLES
    ]
    names = config.env_names if config is not None else ()
    return TunnelConfigInfo(
        configured=config is not None,
        command=list(config.command) if config is not None else [],
        public_url=config.public_url if config is not None else None,
        url_regex=config.url_regex if config is not None else tunnel_module.DEFAULT_URL_REGEX,
        env=[TunnelEnvVar(name=n, defined=env_value(n) is not None) for n in names],
        examples=examples,
    )


def _status(ctx: Any) -> TunnelStatus:
    manager = ctx.state.tunnel
    if manager is None:
        return TunnelStatus(
            state="parado", stage=None, stage_label=None, url=None, public_host=None,
            error=NO_TUNNEL, log=[], findings=[], elapsed_seconds=0, can_start=False,
            blocked_reason=NO_TUNNEL,
        )  # fmt: skip
    job = manager.current()
    blocked = manager.blocked_reason()
    if manager.store.load() is None and blocked is None:
        blocked = "configure o comando do túnel"
    if ctx.state.read_only and blocked is None:
        blocked = "a interface está em modo somente leitura"
    if job is None:
        return TunnelStatus(
            state="parado",
            stage=None,
            stage_label=None,
            url=None,
            public_host=None,
            error=None,
            log=[],
            findings=[],
            elapsed_seconds=0,
            can_start=blocked is None,
            blocked_reason=blocked,
        )
    running = job.state in (tunnel_module.STARTING, tunnel_module.UP)
    return TunnelStatus(
        state=job.state,
        stage=job.stage,
        stage_label=tunnel_module.STAGES.get(job.stage or ""),
        url=job.url,
        public_host=job.host,
        error=job.error,
        log=list(job.log),
        findings=[
            TunnelFinding(kind=f.kind, where=f.where, masked=f.masked, hash=f.hash)
            for f in job.findings
        ],
        elapsed_seconds=int(time.monotonic() - job.started_at) if running and job.started_at else 0,
        can_start=blocked is None,
        blocked_reason=blocked,
    )


@strawberry.type
class TunnelQueries:
    @described("A configuração do túnel (só nomes de variáveis) e os exemplos prontos.")
    def tunnel_config(self, info: strawberry.Info) -> TunnelConfigInfo:
        return _config_info(_ctx(info))

    @described("Estado do túnel: etapa, URL pública do MCP, log mascarado e achados de segredo.")
    def tunnel_status(self, info: strawberry.Info) -> TunnelStatus:
        return _status(_ctx(info))


@strawberry.type
class TunnelMutations:
    @described(
        "Grava o comando do túnel (`{port}` e `{host}` são trocados) e os NOMES das variáveis."
    )
    def save_tunnel_config(
        self,
        info: strawberry.Info,
        command: list[str],
        public_url: str | None = None,
        url_regex: str | None = None,
        env_names: list[str] | None = None,
    ) -> TunnelConfigInfo:
        ctx = _guard(info)
        manager = _manager(ctx)
        try:
            config = tunnel_module.validate_config(command, public_url, url_regex, env_names or [])
        except ConfigError as exc:
            raise QueryError(str(exc)) from exc
        manager.store.save(config)
        return _config_info(ctx)

    @described("Sobe o túnel: pré-checagens, varredura de segredos, URL pública e health.")
    def start_tunnel(self, info: strawberry.Info) -> TunnelStatus:
        ctx = _guard(info)
        try:
            _manager(ctx).start()
        except ConfigError as exc:
            raise QueryError(str(exc)) from exc
        return _status(ctx)

    @described("Para o túnel (mata o processo e tira o host público).")
    def stop_tunnel(self, info: strawberry.Info) -> TunnelStatus:
        ctx = _guard(info)
        _manager(ctx).stop()
        return _status(ctx)


def mcp_url(host: str) -> str:
    return f"https://{host}{MCP_PATH}"
