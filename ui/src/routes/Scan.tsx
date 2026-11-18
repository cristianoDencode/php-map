/**
 * "Atualizar mapeamento" (`/scan`): escolher a pasta do e-cidade, rodar o scan,
 * acompanhar as etapas, cancelar e ver o resumo. O grafo novo passa a ser servido sem
 * reiniciar o servidor.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  Circle,
  CircleCheck,
  CircleMinus,
  CircleX,
  FolderSearch,
  LoaderCircle,
  Plus,
  RefreshCw,
  Trash,
  X,
} from "lucide-react";
import { type FormEvent, type ReactNode, useId, useState } from "react";
import { Button, Card, CardHeader, LoadingRows, Notice } from "../components/ui";
import type { ScanDefaultsQuery } from "../graphql/generated/graphql";
import { request } from "../lib/api";
import { SuggestProfileDocument } from "../lib/projects";
import {
  AstCacheDocument,
  CancelScanDocument,
  formatDuration,
  formatElapsed,
  isRunning,
  resultSummary,
  routinesLine,
  ScanDefaultsDocument,
  type ScanJob,
  SetSourceDocument,
  type StageStatus,
  stageStatuses,
  StartScanDocument,
  useScanJob,
} from "../lib/scan";
import { cn, formatNumber, plural } from "../lib/utils";

type Defaults = ScanDefaultsQuery["scanDefaults"];

export function ScanPage() {
  const job = useScanJob();
  const defaults = useQuery({
    queryKey: ["scanDefaults"],
    queryFn: async () => (await request(ScanDefaultsDocument)).scanDefaults,
  });
  const running = isRunning(job.data);
  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <header>
        <h1 className="text-2xl font-semibold">Atualizar mapeamento</h1>
        <p className="text-sm text-muted-foreground">
          Roda o scan {defaults.data?.projectName ? `do projeto ${defaults.data.projectName}` : "do e-cidade"} numa
          pasta deste computador. No fim, a interface passa a mostrar o grafo novo, sem reiniciar o servidor.
        </p>
      </header>
      {job.data ? <JobPanel job={job.data} profile={defaults.data?.profile ?? null} /> : null}
      {running ? null : defaults.data ? (
        <ScanForm defaults={defaults.data} />
      ) : (
        <LoadingRows rows={6} label="Carregando as opções do scan" />
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- formulário

function Field({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <div className="text-sm font-medium">{label}</div>
      {children}
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

function CheckOption({
  checked,
  onChange,
  disabled,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
  label: string;
  hint?: ReactNode;
}) {
  const id = useId();
  return (
    <div className="flex items-start gap-2">
      <input
        id={id}
        type="checkbox"
        className="mt-0.5 h-4 w-4 accent-primary"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <div>
        <label htmlFor={id} className={cn("text-sm", disabled && "text-muted-foreground")}>
          {label}
        </label>
        {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
      </div>
    </div>
  );
}

/** Uma opção de escolha única (fonte do schema), no padrão do `CheckOption`. */
function RadioOption({
  name,
  checked,
  onChange,
  disabled,
  label,
  hint,
}: {
  name: string;
  checked: boolean;
  onChange: () => void;
  disabled?: boolean;
  label: string;
  hint?: ReactNode;
}) {
  const id = useId();
  return (
    <div className="flex items-start gap-2">
      <input
        id={id}
        type="radio"
        name={name}
        className="mt-0.5 h-4 w-4 accent-primary"
        checked={checked}
        disabled={disabled}
        onChange={onChange}
      />
      <div>
        <label htmlFor={id} className={cn("text-sm", disabled && "text-muted-foreground")}>
          {label}
        </label>
        {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
      </div>
    </div>
  );
}

/** Fonte do schema do scan: banco (`--schema-db`), arquivos (`--schema`) ou nenhuma. */
export type SchemaSource = "db" | "files" | "none";

function asSchemaSource(value: string, dbAvailable: boolean): SchemaSource {
  if (value === "db") return dbAvailable ? "db" : "none";
  return value === "files" ? "files" : "none";
}

const DEFAULT_DSN_ENV = "PHP_MAP_CATALOG_DSN";

/** `defaults.dsnEnv` é o NOME da variável do projeto (o valor nunca chega à interface). */
function dsnMissing(env: string) {
  return (
    <>
      Indisponível: defina <code className="font-mono">{env}</code> no <code className="font-mono">.env</code> da raiz
      do projeto.
    </>
  );
}

const INPUT = "h-9 w-full rounded-md border border-input bg-card px-3 font-mono text-sm";

type SourceCheck = { ok: boolean; message: string } | null;

function ScanForm({ defaults }: { defaults: Defaults }) {
  const client = useQueryClient();
  const dsnEnv = defaults.dsnEnv ?? DEFAULT_DSN_ENV;
  const isEcidade = !defaults.profile || defaults.profile === "ecidade";
  const marker = defaults.marker ?? "libs/db_stdlib.php";
  const [source, setSource] = useState(defaults.source ?? "");
  const [schema, setSchema] = useState<string[]>(defaults.schema.length ? defaults.schema : [""]);
  const [schemaSource, setSchemaSource] = useState<SchemaSource>(
    asSchemaSource(defaults.schemaSource, defaults.schemaDbAvailable),
  );
  const schemaName = useId();
  const [catalog, setCatalog] = useState(defaults.catalogFromEnv && defaults.catalogAvailable);
  const [reuseAst, setReuseAst] = useState(defaults.reuseAst);
  const [search, setSearch] = useState(true);
  const [analytics, setAnalytics] = useState(true);
  const [check, setCheck] = useState<SourceCheck>(null);
  // Pasta cuja extração o aviso descreve: a inicial, a validada ou a recente escolhida.
  const [cacheSource, setCacheSource] = useState(defaults.source ?? "");
  const sourceId = useId();

  const validate = useMutation({
    // E-cidade: `setSource` também passa a usar a pasta no painel de código. Outros perfis:
    // confere o marcador do perfil (a pasta do projeto só muda pela tela de projetos).
    mutationFn: async (path: string) => {
      if (isEcidade) return (await request(SetSourceDocument, { path })).setSource.source ?? path;
      const { suggestProfile: found } = await request(SuggestProfileDocument, { source: path });
      const ok = found.markers.some((m) => m.profile === defaults.profile && m.found);
      if (!found.exists) throw new Error("A pasta não existe neste computador.");
      if (!ok) throw new Error(`${path} não parece um projeto ${defaults.profile}: falta ${marker}`);
      return path;
    },
    meta: { silent: true }, // o motivo aparece embaixo do campo
    onSuccess: (resolved) => {
      const folder = resolved ?? source;
      setSource(folder);
      setCacheSource(folder);
      setCheck({
        ok: true,
        message: isEcidade
          ? "Parece um e-cidade. O painel “Código” passa a usar esta pasta."
          : `Parece um projeto ${defaults.profile} (achei ${marker}).`,
      });
      void client.invalidateQueries({ queryKey: ["workspace"] });
      void client.invalidateQueries({ queryKey: ["sourceFile"] });
      void client.invalidateQueries({ queryKey: ["scanDefaults"] });
    },
    onError: (error) => setCheck({ ok: false, message: error.message }),
  });

  const start = useMutation({
    mutationFn: () =>
      request(StartScanDocument, {
        input: {
          ...(defaults.project ? { project: defaults.project } : {}),
          source: source.trim(),
          schemaSource,
          // Só em "Arquivos" os caminhos vão para o scan (as fontes são excludentes).
          schema: schemaSource === "files" ? schema.map((s) => s.trim()).filter(Boolean) : [],
          catalogFromEnv: catalog,
          reuseAst,
          search,
          analytics,
        },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["scanJob"] });
      void client.invalidateQueries({ queryKey: ["workspace"] });
      void client.invalidateQueries({ queryKey: ["scanDefaults"] });
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    start.mutate();
  };
  const disabled = defaults.readOnly || start.isPending;

  return (
    <Card>
      <CardHeader title="Novo scan" description="As opções ficam salvas para a próxima vez." />
      <form onSubmit={submit} className="space-y-5 px-5 py-4" aria-label="Opções do scan">
        {defaults.readOnly ? (
          <Notice tone="warning">
            O servidor está em modo somente leitura (<code className="font-mono">--read-only</code>): não dá
            para trocar a pasta nem rodar o scan por aqui.
          </Notice>
        ) : null}

        <Field
          label={<label htmlFor={sourceId}>{isEcidade ? "Pasta do e-cidade" : "Pasta do projeto"}</label>}
          hint={`${isEcidade ? "A instalação real ou a cópia de teste. " : ""}Precisa ter ${marker}.`}
        >
          <div className="flex gap-2">
            <input
              id={sourceId}
              className={INPUT}
              value={source}
              placeholder="D:\ecidade-windows\www\html\e-cidade"
              onChange={(event) => {
                setSource(event.target.value);
                setCheck(null);
              }}
              spellCheck={false}
            />
            <Button
              variant="outline"
              disabled={defaults.readOnly || !source.trim() || validate.isPending}
              onClick={() => validate.mutate(source.trim())}
            >
              <FolderSearch className="h-4 w-4" aria-hidden="true" />
              Validar
            </Button>
          </div>
          {check ? (
            <p
              role="status"
              className={cn("flex items-center gap-1.5 text-sm", check.ok ? "text-success" : "text-warning")}
            >
              {check.ok ? (
                <Check className="h-4 w-4" aria-hidden="true" />
              ) : (
                <CircleX className="h-4 w-4" aria-hidden="true" />
              )}
              {check.message}
            </p>
          ) : null}
          {defaults.recentSources.length ? (
            <div className="flex flex-wrap items-center gap-1.5 text-xs">
              <span className="text-muted-foreground">Recentes:</span>
              {defaults.recentSources.map((recent) => (
                <button
                  key={recent}
                  type="button"
                  className="rounded-full border px-2 py-0.5 font-mono hover:bg-muted"
                  onClick={() => {
                    setSource(recent);
                    setCacheSource(recent);
                    setCheck(null);
                  }}
                >
                  {recent}
                </button>
              ))}
            </div>
          ) : null}
        </Field>

        <fieldset className="space-y-2">
          <legend className="text-sm font-medium">Fonte do schema</legend>
          <RadioOption
            name={schemaName}
            label="Banco (.env)"
            checked={schemaSource === "db"}
            onChange={() => setSchemaSource("db")}
            disabled={!defaults.schemaDbAvailable}
            hint={
              defaults.schemaDbAvailable
                ? "Tabelas, funções e triggers lidos do banco, só leitura; as tabelas ficam no schema real (empenho.empempenho) e o texto das funções fica gravado para consulta (o que cada fc_ faz). Recomendado."
                : dsnMissing(dsnEnv)
            }
          />
          <RadioOption
            name={schemaName}
            label="Arquivos"
            checked={schemaSource === "files"}
            onChange={() => setSchemaSource("files")}
            hint="Arquivo .sql, pg_dump ou pasta de incrementais (ex.: a pasta db do e-cidade). Sem o texto das funções."
          />
          <RadioOption
            name={schemaName}
            label="Nenhum"
            checked={schemaSource === "none"}
            onChange={() => setSchemaSource("none")}
            hint="As tabelas ficam como placeholder e as funções sem texto."
          />
        </fieldset>

        {schemaSource === "files" ? (
          <Field label="Caminhos do schema">
            <ul className="space-y-2">
              {schema.map((path, index) => (
                <li key={index} className="flex gap-2">
                  <input
                    aria-label={`Caminho de schema ${index + 1}`}
                    className={INPUT}
                    value={path}
                    spellCheck={false}
                    onChange={(event) => setSchema(schema.map((p, i) => (i === index ? event.target.value : p)))}
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Remover o caminho de schema ${index + 1}`}
                    onClick={() => setSchema(schema.length > 1 ? schema.filter((_, i) => i !== index) : [""])}
                  >
                    <Trash className="h-4 w-4" aria-hidden="true" />
                  </Button>
                </li>
              ))}
            </ul>
            <Button variant="link" size="sm" onClick={() => setSchema([...schema, ""])}>
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
              Adicionar caminho
            </Button>
          </Field>
        ) : null}

        <div className="space-y-3">
          {isEcidade ? (
          <CheckOption
            label="Ler o catálogo do banco (.env)"
            checked={catalog}
            onChange={setCatalog}
            disabled={!defaults.catalogAvailable}
            hint={
              defaults.catalogAvailable ? (
                "Dicionário de dados e menus lidos do banco, só leitura. A conexão vem do .env e não passa pela interface."
              ) : (
                <>
                  Indisponível: defina <code className="font-mono">{dsnEnv}</code> no{" "}
                  <code className="font-mono">.env</code> da raiz do projeto. Sem o catálogo, o grafo sai sem
                  módulos e sem menus.
                </>
              )
            }
          />
          ) : (
            <p className="text-xs text-muted-foreground">
              O perfil {defaults.profile} não lê catálogo: os módulos vêm da estrutura de pastas do projeto.
            </p>
          )}
          <CheckOption
            label="Reaproveitar a extração anterior (~3 min em vez de 6+)"
            checked={reuseAst}
            onChange={setReuseAst}
            hint="Só os arquivos novos ou alterados voltam ao extrator."
          />
          <AstCacheNotice source={cacheSource} reuseAst={reuseAst} dockerAvailable={defaults.dockerAvailable} />
          {defaults.dockerAvailable ? null : (
            <Notice tone="warning">
              Docker indisponível{defaults.dockerMessage ? ` (${defaults.dockerMessage})` : ""}. Com o cache
              completo o scan roda sem Docker; arquivos novos ou alterados precisam dele.
            </Notice>
          )}
          <details className="rounded-md border px-3 py-2">
            <summary className="cursor-pointer text-sm font-medium">Avançado</summary>
            <div className="mt-3 space-y-3">
              <CheckOption
                label="Construir o índice de busca"
                checked={search}
                onChange={setSearch}
                hint="Sem ele, a busca e o Ctrl+K não funcionam no grafo novo."
              />
              <CheckOption
                label="Calcular os agregados (módulos, acoplamento, pontos quentes)"
                checked={analytics}
                onChange={setAnalytics}
                hint="Sem eles, o início e a página de módulos ficam vazios."
              />
            </div>
          </details>
        </div>

        <div className="flex items-center gap-3 border-t pt-4">
          <Button type="submit" disabled={disabled || !source.trim()}>
            <RefreshCw className={cn("h-4 w-4", start.isPending && "animate-spin")} aria-hidden="true" />
            Rodar o scan
          </Button>
          <span className="text-xs text-muted-foreground">Um scan por vez. Dá para cancelar a qualquer momento.</span>
        </div>
      </form>
    </Card>
  );
}

/** Se a pasta já tem extração no `out` do servidor: evita a surpresa de 6+ min. */
function AstCacheNotice({
  source,
  reuseAst,
  dockerAvailable,
}: {
  source: string;
  reuseAst: boolean;
  dockerAvailable: boolean;
}) {
  const folder = source.trim();
  const cache = useQuery({
    queryKey: ["astCache", folder],
    queryFn: async () => (await request(AstCacheDocument, { source: folder })).astCache,
    enabled: folder !== "",
    meta: { silent: true },
  });
  const info = cache.data;
  if (!info) return null;
  const redo = !reuseAst && info.exists ? " Sem “Reaproveitar”, a extração desta pasta será refeita do zero." : "";
  if (info.reusable) {
    return (
      <p role="note" aria-label="Extração desta pasta" className="text-xs text-muted-foreground">
        {info.message}
        {redo}
      </p>
    );
  }
  return (
    <Notice tone="warning">
      <span aria-label="Extração desta pasta">
        {info.message}
        {redo}
        {dockerAvailable ? "" : " Sem o Docker disponível, esse scan vai falhar."}
      </span>
    </Notice>
  );
}

// --------------------------------------------------------------------------- andamento e resultado

const STATUS_ICON: Record<StageStatus, ReactNode> = {
  done: <CircleCheck className="h-4 w-4 text-success" aria-hidden="true" />,
  current: <LoaderCircle className="h-4 w-4 animate-spin text-primary" aria-hidden="true" />,
  stopped: <CircleX className="h-4 w-4 text-warning" aria-hidden="true" />,
  pending: <Circle className="h-4 w-4 text-muted-foreground" aria-hidden="true" />,
  skipped: <CircleMinus className="h-4 w-4 text-muted-foreground" aria-hidden="true" />,
};

const STATUS_TEXT: Record<StageStatus, string> = {
  done: "feita",
  current: "em andamento",
  stopped: "parou aqui",
  pending: "pendente",
  skipped: "não precisou",
};

function Stages({ job, profile }: { job: ScanJob; profile: string | null }) {
  return (
    <ol aria-label="Etapas do scan" className="space-y-1.5">
      {stageStatuses(job, profile).map(({ stage, label, status }) => (
        <li
          key={stage}
          data-status={status}
          aria-current={status === "current" ? "step" : undefined}
          className={cn(
            "flex items-center gap-2 text-sm",
            status === "current" && "font-medium",
            (status === "pending" || status === "skipped") && "text-muted-foreground",
          )}
        >
          {STATUS_ICON[status]}
          <span>{label}</span>
          <span className="sr-only">({STATUS_TEXT[status]})</span>
          {status === "skipped" ? <span className="text-xs">(não precisou)</span> : null}
        </li>
      ))}
    </ol>
  );
}

function Log({ lines, open = false }: { lines: string[]; open?: boolean }) {
  if (!lines.length) return null;
  return (
    <details className="rounded-md border" open={open}>
      <summary className="cursor-pointer px-3 py-2 text-sm font-medium">Log ({plural(lines.length, "linha")})</summary>
      <pre className="max-h-80 overflow-auto border-t bg-muted px-3 py-2 font-mono text-xs leading-5 whitespace-pre-wrap">
        {lines.join("\n")}
      </pre>
    </details>
  );
}

function CancelButton() {
  const client = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const cancel = useMutation({
    mutationFn: () => request(CancelScanDocument),
    onSettled: () => {
      setConfirming(false);
      void client.invalidateQueries({ queryKey: ["scanJob"] });
    },
  });
  if (!confirming) {
    return (
      <Button variant="outline" onClick={() => setConfirming(true)}>
        <X className="h-4 w-4" aria-hidden="true" />
        Cancelar
      </Button>
    );
  }
  return (
    <div role="alertdialog" aria-label="Confirmar o cancelamento" className="flex flex-wrap items-center gap-2">
      <span className="text-sm">Cancelar o scan? O grafo parcial é descartado.</span>
      <Button onClick={() => cancel.mutate()} disabled={cancel.isPending}>
        {cancel.isPending ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" /> : null}
        Sim, cancelar
      </Button>
      <Button variant="ghost" onClick={() => setConfirming(false)} disabled={cancel.isPending}>
        Continuar o scan
      </Button>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-lg border px-4 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-xl font-semibold tabular-nums">{value}</p>
    </div>
  );
}

function JobPanel({ job, profile }: { job: ScanJob; profile: string | null }) {
  if (job.state === "running") {
    return (
      <Card aria-label="Scan em andamento">
        <CardHeader
          title="Scan em andamento"
          description={`${job.stageLabel ?? "Preparando"} · ${formatElapsed(job.elapsedSeconds)}`}
          action={<CancelButton />}
        />
        <div className="space-y-4 px-5 py-4">
          <Stages job={job} profile={profile} />
          <Log lines={job.logTail} />
        </div>
      </Card>
    );
  }
  if (job.state === "succeeded") {
    const { summary, seconds } = resultSummary(job);
    const warnings = summary.warnings ?? [];
    return (
      <Card aria-label="Resultado do scan">
        <CardHeader
          title="Scan concluído"
          description={`Levou ${formatDuration(seconds ?? job.elapsedSeconds)}`}
        />
        <div className="space-y-4 px-5 py-4">
          <Notice>A interface já está usando o grafo novo.</Notice>
          {routinesLine(summary) ? <p className="text-sm text-muted-foreground">{routinesLine(summary)}</p> : null}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            <Metric label="Nós" value={formatNumber(summary.nodes)} />
            <Metric label="Arestas" value={formatNumber(summary.edges)} />
            <Metric label="Módulos" value={formatNumber(summary.modules)} />
            <Metric label="Telas no menu" value={formatNumber(summary.screens_in_menu)} />
            <Metric label="Tempo" value={formatDuration(seconds ?? job.elapsedSeconds)} />
          </div>
          {warnings.length ? (
            <Notice tone="warning">
              <p className="font-medium">Avisos do scan</p>
              <ul className="mt-1 list-disc pl-5">
                {warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </Notice>
          ) : null}
          <Log lines={job.logTail} />
        </div>
      </Card>
    );
  }
  const failed = job.state === "failed";
  return (
    <Card aria-label="Resultado do scan">
      <CardHeader
        title={failed ? "O scan falhou" : "Scan cancelado"}
        description={`Depois de ${formatDuration(job.elapsedSeconds)} · o grafo em uso não mudou`}
      />
      <div className="space-y-4 px-5 py-4">
        <Notice tone="warning">
          {failed ? (job.error ?? "O scan terminou com erro.") : "O scan foi cancelado e o grafo parcial foi descartado."}
        </Notice>
        <Stages job={job} profile={profile} />
        <Log lines={job.logTail} open={failed} />
      </div>
    </Card>
  );
}
