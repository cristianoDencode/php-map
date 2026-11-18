/**
 * Publicar → «Este computador»: o `/mcp` da própria interface exposto por
 * um túnel configurável (ngrok free é o exemplo de referência; cloudflared, o segundo).
 *
 * Nada de segredo passa por aqui: a configuração guarda só NOMES de variáveis, o log chega
 * mascarado, e o token do cliente nunca é mostrado (o trecho de conexão usa `<seu-token>`).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Circle, CircleCheck, CircleX, Copy, LoaderCircle, Play, Square } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { Badge, Button, Card, CardHeader, LoadingRows, Notice } from "../components/ui";
import { claudeCommand, mcpJson, useMcpStatus } from "../lib/access";
import { request } from "../lib/api";
import {
  isTunnelActive,
  joinCommand,
  SaveTunnelConfigDocument,
  splitCommand,
  splitNames,
  StartTunnelDocument,
  STATE_LABEL,
  StopTunnelDocument,
  TUNNEL_STAGES,
  useTunnelConfig,
  useTunnelStatus,
} from "../lib/tunnel";
import { cn } from "../lib/utils";

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

// --------------------------------------------------------------------------- configuração

type ConfigData = NonNullable<ReturnType<typeof useTunnelConfig>["data"]>;

function ConfigCard({ locked }: { locked: boolean }) {
  const config = useTunnelConfig();
  return (
    <Card aria-label="Configuração do túnel">
      <CardHeader
        title="Túnel"
        description="O comando que o mapeador roda para levar a porta desta interface até um endereço https público. {port} e {host} são trocados."
      />
      {config.isPending || !config.data ? (
        <div className="px-5 pb-5">
          <LoadingRows rows={3} label="Carregando a configuração" />
        </div>
      ) : (
        <ConfigForm data={config.data} locked={locked} />
      )}
    </Card>
  );
}

function ConfigForm({ data, locked }: { data: ConfigData; locked: boolean }) {
  const client = useQueryClient();
  const ids = { command: useId(), url: useId(), names: useId(), regex: useId() };
  const [command, setCommand] = useState(() => joinCommand(data.command));
  const [publicUrl, setPublicUrl] = useState(data.publicUrl ?? "");
  const [names, setNames] = useState(() => data.env.map((e) => e.name).join(", "));
  const [regex, setRegex] = useState(data.urlRegex);
  const save = useMutation({
    mutationFn: async () =>
      request(SaveTunnelConfigDocument, {
        command: splitCommand(command),
        publicUrl: publicUrl.trim() || null,
        urlRegex: regex.trim() || null,
        envNames: splitNames(names),
      }),
    onSuccess: () => {
      toast.success("Configuração do túnel salva");
      void client.invalidateQueries({ queryKey: ["tunnelConfig"] });
      void client.invalidateQueries({ queryKey: ["tunnelStatus"] });
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate();
  }
  return (
    <form onSubmit={submit} className="space-y-3 px-5 pb-5">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-muted-foreground">Usar exemplo:</span>
        {data.examples.map((e) => (
          <Button
            key={e.id}
            size="sm"
            variant="outline"
            disabled={locked}
            onClick={() => {
              setCommand(joinCommand(e.command));
              setPublicUrl(e.publicUrl ?? "");
              setNames(e.envNames.join(", "));
              setRegex(e.urlRegex);
            }}
          >
            {e.id === "ngrok" ? "ngrok" : "cloudflared"}
          </Button>
        ))}
      </div>
      <div className="space-y-1">
        <label htmlFor={ids.command} className="text-sm font-medium">
          Comando
        </label>
        <input
          id={ids.command}
          className={cn(INPUT, "font-mono")}
          value={command}
          onChange={(e) => setCommand(e.target.value)}
          placeholder="ngrok http {port} --url=https://SEU-DOMINIO.ngrok-free.app"
          disabled={locked}
        />
        <p className="text-xs text-muted-foreground">
          Roda sem shell. Não use <code>--host-header=rewrite</code>: o mapeador precisa do <code>Host</code> público.
        </p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <label htmlFor={ids.url} className="text-sm font-medium">
            URL pública fixa (opcional)
          </label>
          <input
            id={ids.url}
            className={INPUT}
            value={publicUrl}
            onChange={(e) => setPublicUrl(e.target.value)}
            placeholder="https://SEU-DOMINIO.ngrok-free.app"
            disabled={locked}
          />
        </div>
        <div className="space-y-1">
          <label htmlFor={ids.names} className="text-sm font-medium">
            Variáveis de ambiente (só os nomes)
          </label>
          <input
            id={ids.names}
            className={INPUT}
            value={names}
            onChange={(e) => setNames(e.target.value)}
            placeholder="NGROK_AUTHTOKEN"
            disabled={locked}
          />
          <p className="text-xs text-muted-foreground">
            O valor vem do ambiente ou do <code>.env</code>; nunca é gravado nem mostrado.
            {data.env.length > 0 && (
              <span className="ml-1 inline-flex flex-wrap gap-1">
                {data.env.map((v) => (
                  <Badge key={v.name} className={v.defined ? "border-primary/40 text-primary" : "border-warning text-warning"}>
                    {v.name}: {v.defined ? "definida" : "não definida"}
                  </Badge>
                ))}
              </span>
            )}
          </p>
        </div>
      </div>
      <div className="space-y-1">
        <label htmlFor={ids.regex} className="text-sm font-medium">
          Expressão regular da URL na saída do comando
        </label>
        <input id={ids.regex} className={cn(INPUT, "font-mono")} value={regex} onChange={(e) => setRegex(e.target.value)} disabled={locked} />
      </div>
      {save.error && <Notice tone="warning">{save.error.message}</Notice>}
      <Button type="submit" disabled={locked || save.isPending || !command.trim()}>
        {save.isPending ? "Salvando…" : "Salvar configuração"}
      </Button>
    </form>
  );
}

// --------------------------------------------------------------------------- controle

function stageIcon(kind: "done" | "now" | "failed" | "todo") {
  if (kind === "done") return <CircleCheck className="h-4 w-4 text-success" aria-hidden="true" />;
  if (kind === "now") return <LoaderCircle className="h-4 w-4 animate-spin text-primary" aria-hidden="true" />;
  if (kind === "failed") return <CircleX className="h-4 w-4 text-warning" aria-hidden="true" />;
  return <Circle className="h-4 w-4 text-muted-foreground" aria-hidden="true" />;
}

function ControlCard() {
  const client = useQueryClient();
  const status = useTunnelStatus();
  const data = status.data;
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["tunnelStatus"] });
    void client.invalidateQueries({ queryKey: ["mcpStatus"] });
  };
  const start = useMutation({ mutationFn: async () => request(StartTunnelDocument), onSuccess: refresh });
  const stop = useMutation({ mutationFn: async () => request(StopTunnelDocument), onSuccess: refresh });
  if (status.isPending || !data) {
    return (
      <Card aria-label="Expor o MCP">
        <div className="px-5 py-4">
          <LoadingRows rows={2} label="Carregando o estado do túnel" />
        </div>
      </Card>
    );
  }
  const active = isTunnelActive(data.state);
  const order = TUNNEL_STAGES.map((s) => s.id);
  const current = data.stage ? order.indexOf(data.stage) : data.state === "no-ar" ? order.length : -1;
  return (
    <Card aria-label="Expor o MCP">
      <CardHeader
        title="Expor o MCP"
        description="Sobe o túnel só depois de conferir token ativo e varredura de segredos, e testa a URL pública (401 sem token em /mcp, 404 no resto)."
        action={
          <span className="flex items-center gap-2">
            <Badge className={data.state === "no-ar" ? "border-primary/40 text-primary" : data.state === "falhou" || data.state === "caiu" ? "border-warning text-warning" : ""}>
              {STATE_LABEL[data.state] ?? data.state}
            </Badge>
            {active ? (
              <Button size="sm" variant="outline" onClick={() => stop.mutate()} disabled={stop.isPending}>
                <Square className="h-4 w-4" aria-hidden="true" /> Parar
              </Button>
            ) : (
              <Button size="sm" onClick={() => start.mutate()} disabled={!data.canStart || start.isPending}>
                <Play className="h-4 w-4" aria-hidden="true" /> {data.state === "caiu" || data.state === "falhou" ? "Expor de novo" : "Expor"}
              </Button>
            )}
          </span>
        }
      />
      <div className="space-y-3 px-5 pb-5">
        {!active && data.blockedReason && <p className="text-sm text-muted-foreground">{data.blockedReason}</p>}
        {start.error && <Notice tone="warning">{start.error.message}</Notice>}
        {data.state !== "parado" && (
          <ol className="grid gap-1 text-sm sm:grid-cols-5" aria-label="Etapas do túnel">
            {TUNNEL_STAGES.map((s, i) => {
              const failedHere = data.state === "falhou" && data.stage === s.id;
              const kind = failedHere ? "failed" : i < current ? "done" : i === current && active ? "now" : "todo";
              return (
                <li key={s.id} className="flex items-center gap-1.5">
                  {stageIcon(kind)} {s.label}
                </li>
              );
            })}
          </ol>
        )}
        {data.error && <Notice tone="warning">{data.error}</Notice>}
        {data.findings.length > 0 && (
          <ul className="space-y-1 rounded-md border border-warning bg-warning-bg p-3 text-xs" aria-label="Achados de segredo">
            {data.findings.map((f) => (
              <li key={f.hash}>
                <span className="font-medium">{f.kind}</span> · {f.where} · <code>{f.masked}</code>
              </li>
            ))}
          </ul>
        )}
        {data.state === "no-ar" && data.url && (
          <div className="flex items-center gap-2 text-sm">
            <span className="text-muted-foreground">URL pública do MCP:</span>
            <code className="break-all font-medium">{data.url}</code>
            <Button size="sm" variant="outline" onClick={() => void copy(data.url ?? "", "URL")} aria-label="Copiar a URL pública">
              <Copy className="h-3.5 w-3.5" aria-hidden="true" />
            </Button>
          </div>
        )}
        {data.log.length > 0 && (
          <details>
            <summary className="cursor-pointer text-sm text-muted-foreground">Log do túnel (mascarado)</summary>
            <pre className="mt-2 max-h-48 overflow-auto rounded-md border bg-muted p-2 text-xs whitespace-pre-wrap">{data.log.join("\n")}</pre>
          </details>
        )}
      </div>
    </Card>
  );
}

// --------------------------------------------------------------------------- como conectar

function ConnectCard() {
  const mcp = useMcpStatus();
  const tunnel = useTunnelStatus();
  const url = tunnel.data?.state === "no-ar" && tunnel.data.url ? tunnel.data.url : (mcp.data?.publicUrls[0] ?? "https://SEU-DOMINIO/mcp");
  return (
    <Card aria-label="Como conectar">
      <CardHeader
        title="Como conectar"
        description={
          <>
            Cada cliente usa um token da página{" "}
            <Link to="/acesso" className="text-primary underline-offset-4 hover:underline">
              Acesso
            </Link>
            . Troque <code>&lt;seu-token&gt;</code> pelo valor mostrado lá.
          </>
        }
      />
      <div className="space-y-3 px-5 pb-5">
        <Snippet label="Claude Code" text={claudeCommand(url, "<seu-token>")} />
        <Snippet label=".mcp.json (Cursor, VS Code)" text={mcpJson(url)} />
        <Notice>
          <strong>ngrok grátis:</strong> use o domínio fixo da sua conta (<code>--url=https://…ngrok-free.app</code>); sem ele a
          URL muda a cada «Expor» e a configuração de cada cliente muda junto. Há limite mensal de tráfego. A página de aviso
          do ngrok aparece só para navegadores e não afeta os clientes MCP.
        </Notice>
      </div>
    </Card>
  );
}

export function ThisComputerPanel() {
  const status = useTunnelStatus();
  const locked = isTunnelActive(status.data?.state);
  return (
    <div className="space-y-6">
      <ControlCard />
      <ConfigCard locked={locked} />
      <ConnectCard />
    </div>
  );
}
