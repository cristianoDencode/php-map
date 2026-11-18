/**
 * Acesso (`/acesso`): o MCP em `/mcp`, os tokens e os usuários do login.
 *
 * O token aparece uma única vez, no diálogo de criação. Revogar pede confirmação. O código-fonte
 * pelo MCP vem desligado: o analista liga de propósito, com o aviso.
 */
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Plus, X } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { toast } from "sonner";
import { Badge, Button, Card, CardHeader, LoadingRows, Notice } from "../components/ui";
import {
  AccessTokensDocument,
  AccessUsersDocument,
  AddAccessUserDocument,
  claudeCommand,
  CreateAccessTokenDocument,
  DisableAccessUserDocument,
  EnableAccessUserDocument,
  EXPIRES_CHOICES,
  mcpJson,
  ResetAccessUserPasswordDocument,
  ROLE_CHOICES,
  ROLE_LABEL,
  RevokeAccessTokenDocument,
  SetAccessUserRoleDocument,
  SetMcpExposeSourceDocument,
  useMcpStatus,
} from "../lib/access";
import { request } from "../lib/api";
import { cn, formatDateTime } from "../lib/utils";

const INPUT = "h-9 w-full rounded-md border border-input bg-card px-3 text-sm";

async function copy(text: string, what: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(`${what} copiado`);
  } catch {
    toast.error("Não consegui copiar: selecione o texto e copie à mão");
  }
}

function Snippet({ label, text }: { label: string; text: string }) {
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">{label}</span>
        <Button size="sm" variant="outline" onClick={() => void copy(text, label)} aria-label={`Copiar ${label}`}>
          <Copy className="h-3.5 w-3.5" aria-hidden="true" /> Copiar
        </Button>
      </div>
      <pre className="overflow-x-auto rounded-md border bg-muted p-2 text-xs whitespace-pre-wrap break-all">{text}</pre>
    </div>
  );
}

// --------------------------------------------------------------------------- MCP

function McpCard() {
  const status = useMcpStatus();
  const client = useQueryClient();
  const toggle = useMutation({
    mutationFn: async (expose: boolean) => request(SetMcpExposeSourceDocument, { expose }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["mcpStatus"] }),
  });
  const data = status.data;
  return (
    <Card aria-label="MCP">
      <CardHeader
        title="MCP"
        description="As IAs consultam o mapa por aqui. O endereço usa um token da lista abaixo (cabeçalho Authorization: Bearer)."
      />
      <div className="space-y-3 px-5 pb-5">
        {status.isPending || !data ? (
          <LoadingRows rows={2} label="Carregando o estado do MCP" />
        ) : (
          <>
            {!data.available && <Notice tone="warning">{data.reason ?? "O MCP não está disponível."}</Notice>}
            <dl className="grid gap-2 text-sm sm:grid-cols-[10rem_1fr]">
              <dt className="text-muted-foreground">Estado</dt>
              <dd>
                <Badge className={data.available ? "border-primary/40 text-primary" : ""}>
                  {data.available ? "no ar" : "indisponível"}
                </Badge>{" "}
                <span className="text-muted-foreground">
                  {data.activeTokens} token(s) ativo(s){data.legacyToken ? " + o token legado do ambiente" : ""}
                </span>
              </dd>
              <dt className="text-muted-foreground">Endereço local</dt>
              <dd className="flex items-center gap-2">
                <code className="break-all">{data.localUrl}</code>
                <Button size="sm" variant="outline" onClick={() => void copy(data.localUrl, "Endereço")} aria-label="Copiar o endereço local">
                  <Copy className="h-3.5 w-3.5" aria-hidden="true" />
                </Button>
              </dd>
              <dt className="text-muted-foreground">Endereços públicos</dt>
              <dd>
                {data.publicUrls.length === 0 ? (
                  <span className="text-muted-foreground">nenhum (o túnel da publicação preenche aqui)</span>
                ) : (
                  <ul className="space-y-1">
                    {data.publicUrls.map((url) => (
                      <li key={url}>
                        <code className="break-all">{url}</code>
                      </li>
                    ))}
                  </ul>
                )}
              </dd>
            </dl>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                className="mt-1"
                checked={data.exposeSource}
                disabled={toggle.isPending}
                onChange={(e) => toggle.mutate(e.target.checked)}
              />
              <span>
                Permitir ler o código-fonte pelo MCP
                <span className="block text-xs text-muted-foreground">
                  Desligado por padrão: o código do e-cidade tem arquivos com credenciais (libs/db_conn.php, .env). Ligue só
                  se confia em todo cliente com token. Volta a desligado quando o servidor reinicia.
                </span>
              </span>
            </label>
          </>
        )}
      </div>
    </Card>
  );
}

// --------------------------------------------------------------------------- tokens

function NewTokenDialog({ url, open, onOpenChange }: { url: string; open: boolean; onOpenChange: (open: boolean) => void }) {
  const client = useQueryClient();
  const ids = { name: useId(), days: useId() };
  const [name, setName] = useState("");
  const [days, setDays] = useState("90");
  const [created, setCreated] = useState<string | null>(null);
  const make = useMutation({
    mutationFn: async () =>
      (await request(CreateAccessTokenDocument, { name: name.trim(), expiresDays: days === "never" ? null : Number(days) }))
        .createAccessToken,
    onSuccess: (data) => {
      setCreated(data.token);
      void client.invalidateQueries({ queryKey: ["accessTokens"] });
      void client.invalidateQueries({ queryKey: ["mcpStatus"] });
    },
  });

  function close(value: boolean) {
    onOpenChange(value);
    if (!value) {
      setCreated(null); // o valor do token sai da memória da página
      setName("");
      setDays("90");
      make.reset();
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    make.mutate();
  }

  return (
    <DialogPrimitive.Root open={open} onOpenChange={close}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <DialogPrimitive.Content className="fixed left-1/2 top-1/2 z-50 max-h-[90vh] w-full max-w-xl -translate-x-1/2 -translate-y-1/2 space-y-4 overflow-y-auto rounded-xl border bg-card p-6 shadow-xl focus:outline-none">
          <div className="flex items-start justify-between gap-4">
            <DialogPrimitive.Title className="text-lg font-semibold">
              {created ? "Token criado" : "Novo token"}
            </DialogPrimitive.Title>
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="icon" aria-label="Fechar">
                <X className="h-4 w-4" aria-hidden="true" />
              </Button>
            </DialogPrimitive.Close>
          </div>
          <DialogPrimitive.Description className="text-sm text-muted-foreground">
            {created
              ? "Guarde o token agora: ele não aparece de novo. O cofre guarda só o hash."
              : "Um token por cliente (ex.: meu-notebook), para revogar um sem derrubar os outros."}
          </DialogPrimitive.Description>
          {created ? (
            <div className="space-y-3">
              <Snippet label="Token" text={created} />
              <Snippet label="Comando do Claude Code" text={claudeCommand(url, created)} />
              <Snippet label=".mcp.json" text={mcpJson(url)} />
              <Button className="w-full" onClick={() => close(false)}>
                Já guardei, fechar
              </Button>
            </div>
          ) : (
            <form onSubmit={submit} className="space-y-3">
              <div className="space-y-1">
                <label htmlFor={ids.name} className="text-sm font-medium">
                  Nome do token
                </label>
                <input id={ids.name} className={INPUT} value={name} onChange={(e) => setName(e.target.value)} required />
              </div>
              <div className="space-y-1">
                <label htmlFor={ids.days} className="text-sm font-medium">
                  Validade
                </label>
                <select id={ids.days} className={INPUT} value={days} onChange={(e) => setDays(e.target.value)}>
                  {EXPIRES_CHOICES.map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </div>
              {days === "never" && <Notice tone="warning">Token sem validade: revogue-o quando não precisar mais.</Notice>}
              {make.error && <Notice tone="warning">{make.error.message}</Notice>}
              <Button type="submit" className="w-full" disabled={make.isPending || !name.trim()}>
                {make.isPending ? "Criando…" : "Criar token"}
              </Button>
            </form>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

function TokensCard({ url }: { url: string }) {
  const client = useQueryClient();
  const tokens = useQuery({
    queryKey: ["accessTokens"],
    queryFn: async () => (await request(AccessTokensDocument)).accessTokens,
    meta: { silent: true },
  });
  const [creating, setCreating] = useState(false);
  const [confirming, setConfirming] = useState<number | null>(null);
  const revoke = useMutation({
    mutationFn: async (id: number) => request(RevokeAccessTokenDocument, { id }),
    onSuccess: () => {
      setConfirming(null);
      toast.success("Token revogado");
      void client.invalidateQueries({ queryKey: ["accessTokens"] });
      void client.invalidateQueries({ queryKey: ["mcpStatus"] });
    },
  });
  return (
    <Card aria-label="Tokens">
      <CardHeader
        title="Tokens"
        description="Cada cliente MCP usa um token. Vencido ou revogado, ele recebe 401 na próxima chamada."
        action={
          <Button size="sm" onClick={() => setCreating(true)}>
            <Plus className="h-4 w-4" aria-hidden="true" /> Novo token
          </Button>
        }
      />
      <div className="overflow-x-auto px-5 pb-5">
        {tokens.error && <Notice tone="warning">{tokens.error.message}</Notice>}
        {tokens.isPending ? (
          <LoadingRows rows={2} label="Carregando os tokens" />
        ) : (tokens.data?.length ?? 0) === 0 ? (
          <p className="text-sm text-muted-foreground">Nenhum token ainda.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th className="py-2 pr-3 font-medium">Nome</th>
                <th className="pr-3 font-medium">Prefixo</th>
                <th className="pr-3 font-medium">Criado</th>
                <th className="pr-3 font-medium">Vence</th>
                <th className="pr-3 font-medium">Último uso</th>
                <th className="pr-3 font-medium">Estado</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {tokens.data?.map((t) => (
                <tr key={t.id} className="border-b last:border-0">
                  <td className="py-2 pr-3 font-medium">{t.name}</td>
                  <td className="pr-3 font-mono text-xs">{t.prefix}…</td>
                  <td className="pr-3">{formatDateTime(t.createdAt)}</td>
                  <td className="pr-3">{t.expiresAt ? formatDateTime(t.expiresAt) : "nunca"}</td>
                  <td className="pr-3">{t.lastUsedAt ? `${formatDateTime(t.lastUsedAt)} (${t.lastUsedIp ?? "?"})` : "—"}</td>
                  <td className="pr-3">
                    <Badge className={cn(t.state === "ativo" ? "border-primary/40 text-primary" : "text-muted-foreground")}>
                      {t.state}
                    </Badge>
                  </td>
                  <td className="text-right">
                    {t.state === "revogado" ? null : confirming === t.id ? (
                      <span className="inline-flex gap-1">
                        <Button size="sm" onClick={() => revoke.mutate(t.id)} disabled={revoke.isPending} aria-label={`Confirmar a revogação de ${t.name}`}>
                          Confirmar revogação
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setConfirming(null)}>
                          Cancelar
                        </Button>
                      </span>
                    ) : (
                      <Button size="sm" variant="outline" onClick={() => setConfirming(t.id)} aria-label={`Revogar ${t.name}`}>
                        Revogar
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <NewTokenDialog url={url} open={creating} onOpenChange={setCreating} />
    </Card>
  );
}

// --------------------------------------------------------------------------- usuários

function UsersCard() {
  const client = useQueryClient();
  const ids = { name: useId(), pass: useId(), role: useId() };
  const users = useQuery({
    queryKey: ["accessUsers"],
    queryFn: async () => (await request(AccessUsersDocument)).accessUsers,
    meta: { silent: true },
  });
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("leitor");
  const [resetting, setResetting] = useState<string | null>(null);
  const [fresh, setFresh] = useState("");
  const refresh = () => void client.invalidateQueries({ queryKey: ["accessUsers"] });
  const add = useMutation({
    mutationFn: async () => request(AddAccessUserDocument, { username: username.trim(), password, role }),
    onSuccess: () => {
      toast.success("Usuário criado");
      setUsername("");
      setPassword("");
      refresh();
    },
  });
  const toggle = useMutation({
    mutationFn: async (v: { username: string; enable: boolean }) =>
      v.enable ? request(EnableAccessUserDocument, { username: v.username }) : request(DisableAccessUserDocument, { username: v.username }),
    onSuccess: refresh,
  });
  const changeRole = useMutation({
    mutationFn: async (v: { username: string; role: string }) => request(SetAccessUserRoleDocument, v),
    onSuccess: () => {
      toast.success("Papel alterado");
      refresh();
    },
  });
  const reset = useMutation({
    mutationFn: async (name: string) => request(ResetAccessUserPasswordDocument, { username: name, password: fresh }),
    onSuccess: () => {
      toast.success("Senha redefinida");
      setResetting(null);
      setFresh("");
    },
  });
  const activeAdmins = users.data?.filter((u) => u.active && u.role === "admin").length ?? 0;
  return (
    <Card aria-label="Usuários">
      <CardHeader
        title="Usuários"
        description="Quem entra na interface. O administrador muda tudo; o leitor só consulta o mapa. Desativar não apaga: derruba as sessões e pode ser desfeito."
      />
      <div className="space-y-4 px-5 pb-5">
        {users.error && <Notice tone="warning">{users.error.message}</Notice>}
        {users.isPending ? (
          <LoadingRows rows={2} label="Carregando os usuários" />
        ) : (
          <ul className="divide-y text-sm">
            {users.data?.map((u) => {
              const lastAdmin = u.active && u.role === "admin" && activeAdmins <= 1;
              const blocked = u.isCurrent
                ? "Você não pode desativar o próprio usuário"
                : lastAdmin
                  ? "Último administrador ativo"
                  : undefined;
              return (
                <li key={u.username} className="flex flex-wrap items-center gap-2 py-2">
                  <span className="font-medium">{u.username}</span>
                  {u.isCurrent && <Badge>você</Badge>}
                  <select
                    className="h-8 rounded-md border border-input bg-card px-2 text-xs"
                    aria-label={`Papel de ${u.username}`}
                    value={u.role}
                    disabled={changeRole.isPending || (lastAdmin && u.role === "admin")}
                    title={lastAdmin ? "Último administrador ativo" : ROLE_LABEL[u.role]}
                    onChange={(e) => changeRole.mutate({ username: u.username, role: e.target.value })}
                  >
                    {ROLE_CHOICES.map((choice) => (
                      <option key={choice.value} value={choice.value}>
                        {ROLE_LABEL[choice.value]}
                      </option>
                    ))}
                  </select>
                  <Badge className={u.active ? "border-primary/40 text-primary" : "text-muted-foreground"}>
                    {u.active ? "ativo" : "desativado"}
                  </Badge>
                  {u.locked && <Badge className="text-warning">bloqueado</Badge>}
                  <span className="flex-1 text-xs text-muted-foreground">
                    último login: {u.lastLoginAt ? formatDateTime(u.lastLoginAt) : "nunca"}
                  </span>
                  {resetting === u.username ? (
                    <span className="flex items-center gap-1">
                      <input
                        type="password"
                        className={cn(INPUT, "w-44")}
                        aria-label={`Nova senha de ${u.username}`}
                        value={fresh}
                        onChange={(e) => setFresh(e.target.value)}
                        autoComplete="new-password"
                      />
                      <Button size="sm" onClick={() => reset.mutate(u.username)} disabled={reset.isPending || fresh.length === 0}>
                        Salvar senha
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setResetting(null)}>
                        Cancelar
                      </Button>
                    </span>
                  ) : (
                    <Button size="sm" variant="outline" onClick={() => { setResetting(u.username); setFresh(""); }} aria-label={`Redefinir a senha de ${u.username}`}>
                      Redefinir senha
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={toggle.isPending || (u.active && blocked !== undefined)}
                    title={u.active ? blocked : undefined}
                    aria-label={`${u.active ? "Desativar" : "Reativar"} ${u.username}`}
                    onClick={() => toggle.mutate({ username: u.username, enable: !u.active })}
                  >
                    {u.active ? "Desativar" : "Reativar"}
                  </Button>
                </li>
              );
            })}
          </ul>
        )}
        {toggle.error && <Notice tone="warning">{toggle.error.message}</Notice>}
        {changeRole.error && <Notice tone="warning">{changeRole.error.message}</Notice>}
        {reset.error && <Notice tone="warning">{reset.error.message}</Notice>}
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            add.mutate();
          }}
        >
          <div className="space-y-1">
            <label htmlFor={ids.name} className="text-xs font-medium">
              E-mail do novo usuário
            </label>
            <input
              id={ids.name}
              type="email"
              className={cn(INPUT, "w-56")}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="off"
              placeholder="pessoa@exemplo.com"
              spellCheck={false}
            />
          </div>
          <div className="space-y-1">
            <label htmlFor={ids.pass} className="text-xs font-medium">
              Senha (mín. 10)
            </label>
            <input
              id={ids.pass}
              type="password"
              className={cn(INPUT, "w-44")}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
            />
          </div>
          <div className="space-y-1">
            <label htmlFor={ids.role} className="text-xs font-medium">
              Papel
            </label>
            <select id={ids.role} className={cn(INPUT, "w-48")} value={role} onChange={(e) => setRole(e.target.value)}>
              {ROLE_CHOICES.map((choice) => (
                <option key={choice.value} value={choice.value}>
                  {choice.label}
                </option>
              ))}
            </select>
          </div>
          <Button type="submit" size="sm" disabled={add.isPending || !username.trim() || !password}>
            Adicionar
          </Button>
        </form>
        {add.error && <Notice tone="warning">{add.error.message}</Notice>}
      </div>
    </Card>
  );
}

// --------------------------------------------------------------------------- página

export function AccessPage() {
  const status = useMcpStatus();
  const data = status.data;
  const url = data?.publicUrls[0] ?? data?.localUrl ?? "http://127.0.0.1:8765/mcp";
  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <header>
        <h1 className="text-2xl font-semibold">Acesso</h1>
        <p className="text-sm text-muted-foreground">
          O MCP sai por esta interface em <code>/mcp</code>, com tokens deste cofre. Pela URL pública só o MCP responde: a
          interface, o scan e as contas ficam só neste computador.
        </p>
      </header>
      {data && !data.vaultAvailable ? (
        <Notice tone="warning">O cofre de acesso não está ligado nesta interface: tokens e usuários ficam indisponíveis.</Notice>
      ) : (
        <>
          <McpCard />
          <TokensCard url={url} />
          <UsersCard />
        </>
      )}
    </div>
  );
}
