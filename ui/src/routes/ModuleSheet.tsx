/** Ficha do módulo: `/modulos/:id`. */
import { useQuery } from "@tanstack/react-query";
import { Radar, ScrollText } from "lucide-react";
import { type ReactNode, useState } from "react";
import { Link, useParams } from "react-router";
import { LabelBadge, NodeLink } from "../components/NodeBits";
import { Badge, Button, Card, CardHeader, LoadingRows, Notice } from "../components/ui";
import { graphql } from "../graphql/generated";
import { request } from "../lib/api";
import { keyOf, rulesHref } from "../lib/ids";
import {
  candidateScore,
  EDGE_KINDS,
  type EdgeKind,
  formatWeight,
  KIND_LABELS,
  maxCeOf,
  type ModuleEdge,
  type ModuleRow,
  NO_MODULE,
} from "../lib/modules";
import { formatNumber } from "../lib/utils";
import { ModuleNumbers, moduleHref, PairSamples } from "./Modules";

const ModuleSheetDocument = graphql(`
  query ModuleSheet($id: ID!) {
    module(id: $id) {
      module {
        ...ModuleFields
      }
      incoming {
        ...ModuleEdgeFields
      }
      outgoing {
        ...ModuleEdgeFields
      }
      tables {
        ...TableStatsFields
      }
      writesForeign {
        ...TableStatsFields
      }
      screens {
        fileId
        inMenu
        menuItems
        tablesWritten
        tablesRead
        reachedBy
      }
      hotspots {
        nodeId
        label
        inDegree
        fanInModules
      }
    }
    modules {
      ...ModuleFields
    }
  }

  fragment ModuleEdgeFields on ModuleEdge {
    src
    dst
    kind
    weight
    edges
    samples
  }

  fragment TableStatsFields on TableStats {
    tableId
    ownerModules
    readerModules
    writerModules
    readers
    writers
    shared
  }
`);

export const LIST_LIMIT = 100;

type Pair = { other: string; total: number; byKind: Partial<Record<EdgeKind, ModuleEdge>> };

/** Arestas do módulo agrupadas pelo outro módulo, do mais forte para o mais fraco. */
export function groupPairs(edges: ModuleEdge[], side: "src" | "dst"): Pair[] {
  const pairs = new Map<string, Pair>();
  for (const e of edges) {
    const other = e[side];
    const pair = pairs.get(other) ?? { other, total: 0, byKind: {} };
    pair.total += e.weight;
    pair.byKind[e.kind as EdgeKind] = e;
    pairs.set(other, pair);
  }
  return [...pairs.values()].sort((a, b) => b.total - a.total || a.other.localeCompare(b.other));
}

/** `{"Module:a": 3, …}` → os `n` maiores, com o nome do módulo. */
export function topModules(map: unknown, name: (id: string) => string, n = 3): string {
  const entries = Object.entries((map ?? {}) as Record<string, number>).sort((a, b) => b[1] - a[1]);
  const shown = entries.slice(0, n).map(([id, count]) => `${name(id)} (${count})`);
  return entries.length > n ? `${shown.join(", ")} +${entries.length - n}` : shown.join(", ") || "—";
}

function Section({ title, description, children }: { title: ReactNode; description?: string; children: ReactNode }) {
  return (
    <Card aria-label={typeof title === "string" ? title : undefined}>
      <CardHeader title={title} description={description} />
      {children}
    </Card>
  );
}

function impactHref(tableId: string): string {
  return `/impacto?tabela=${encodeURIComponent(keyOf(tableId))}`;
}

export function ModuleSheetPage() {
  const id = useParams().id ?? "";
  const query = useQuery({
    queryKey: ["moduleSheet", id],
    queryFn: () => request(ModuleSheetDocument, { id }),
    enabled: id.length > 0,
    meta: { silent: true },
  });
  if (query.isPending) return <LoadingRows rows={10} label="Carregando o módulo" />;
  if (query.isError) return <Notice tone="warning">{query.error.message}</Notice>;
  const detail = query.data.module;
  if (!detail) {
    return (
      <Notice tone="warning">
        Módulo não encontrado: <code className="font-mono">{id}</code>. <Link to="/modulos" className="underline">Voltar aos módulos</Link>
      </Notice>
    );
  }
  const modules = query.data.modules as ModuleRow[];
  const names = new Map(modules.map((m) => [m.module, m.name]));
  const name = (m: string) => names.get(m) ?? (m === NO_MODULE ? NO_MODULE : keyOf(m));
  const m = detail.module as ModuleRow;
  const candidate = m.module === NO_MODULE ? null : candidateScore(m, maxCeOf(modules));

  return (
    <div className="space-y-5">
      <header className="space-y-2">
        <p className="text-sm text-muted-foreground">
          <Link to="/modulos" className="hover:underline">
            Módulos
          </Link>
        </p>
        <h1 className="text-2xl font-semibold">{m.name}</h1>
        <p className="font-mono text-xs text-muted-foreground">{m.module}</p>
      </header>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Card className="px-5 py-4" aria-label="Números do módulo">
          <ModuleNumbers module={m} />
        </Card>
        {candidate ? (
          <Card className="space-y-1 px-5 py-4 text-sm" aria-label="Candidato a extração">
            <p className="text-xs text-muted-foreground">Pontuação de candidato a extração</p>
            <p className="text-2xl font-semibold tabular-nums">
              {candidate.score.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}
              {candidate.reliable ? null : (
                <span className="ml-2 align-middle rounded-full border border-warning px-2 text-xs font-normal text-warning">pouco confiável</span>
              )}
            </p>
            <ul className="space-y-0.5 text-xs text-muted-foreground">
              {candidate.explanation.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </Card>
        ) : null}
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Dependencies title="Depende de" pairs={groupPairs(detail.outgoing as ModuleEdge[], "dst")} name={name} />
        <Dependencies title="Dependem dele" pairs={groupPairs(detail.incoming as ModuleEdge[], "src")} name={name} />
      </div>

      <Section title="Tabelas próprias" description="Do dicionário do e-cidade, com quem mais lê e grava cada uma.">
        <TableList tables={detail.tables} name={name} empty="O módulo não é dono de tabelas no dicionário." />
      </Section>
      <Section
        title="Tabelas de outros módulos que ele grava"
        description="Sinal forte de acoplamento: o módulo altera dados que pertencem a outro."
      >
        <TableList tables={detail.writesForeign} name={name} empty="Não grava tabelas de outros módulos." showOwner />
      </Section>
      <Section title="Telas" description="Com a situação no menu e a ficha de regras.">
        <Screens screens={detail.screens} />
      </Section>
      <Section title="Pontos quentes" description="Os nós do módulo que mais módulos alcançam.">
        {detail.hotspots.length ? (
          <ul className="divide-y text-sm">
            {detail.hotspots.map((h) => (
              <li key={h.nodeId} className="flex flex-wrap items-center gap-2 px-5 py-1.5">
                <LabelBadge label={h.label} />
                <NodeLink id={h.nodeId} className="min-w-0 flex-1" />
                <span className="text-xs text-muted-foreground">
                  {formatNumber(h.fanInModules)} módulos · {formatNumber(h.inDegree)} ligações de entrada
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="px-5 py-3 text-sm text-muted-foreground">Nenhum ponto quente neste módulo.</p>
        )}
      </Section>
    </div>
  );
}

function Dependencies({ title, pairs, name }: { title: string; pairs: Pair[]; name: (id: string) => string }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <Section title={`${title} (${pairs.length})`} description="Peso somado dos três tipos; clique para ver as amostras.">
      {pairs.length ? (
        <ul className="divide-y text-sm" aria-label={title}>
          {pairs.map((p) => (
            <li key={p.other} className="px-5 py-1.5">
              <div className="flex flex-wrap items-center gap-2">
                {p.other === NO_MODULE ? (
                  <span className="text-muted-foreground">{NO_MODULE}</span>
                ) : (
                  <Link className="text-primary hover:underline" to={moduleHref(p.other)}>
                    {name(p.other)}
                  </Link>
                )}
                <span className="text-xs tabular-nums text-muted-foreground">
                  {EDGE_KINDS.filter((k) => p.byKind[k])
                    .map((k) => `${KIND_LABELS[k]} ${formatWeight(p.byKind[k]!.weight)}`)
                    .join(" · ")}
                </span>
                <span className="ml-auto font-semibold tabular-nums">{formatWeight(p.total)}</span>
                <Button variant="ghost" size="sm" onClick={() => setOpen(open === p.other ? null : p.other)} aria-expanded={open === p.other}>
                  {open === p.other ? "Esconder" : "Amostras"}
                </Button>
              </div>
              {open === p.other ? (
                <div className="mt-1 rounded-md bg-muted/60 px-3 py-2">
                  <PairSamples byKind={p.byKind} />
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="px-5 py-3 text-sm text-muted-foreground">Nenhuma dependência.</p>
      )}
    </Section>
  );
}

type TableStats = { tableId: string; ownerModules: string[]; readerModules: unknown; writerModules: unknown; readers: number; writers: number; shared: boolean };

function TableList({ tables, name, empty, showOwner }: { tables: TableStats[]; name: (id: string) => string; empty: string; showOwner?: boolean }) {
  const [limit, setLimit] = useState(LIST_LIMIT);
  if (!tables.length) return <p className="px-5 py-3 text-sm text-muted-foreground">{empty}</p>;
  return (
    <>
      <table className="w-full text-sm">
        <thead className="text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-5 py-2 font-medium">Tabela</th>
            {showOwner ? <th className="px-2 py-2 font-medium">Dono</th> : null}
            <th className="px-2 py-2 font-medium">Quem mais lê</th>
            <th className="px-2 py-2 font-medium">Quem mais grava</th>
            <th className="px-5 py-2" />
          </tr>
        </thead>
        <tbody className="divide-y">
          {tables.slice(0, limit).map((t) => (
            <tr key={t.tableId}>
              <td className="px-5 py-1.5">
                <NodeLink id={t.tableId} />
                {t.shared ? <Badge className="ml-2 text-muted-foreground">compartilhada</Badge> : null}
              </td>
              {showOwner ? <td className="px-2 py-1.5 text-xs">{t.ownerModules.map(name).join(", ")}</td> : null}
              <td className="px-2 py-1.5 text-xs text-muted-foreground">{topModules(t.readerModules, name)}</td>
              <td className="px-2 py-1.5 text-xs text-muted-foreground">{topModules(t.writerModules, name)}</td>
              <td className="px-5 py-1.5 text-right">
                <Link className="inline-flex items-center gap-1 text-xs text-primary hover:underline" to={impactHref(t.tableId)}>
                  <Radar className="h-3.5 w-3.5" aria-hidden="true" /> Ver impacto
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {tables.length > limit ? (
        <div className="border-t px-5 py-2">
          <Button variant="ghost" size="sm" onClick={() => setLimit(limit + LIST_LIMIT)}>
            Mostrar mais ({formatNumber(tables.length - limit)} restantes)
          </Button>
        </div>
      ) : null}
    </>
  );
}

type Screen = { fileId: string; inMenu: boolean; menuItems: number; tablesWritten: number; tablesRead: number; reachedBy: number };

function Screens({ screens }: { screens: Screen[] }) {
  const [onlyOut, setOnlyOut] = useState(false);
  const [limit, setLimit] = useState(LIST_LIMIT);
  const list = onlyOut ? screens.filter((s) => !s.inMenu) : screens;
  const outside = screens.filter((s) => !s.inMenu).length;
  if (!screens.length) return <p className="px-5 py-3 text-sm text-muted-foreground">Nenhuma tela neste módulo.</p>;
  return (
    <>
      <div className="flex items-center gap-3 px-5 py-2 text-sm">
        <span className="text-muted-foreground">
          {formatNumber(screens.length)} telas, {formatNumber(outside)} fora do menu
        </span>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={onlyOut} onChange={(event) => setOnlyOut(event.target.checked)} />
          Só fora do menu
        </label>
      </div>
      <ul className="divide-y border-t text-sm">
        {list.slice(0, limit).map((s) => (
          <li key={s.fileId} className="flex flex-wrap items-center gap-2 px-5 py-1.5">
            <NodeLink id={s.fileId} name={keyOf(s.fileId)} className="min-w-0 flex-1" />
            {s.inMenu ? <Badge>no menu</Badge> : <Badge className="text-muted-foreground">fora do menu</Badge>}
            <span className="text-xs tabular-nums text-muted-foreground">
              grava {s.tablesWritten} · lê {s.tablesRead} · alcançada por {s.reachedBy}
            </span>
            <Link className="inline-flex items-center gap-1 text-xs text-primary hover:underline" to={rulesHref(s.fileId)}>
              <ScrollText className="h-3.5 w-3.5" aria-hidden="true" /> Ver regras
            </Link>
          </li>
        ))}
      </ul>
      {list.length > limit ? (
        <div className="border-t px-5 py-2">
          <Button variant="ghost" size="sm" onClick={() => setLimit(limit + LIST_LIMIT)}>
            Mostrar mais ({formatNumber(list.length - limit)} restantes)
          </Button>
        </div>
      ) : null}
    </>
  );
}
