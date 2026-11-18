"""API do acesso: tokens do MCP, usuários e o estado do `/mcp`.

As consultas só leem. As mutações passam pela `_guard` (`Origin` local, JSON, `--read-only`
desliga) e, com login exigido, pela sessão (o `SessionGate` já barra `/graphql`).
O token novo só existe na resposta de `createAccessToken`: o cofre guarda o hash. Nunca há senha,
hash ou token completo em outra resposta.
"""

from collections.abc import Callable
from typing import Any, TypeVar, cast

import strawberry

from php_map.access import tokens as access_tokens
from php_map.access import users as access_users
from php_map.access.store import Vault, from_text
from php_map.config import env_value
from php_map.errors import AccessError, QueryError
from php_map.web.mcp_mount import MCP_PATH, public_urls

F = TypeVar("F", bound=Callable[..., Any])

NO_VAULT = "o cofre de acesso não está ligado nesta interface"
EXPIRES_DAYS = (30, 90, 180, 365)


def described(text: str) -> Callable[[F], F]:
    return cast(Callable[[F], F], strawberry.field(description=text))


def _ctx(info: strawberry.Info) -> Any:
    return info.context


def _guard(info: strawberry.Info) -> Any:
    from php_map.web.schema import _guard as guard  # evita o ciclo de importação

    return guard(info)


def _vault(ctx: Any) -> Vault:
    vault = ctx.state.vault
    if vault is None:
        raise QueryError(NO_VAULT)
    return cast(Vault, vault)


def _wrap(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except AccessError as exc:
        raise QueryError(str(exc)) from exc


@strawberry.type(description="Um token do MCP. O valor nunca é devolvido, só o prefixo.")
class AccessToken:
    id: int
    name: str
    prefix: str
    created_at: str
    created_by: str | None
    expires_at: str | None
    last_used_at: str | None
    last_used_ip: str | None
    state: str  # ativo | vencido | revogado

    @classmethod
    def of(cls, info: access_tokens.TokenInfo, vault: Vault) -> "AccessToken":
        return cls(
            id=info.id,
            name=info.name,
            prefix=info.prefix,
            created_at=info.created_at,
            created_by=info.created_by,
            expires_at=info.expires_at,
            last_used_at=info.last_used_at,
            last_used_ip=info.last_used_ip,
            state=info.state(vault.now()),
        )


@strawberry.type(description="O token recém-criado: o valor só aparece nesta resposta.")
class CreatedAccessToken:
    token: str
    info: AccessToken


@strawberry.type
class AccessUser:
    username: str
    role: str  # admin | leitor
    active: bool
    created_at: str
    last_login_at: str | None
    locked: bool
    is_current: bool


@strawberry.type
class McpStatus:
    available: bool
    reason: str | None
    path: str
    local_url: str
    public_urls: list[str]
    expose_source: bool
    active_tokens: int
    legacy_token: bool
    vault_available: bool


def _user(user: access_users.User, vault: Vault, current: str | None) -> AccessUser:
    locked = from_text(user.locked_until)
    return AccessUser(
        username=user.username,
        role=user.role,
        active=user.active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        locked=locked is not None and locked > vault.now(),
        is_current=current is not None and current.lower() == user.username.lower(),
    )


def _status(ctx: Any) -> McpStatus:
    from php_map.mcp.auth import TOKEN_ENV

    state = ctx.state
    vault: Vault | None = state.vault
    active = 0
    if vault is not None:
        now = vault.now()
        active = sum(1 for t in access_tokens.list_tokens(vault) if t.state(now) == "ativo")
    gateway_reason = None
    if not state.mcp_available:
        from php_map.web.mcp_mount import DISABLED, NOT_INSTALLED

        gateway_reason = NOT_INSTALLED if vault is not None else DISABLED
    return McpStatus(
        available=bool(state.mcp_available),
        reason=gateway_reason,
        path=MCP_PATH,
        local_url=f"http://127.0.0.1:{state.port}{MCP_PATH}",
        public_urls=public_urls(state.public_host_list()),
        expose_source=bool(state.mcp_expose_source),
        active_tokens=active,
        legacy_token=env_value(TOKEN_ENV) is not None,
        vault_available=vault is not None,
    )


@strawberry.type
class AccessQueries:
    @described("Tokens do MCP (nunca o valor, só o prefixo).")
    def access_tokens(self, info: strawberry.Info) -> list[AccessToken]:
        vault = _vault(_ctx(info))
        return [AccessToken.of(t, vault) for t in access_tokens.list_tokens(vault)]

    @described("Usuários do login da interface (nunca a senha).")
    def access_users(self, info: strawberry.Info) -> list[AccessUser]:
        ctx = _ctx(info)
        vault = _vault(ctx)
        return [_user(u, vault, ctx.user) for u in access_users.list_users(vault)]

    @described("O MCP montado em `/mcp`: estado, endereços, código-fonte e tokens ativos.")
    def mcp_status(self, info: strawberry.Info) -> McpStatus:
        return _status(_ctx(info))


@strawberry.type
class AccessMutations:
    @described("Cria um token do MCP. `expiresDays` ∈ 30, 90, 180, 365 ou nulo (sem validade).")
    def create_access_token(
        self, info: strawberry.Info, name: str, expires_days: int | None = 90
    ) -> CreatedAccessToken:
        ctx = _guard(info)
        vault = _vault(ctx)
        if expires_days is not None and expires_days not in EXPIRES_DAYS:
            raise QueryError("validade inválida: 30, 90, 180, 365 dias ou sem validade")
        raw, created = _wrap(
            lambda: access_tokens.create_token(
                vault,
                name,
                expires="never" if expires_days is None else f"{expires_days}d",
                created_by=ctx.user or "interface",
            )
        )
        return CreatedAccessToken(token=raw, info=AccessToken.of(created, vault))

    @described("Revoga o token: a próxima chamada do cliente recebe 401.")
    def revoke_access_token(self, info: strawberry.Info, id: int) -> AccessToken:
        ctx = _guard(info)
        vault = _vault(ctx)
        revoked = _wrap(lambda: access_tokens.revoke_token(vault, str(id)))
        return AccessToken.of(revoked, vault)

    @described(
        "Cria um usuário (senha com pelo menos 10 caracteres). `role`: `admin` ou `leitor` "
        "(padrão `leitor`, que só consulta)."
    )
    def add_access_user(
        self, info: strawberry.Info, username: str, password: str, role: str = access_users.READER
    ) -> AccessUser:
        ctx = _guard(info)
        vault = _vault(ctx)
        created = _wrap(lambda: access_users.add_user(vault, username, password, role))
        return _user(created, vault, ctx.user)

    @described("Troca o papel do usuário. Não deixa rebaixar o último administrador ativo.")
    def set_access_user_role(self, info: strawberry.Info, username: str, role: str) -> AccessUser:
        ctx = _guard(info)
        vault = _vault(ctx)
        return _user(_wrap(lambda: access_users.set_role(vault, username, role)), vault, ctx.user)

    @described("Desativa o usuário. Não deixa desativar a si mesmo nem o último administrador.")
    def disable_access_user(self, info: strawberry.Info, username: str) -> AccessUser:
        ctx = _guard(info)
        vault = _vault(ctx)
        target = _wrap(lambda: access_users.get_user(vault, username))
        if target is None:
            raise QueryError(f"usuário '{username}' não encontrado")
        if ctx.user is not None and ctx.user.lower() == target.username.lower():
            raise QueryError("você não pode desativar o próprio usuário")
        return _user(_wrap(lambda: access_users.disable_user(vault, username)), vault, ctx.user)

    @described("Reativa um usuário desativado.")
    def enable_access_user(self, info: strawberry.Info, username: str) -> AccessUser:
        ctx = _guard(info)
        vault = _vault(ctx)
        return _user(_wrap(lambda: access_users.enable_user(vault, username)), vault, ctx.user)

    @described("Define uma senha nova para o usuário (e desbloqueia a conta).")
    def reset_access_user_password(
        self, info: strawberry.Info, username: str, password: str
    ) -> AccessUser:
        ctx = _guard(info)
        vault = _vault(ctx)
        changed = _wrap(lambda: access_users.set_password(vault, username, password))
        return _user(changed, vault, ctx.user)

    @described("Liga ou desliga a leitura do código-fonte pelo MCP (desligada por padrão).")
    def set_mcp_expose_source(self, info: strawberry.Info, expose: bool) -> McpStatus:
        ctx = _guard(info)
        ctx.state.mcp_expose_source = bool(expose)
        return _status(ctx)
