/**
 * Comunicação (`/comunicacao`): por onde o projeto ativo conversa.
 *
 * Endpoints SOAP e RPC e rotas HTTP numa lista só, com parâmetros, quem implementa, quem
 * chama e o outro lado da costura (o `cross`). A lista é paginada na API.
 * URL: `?sistema=&protocolo=&direcao=&modulo=&q=&pagina=&no=`.
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ArrowDownToLine, ArrowUpFromLine, Download, X } from "lucide-react";
import { useCallback, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { EvidenceLink } from "../app/code-panel";
import { NodeLink } from "../components/NodeBits";
import { Badge, Button, Card, CardHeader, LoadingRows, Notice } from "../components/ui";
import { request } from "../lib/api";
import {
  communicationCsv,
  type CommunicationPage as CommunicationData,
  CommunicationDocument,
  type CommunicationRow,
  csvName,
  DIRECTION_HELP,
  DIRECTION_LABELS,
  type EndpointDetail,
  EndpointDetailDocument,
  fetchAll,
  type Filters,
  listVariables,
  moduleName,
  PAGE_SIZE,
  paramsText,
  parsePage,
  protocolLabel,
  rangeText,
  STATUS_HELP,
  STATUS_LABELS,
} from "../lib/communication";
import { keyOf } from "../lib/ids";
import { activeOf, useProjects } from "../lib/projects";
import { ModuleNameMapDocument } from "../lib/subgraph";
import { cn, formatNumber } from "../lib/utils";

const SELECT = "h-9 max-w-56 rounded-md border border-input bg-card px-2 text-sm";

function download(text: string, name: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

// --------------------------------------------------------------------------- selos

export function DirectionBadge({ direction }: { direction: string }) {
  const Icon = direction === "entra" ? ArrowDownToLine : ArrowUpFromLine;
  return (
    <Badge title={DIRECTION_HELP[direction]} className="bg-card">
      <Icon className="h-3 w-3" aria-hidden="true" />
      {DIRECTION_LABELS[direction] ?? direction}
    </Badge>
  );
}

const STATUS_TONE: Record<string, string> = {
  casado: "border-success text-success",
  chamado_sem_servidor: "border-warning text-warning",
  servido_sem_cliente: "text-muted-foreground",
};

export function StatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <span className="text-muted-foreground">—</span>;
  return (
    <Badge title={STATUS_HELP[status]} className={cn("bg-card", STATUS_TONE[status])}>
      {STATUS_LABELS[status] ?? status}
    </Badge>
  );
}

// --------------------------------------------------------------------------- página

export function CommunicationPage() {
  const [params, setParams] = useSearchParams();
  const filters: Filters = {
    system: params.get("sistema") || null,
    protocol: params.get("protocolo") || null,
    direction: params.get("direcao") || null,
    module: params.get("modulo") || null,
    search: params.get("q") || null,
  };
  const page = parsePage(params.get("pagina"));
  const selected = params.get("no");

  const update = useCallback(
    (changes: Record<string, string | null>, replace = false) => {
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          for (const [key, value] of Object.entries(changes)) {
            if (value) next.set(key, value);
            else next.delete(key);
          }
          // Mudou o filtro: a lista volta para a primeira página.
          if (!("pagina" in changes) && !("no" in changes)) next.delete("pagina");
          return next;
        },
        { replace },
      );
    },
    [setParams],
  );

  const projects = useProjects();
  const active = activeOf(projects.data);
  const query = useQuery({
    queryKey: ["communication", filters, page],
    queryFn: () => request(CommunicationDocument, listVariables(filters, page)),
    placeholderData: keepPreviousData,
    meta: { silent: true },
  });
  const modules = useQuery({
    queryKey: ["moduleNameMap"],
    queryFn: () => request(ModuleNameMapDocument),
    meta: { silent: true },
    staleTime: Infinity,
  });
  const names = new Map((modules.data?.modules ?? []).map((m) => [m.module, m.name]));
  const data = query.data?.communication ?? null;
  const [exporting, setExporting] = useState(false);

  async function exportCsv() {
    setExporting(true);
    try {
      download(communicationCsv(await fetchAll(filters)), csvName(active?.slug ?? null));
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="space-y-5">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">Comunicação</h1>
        <p className="text-sm text-muted-foreground">
          Por onde {active ? <strong>{active.name}</strong> : "o projeto"} conversa: SOAP, RPC e rotas HTTP, com os
          parâmetros, quem implementa, quem chama e o outro lado da costura quando o outro projeto está mapeado.
        </p>
      </header>

      {data ? <Counters data={data} update={update} /> : null}

      <Filtros data={data} filters={filters} names={names} update={update} />

      {selected ? (
        <DetailCard key={selected} id={selected} names={names} onClose={() => update({ no: null }, true)} />
      ) : null}

      {query.isPending ? (
        <LoadingRows rows={8} label="Carregando a comunicação" />
      ) : query.isError ? (
        <Notice tone="warning">{query.error.message}</Notice>
      ) : data ? (
        <Card>
          <CardHeader
            title={
              <span aria-live="polite">
                {formatNumber(data.total)} {data.total === 1 ? "endpoint ou rota" : "endpoints e rotas"}
              </span>
            }
            description={
              data.crossNote
                ? `Sem o outro lado da costura: ${data.crossNote}.`
                : "Situação calculada pelo cruzamento com os outros projetos mapeados."
            }
            action={
              <Button variant="outline" size="sm" disabled={!data.total || exporting} onClick={() => void exportCsv()}>
                <Download className="h-4 w-4" aria-hidden="true" /> Exportar CSV
              </Button>
            }
          />
          {data.items.length === 0 ? (
            <p className="px-5 py-4 text-sm text-muted-foreground">
              {data.counters.serves + data.counters.calls === 0
                ? "Este grafo não tem endpoints nem rotas."
                : "Nada com estes filtros."}
            </p>
          ) : (
            <>
              <Table items={data.items} names={names} selected={selected} onSelect={(id) => update({ no: id })} />
              <Pager data={data} page={page} onPage={(next) => update({ pagina: next > 1 ? String(next) : null })} />
            </>
          )}
        </Card>
      ) : null}
    </div>
  );
}

// --------------------------------------------------------------------------- contadores e filtros

type Update = (changes: Record<string, string | null>, replace?: boolean) => void;

function Counters({ data, update }: { data: CommunicationData; update: Update }) {
  const c = data.counters;
  const cards: { label: string; value: string; hint: string; filter?: Record<string, string | null> }[] = [
    { label: "Entram", value: formatNumber(c.serves), hint: "o projeto serve", filter: { direcao: "entra" } },
    { label: "Saem", value: formatNumber(c.calls), hint: "o projeto chama", filter: { direcao: "sai" } },
    { label: "Com parâmetro declarado", value: formatNumber(c.withParams), hint: "contrato conhecido" },
    {
      label: "Órfãos",
      value: c.orphans == null ? "—" : formatNumber(c.orphans),
      hint:
        c.orphans == null
          ? "precisa de outro projeto com grafo"
          : "sem servidor ou sem cliente, entre os projetos mapeados",
    },
  ];
  return (
    <dl className="grid grid-cols-2 gap-3 md:grid-cols-4" aria-label="Contadores">
      {cards.map((card) => (
        <div key={card.label} className="rounded-xl border bg-card px-4 py-3">
          <dt className="text-xs text-muted-foreground">{card.label}</dt>
          <dd className="text-2xl font-semibold tabular-nums">
            {card.filter ? (
              <button type="button" className="hover:underline" onClick={() => update(card.filter ?? {})}>
                {card.value}
              </button>
            ) : (
              card.value
            )}
          </dd>
          <dd className="text-xs text-muted-foreground">{card.hint}</dd>
        </div>
      ))}
    </dl>
  );
}

function Filtros({
  data,
  filters,
  names,
  update,
}: {
  data: CommunicationData | null;
  filters: Filters;
  names: Map<string, string>;
  update: Update;
}) {
  const [draft, setDraft] = useState(filters.search ?? "");
  const commit = () => {
    if (draft.trim() !== (filters.search ?? "")) update({ q: draft.trim() || null });
  };
  const options = (list: { value: string; count: number }[] | undefined, label?: (value: string) => string) =>
    (list ?? []).map((f) => (
      <option key={f.value} value={f.value}>
        {label ? label(f.value) : f.value} ({formatNumber(f.count)})
      </option>
    ));
  return (
    <div className="flex flex-wrap items-end gap-3" role="search" aria-label="Filtros">
      <label className="flex flex-col gap-1 text-sm">
        Sistema
        <select
          value={filters.system ?? ""}
          onChange={(event) => update({ sistema: event.target.value || null })}
          className={SELECT}
        >
          <option value="">Todos</option>
          {options(data?.systems.filter((s) => s.value))}
        </select>
      </label>
      <label className="flex flex-col gap-1 text-sm">
        Protocolo
        <select
          value={filters.protocol ?? ""}
          onChange={(event) => update({ protocolo: event.target.value || null })}
          className={SELECT}
        >
          <option value="">Todos</option>
          {options(data?.protocols, protocolLabel)}
        </select>
      </label>
      <label className="flex flex-col gap-1 text-sm">
        Direção
        <select
          value={filters.direction ?? ""}
          onChange={(event) => update({ direcao: event.target.value || null })}
          className={SELECT}
        >
          <option value="">Entram e saem</option>
          <option value="entra">Entram (o projeto serve)</option>
          <option value="sai">Saem (o projeto chama)</option>
        </select>
      </label>
      <label className="flex flex-col gap-1 text-sm">
        Módulo
        <select
          value={filters.module ?? ""}
          onChange={(event) => update({ modulo: event.target.value || null })}
          className={SELECT}
        >
          <option value="">Todos</option>
          {options(data?.modules, (m) => moduleName(m, names))}
        </select>
      </label>
      <label className="flex min-w-56 flex-1 flex-col gap-1 text-sm">
        Texto
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter") commit();
          }}
          placeholder="nome, parâmetro ou arquivo (ex.: cpf)"
          className="h-9 rounded-md border border-input bg-card px-3"
        />
      </label>
    </div>
  );
}

// --------------------------------------------------------------------------- lista

function Table({
  items,
  names,
  selected,
  onSelect,
}: {
  items: CommunicationRow[];
  names: Map<string, string>;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <table className="w-full text-sm" aria-label="Endpoints e rotas">
      <thead className="text-left text-xs text-muted-foreground">
        <tr>
          <th className="px-5 py-2 font-medium">Nome</th>
          <th className="px-2 py-2 font-medium">Protocolo</th>
          <th className="px-2 py-2 font-medium">Direção</th>
          <th className="px-2 py-2 font-medium">Parâmetros</th>
          <th className="px-2 py-2 font-medium">Implementado em</th>
          <th className="px-2 py-2 text-right font-medium">Chamadas</th>
          <th className="px-2 py-2 font-medium">Situação</th>
          <th className="px-5 py-2 font-medium">Módulo</th>
        </tr>
      </thead>
      <tbody className="divide-y border-t">
        {items.map((row) => (
          <tr key={row.endpointId} className={cn("align-top", selected === row.endpointId && "bg-muted")}>
            <td className="px-5 py-2">
              <button
                type="button"
                className="break-all text-left font-mono text-xs text-primary hover:underline"
                aria-label={`Abrir ${row.name}`}
                onClick={() => onSelect(row.endpointId)}
              >
                {row.name}
              </button>
              <div className="text-xs text-muted-foreground">
                {row.system && row.system !== "" ? `${row.system} · ` : ""}
                {row.kind ?? row.nodeLabel}
              </div>
            </td>
            <td className="px-2 py-2">{protocolLabel(row.category)}</td>
            <td className="px-2 py-2">
              <DirectionBadge direction={row.direction} />
            </td>
            <td className="px-2 py-2 font-mono text-xs">{paramsText(row.params)}</td>
            <td className="px-2 py-2">
              {row.implementedBy[0]?.path ? (
                <EvidenceLink path={row.implementedBy[0].path} line={row.implementedBy[0].line} />
              ) : (
                <span className="text-muted-foreground">—</span>
              )}
              {row.implementedBy.length > 1 ? (
                <span className="text-xs text-muted-foreground"> +{row.implementedBy.length - 1}</span>
              ) : null}
            </td>
            <td className="px-2 py-2 text-right tabular-nums">{row.callers ? formatNumber(row.callers) : "—"}</td>
            <td className="px-2 py-2">
              <StatusBadge status={row.status} />
            </td>
            <td className="px-5 py-2 text-muted-foreground">{row.module ? moduleName(row.module, names) : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Pager({ data, page, onPage }: { data: CommunicationData; page: number; onPage: (page: number) => void }) {
  const pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
  return (
    <nav className="flex items-center gap-3 border-t px-5 py-2 text-sm" aria-label="Paginação">
      <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        Anterior
      </Button>
      <span aria-live="polite" className="text-muted-foreground">
        {rangeText(data)} · página {formatNumber(page)} de {formatNumber(pages)}
      </span>
      <Button variant="outline" size="sm" disabled={page >= pages} onClick={() => onPage(page + 1)}>
        Próxima
      </Button>
    </nav>
  );
}

// --------------------------------------------------------------------------- detalhe

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-1.5">
      <h3 className="text-sm font-semibold">{title}</h3>
      {children}
    </section>
  );
}

type Param = EndpointDetail["row"]["params"][number];

function ParamsTable({ params, empty }: { params: Param[]; empty: string }) {
  if (!params.length) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <table className="w-full text-sm" aria-label="Parâmetros">
      <thead className="text-left text-xs text-muted-foreground">
        <tr>
          <th className="py-1 pr-3 font-medium">Nome</th>
          <th className="py-1 pr-3 font-medium">Obrigatório</th>
          <th className="py-1 pr-3 font-medium">Tipo</th>
          <th className="py-1 font-medium">Origem</th>
        </tr>
      </thead>
      <tbody className="divide-y border-t">
        {params.map((p) => (
          <tr key={p.name}>
            <td className="py-1 pr-3 font-mono text-xs">{p.name}</td>
            <td className="py-1 pr-3">{p.required == null ? "—" : p.required ? "sim" : "não"}</td>
            <td className="py-1 pr-3">{p.type ?? "—"}</td>
            <td className="py-1 text-muted-foreground">{p.source ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const SHOWN_TABLES = 30;

function Reach({ reach, names }: { reach: NonNullable<EndpointDetail["row"]["reaches"]>; names: Map<string, string> }) {
  const [all, setAll] = useState(false);
  const tables = all ? reach.tables : reach.tables.slice(0, SHOWN_TABLES);
  return (
    <div className="space-y-1.5 text-sm">
      <p className="text-muted-foreground">
        {formatNumber(reach.tables.length)} {reach.tables.length === 1 ? "tabela" : "tabelas"} e{" "}
        {formatNumber(reach.nodes)} {reach.nodes === 1 ? "nó" : "nós"} alcançados em 3 passos
        {reach.modules.length ? ` · módulos: ${reach.modules.map((m) => moduleName(m, names)).join(", ")}` : ""}.
      </p>
      {tables.length ? (
        <ul className="flex flex-wrap gap-x-4 gap-y-1" aria-label="Tabelas alcançadas">
          {tables.map((id) => (
            <li key={id}>
              <NodeLink id={id} className="font-mono text-xs" />
            </li>
          ))}
        </ul>
      ) : null}
      {reach.tables.length > SHOWN_TABLES && !all ? (
        <Button variant="link" size="sm" onClick={() => setAll(true)}>
          Mostrar as {formatNumber(reach.tables.length - SHOWN_TABLES)} restantes
        </Button>
      ) : null}
    </div>
  );
}

function Callers({
  callers,
  names,
  label,
}: {
  callers: { project?: string | null; path?: string | null; line?: number | null; module?: string | null; occurrences: number }[];
  names: Map<string, string>;
  label: string;
}) {
  if (!callers.length) return <p className="text-sm text-muted-foreground">Ninguém chama.</p>;
  return (
    <table className="w-full text-sm" aria-label={label}>
      <thead className="text-left text-xs text-muted-foreground">
        <tr>
          <th className="py-1 pr-3 font-medium">Projeto</th>
          <th className="py-1 pr-3 font-medium">Arquivo</th>
          <th className="py-1 pr-3 font-medium">Módulo</th>
          <th className="py-1 text-right font-medium">Chamadas</th>
        </tr>
      </thead>
      <tbody className="divide-y border-t">
        {callers.map((c, index) => (
          <tr key={`${c.path}:${c.line}:${index}`}>
            <td className="py-1 pr-3">{c.project ?? "este projeto"}</td>
            <td className="py-1 pr-3">
              {!c.path ? (
                "—"
              ) : c.project ? (
                // O painel de código lê a pasta do projeto ativo: o arquivo de outro projeto fica em texto.
                <span className="font-mono text-xs text-muted-foreground">
                  {c.path}
                  {c.line ? `:${c.line}` : ""}
                </span>
              ) : (
                <EvidenceLink path={c.path} line={c.line} />
              )}
            </td>
            <td className="py-1 pr-3 text-muted-foreground">{c.module ? moduleName(c.module, names) : "—"}</td>
            <td className="py-1 text-right tabular-nums">{c.occurrences}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DetailCard({ id, names, onClose }: { id: string; names: Map<string, string>; onClose: () => void }) {
  const query = useQuery({
    queryKey: ["endpointDetail", id],
    queryFn: () => request(EndpointDetailDocument, { id }),
    meta: { silent: true },
  });
  const detail = query.data?.endpointDetail;
  return (
    <Card aria-label="Detalhe do endpoint">
      <CardHeader
        title={detail ? detail.row.name : keyOf(id)}
        description={detail ? <span className="font-mono">{detail.row.endpointId}</span> : undefined}
        action={
          <Button variant="ghost" size="icon" aria-label="Fechar o detalhe" onClick={onClose}>
            <X className="h-4 w-4" aria-hidden="true" />
          </Button>
        }
      />
      <div className="space-y-5 px-5 py-4">
        {query.isPending ? (
          <LoadingRows rows={4} label="Carregando o detalhe" />
        ) : query.isError ? (
          <Notice tone="warning">{query.error.message}</Notice>
        ) : detail ? (
          <DetailBody detail={detail} names={names} />
        ) : null}
      </div>
    </Card>
  );
}

function DetailBody({ detail, names }: { detail: EndpointDetail; names: Map<string, string> }) {
  const row = detail.row;
  const other = detail.counterpart;
  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        <Badge className="bg-card">{protocolLabel(row.category)}</Badge>
        <DirectionBadge direction={row.direction} />
        <StatusBadge status={row.status} />
        {row.kind ? <span className="text-xs text-muted-foreground">{row.kind}</span> : null}
        {row.module ? <span className="text-xs text-muted-foreground">módulo {moduleName(row.module, names)}</span> : null}
      </div>

      <Section title="Parâmetros">
        <ParamsTable
          params={row.params}
          empty={
            row.direction === "sai"
              ? "O contrato é de quem serve: veja o outro lado da costura abaixo."
              : "Nenhum parâmetro declarado."
          }
        />
      </Section>

      <Section title="Quem implementa">
        {row.implementedBy.length ? (
          <ul className="space-y-0.5" aria-label="Implementado em">
            {row.implementedBy.map((impl) => (
              <li key={`${impl.node}:${impl.path}:${impl.line}`} className="flex flex-wrap items-center gap-2">
                {impl.path ? <EvidenceLink path={impl.path} line={impl.line} /> : null}
                <span className="font-mono text-xs text-muted-foreground">{keyOf(impl.node)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">Este projeto não implementa: ele chama.</p>
        )}
      </Section>

      <Section title="Quem chama (neste projeto)">
        <Callers callers={detail.callers} names={names} label="Chamadores" />
      </Section>

      {row.reaches ? (
        <Section title="O que alcança">
          <Reach reach={row.reaches} names={names} />
        </Section>
      ) : null}

      {other ? (
        <Section title="Outro lado da costura">
          {!other.available ? (
            <Notice>
              {other.message}{" "}
              <Link to="/projetos" className="text-primary hover:underline">
                Cadastrar o projeto
              </Link>
            </Notice>
          ) : (
            <div className="space-y-3 rounded-lg border px-4 py-3">
              <p className="text-sm">
                {other.servedBy ? (
                  <>
                    Servido por <strong>{other.servedBy}</strong>.
                  </>
                ) : (
                  (other.message ?? "Sem servidor nos projetos mapeados.")
                )}{" "}
                {other.servedBy && other.message ? other.message : ""}
              </p>
              {other.servedBy && row.direction === "sai" ? (
                <>
                  <ParamsTable params={other.params} empty="O servidor não declara parâmetros." />
                  {other.servers.length ? (
                    <ul className="space-y-0.5" aria-label="Servido em">
                      {other.servers.map((s) => (
                        <li key={`${s.node}:${s.path}`} className="font-mono text-xs text-muted-foreground">
                          {s.path}
                          {s.line ? `:${s.line}` : ""}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {other.reaches ? <Reach reach={other.reaches} names={names} /> : null}
                </>
              ) : null}
              {row.direction === "entra" && other.callers.length ? (
                <Callers callers={other.callers} names={names} label="Chamadores de outros projetos" />
              ) : null}
            </div>
          )}
        </Section>
      ) : null}
    </>
  );
}
