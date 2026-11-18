/**
 * Módulos: `/modulos?aba=mapa|matriz|ranking`. Mapa de acoplamento (Cytoscape
 * `fcose`), matriz módulo × módulo e ranking de candidatos a extração.
 */
import { useQuery } from "@tanstack/react-query";
import type { ElementDefinition } from "cytoscape";
import { Download, Maximize, TriangleAlert } from "lucide-react";
import { type ReactNode, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { useStats } from "../app/Layout";
import { EvidenceLink } from "../app/code-panel";
import { GraphCanvas, type GraphCanvasHandle } from "../components/graph/GraphCanvas";
import { Button, Card, LoadingRows, Notice, Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui";
import { request } from "../lib/api";
import { coverage } from "../lib/coverage";
import { withParams } from "../lib/graph-url";
import { shortName } from "../lib/ids";
import {
  buildMatrix,
  candidateScore,
  EDGE_KINDS,
  type EdgeKind,
  formatWeight,
  instabilityColor,
  KIND_LABELS,
  matrixCsv,
  maxCeOf,
  type ModuleEdge,
  type ModuleRow,
  ModulesOverviewDocument,
  NO_MODULE,
  rankingCsv,
  realModules,
  SCORE_FORMULA,
  samplesOf,
  type SortKey,
  sortModules,
  weightAlpha,
} from "../lib/modules";
import { formatNumber, formatPercent } from "../lib/utils";

export const TABS = ["mapa", "matriz", "ranking"] as const;
type Tab = (typeof TABS)[number];
const EDGE_LIMITS = [50, 150, 500, 0] as const; // 0 = todas

export function moduleHref(id: string): string {
  return `/modulos/${encodeURIComponent(id)}`;
}

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

export function InstabilityBadge({ value }: { value: number | null | undefined }) {
  return (
    <span className="inline-flex items-center gap-1.5 tabular-nums">
      <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: instabilityColor(value) }} aria-hidden="true" />
      {value === null || value === undefined ? "—" : value.toLocaleString("pt-BR", { maximumFractionDigits: 2 })}
    </span>
  );
}

export function ModulesPage() {
  const [params, setParams] = useSearchParams();
  const tab: Tab = (TABS as readonly string[]).includes(params.get("aba") ?? "") ? (params.get("aba") as Tab) : "mapa";
  const update = (changes: Record<string, string | null>) => setParams((current) => withParams(current, changes));
  const query = useQuery({ queryKey: ["modulesOverview"], queryFn: () => request(ModulesOverviewDocument), meta: { silent: true } });
  const stats = useStats();
  const cov = coverage(stats.data);

  if (query.isPending) return <LoadingRows rows={10} label="Carregando os módulos" />;
  if (query.isError) return <Notice tone="warning">{query.error.message}</Notice>;
  const modules = query.data.modules;
  const edges = query.data.moduleEdges as ModuleEdge[];
  const none = modules.find((m) => m.module === NO_MODULE);
  const share = cov.total ? (cov.withModule ?? 0) / cov.total : null;

  return (
    <div className="space-y-5">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">Módulos</h1>
        <p className="text-sm text-muted-foreground">
          Quem depende de quem, por código e por tabela, e quais módulos são bons candidatos a extrair primeiro.
          Instabilidade I = Ce ÷ (Ca + Ce): perto de 0 é estável (muitos dependem dele), perto de 1 é instável.
        </p>
      </header>
      {share === null ? (
        <Notice tone="warning">
          <TriangleAlert className="mr-1 inline h-4 w-4" aria-hidden="true" />
          Scan sem catálogo do e-cidade: quase tudo cai em “{NO_MODULE}” ({formatNumber(none?.files)} arquivos). Rode o scan com{" "}
          <code className="font-mono">--catalog-db</code> para os números abaixo fazerem sentido.
        </Notice>
      ) : share < 0.7 ? (
        <Notice tone="warning">
          <TriangleAlert className="mr-1 inline h-4 w-4" aria-hidden="true" />
          Cobertura de módulo baixa: {formatPercent(cov.withModule ?? 0, cov.total ?? 0)} dos arquivos têm módulo;{" "}
          {formatNumber(none?.files)} estão em “{NO_MODULE}”. O acoplamento com eles não aparece entre os módulos.
        </Notice>
      ) : null}
      <Tabs value={tab} onValueChange={(value) => update({ aba: value === "mapa" ? null : value })}>
        <TabsList aria-label="Visões dos módulos">
          <TabsTrigger value="mapa">Mapa</TabsTrigger>
          <TabsTrigger value="matriz">Matriz</TabsTrigger>
          <TabsTrigger value="ranking">Ranking</TabsTrigger>
        </TabsList>
        <TabsContent value="mapa" className="pt-4">
          <ModuleMap modules={modules} edges={edges} params={params} update={update} />
        </TabsContent>
        <TabsContent value="matriz" className="pt-4">
          <ModuleMatrix modules={modules} edges={edges} params={params} update={update} />
        </TabsContent>
        <TabsContent value="ranking" className="pt-4">
          <Ranking modules={modules} params={params} update={update} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

type ViewProps = {
  modules: ModuleRow[];
  params: URLSearchParams;
  update: (changes: Record<string, string | null>) => void;
};

function kindOf(params: URLSearchParams): EdgeKind {
  const value = params.get("tipo");
  return EDGE_KINDS.includes(value as EdgeKind) ? (value as EdgeKind) : "code";
}

function KindPicker({ value, onChange, all }: { value: string; onChange: (value: string) => void; all?: boolean }) {
  return (
    <div role="group" aria-label="Tipo de acoplamento" className="flex flex-wrap gap-1">
      {[...(all ? ["todos"] : []), ...EDGE_KINDS].map((kind) => (
        <button
          key={kind}
          type="button"
          aria-pressed={value === kind}
          onClick={() => onChange(kind)}
          className={`rounded-full border px-3 py-1 text-xs ${value === kind ? "bg-accent font-medium text-accent-foreground" : "hover:bg-muted"}`}
        >
          {kind === "todos" ? "todos" : KIND_LABELS[kind as EdgeKind]}
        </button>
      ))}
    </div>
  );
}

// --------------------------------------------------------------------------- mapa

function ModuleMap({ modules, edges, params, update }: ViewProps & { edges: ModuleEdge[] }) {
  const canvas = useRef<GraphCanvasHandle>(null);
  const kind = kindOf(params);
  const limit = Number(params.get("arestas") ?? 150);
  const withNone = params.get("semmodulo") === "1";
  const selected = params.get("sel");
  const shown = useMemo(() => (withNone ? modules : realModules(modules)), [modules, withNone]);
  const chosen = useMemo(() => {
    const ids = new Set(shown.map((m) => m.module));
    const list = edges.filter((e) => e.kind === kind && ids.has(e.src) && ids.has(e.dst) && e.src !== e.dst);
    list.sort((a, b) => b.weight - a.weight);
    return limit > 0 ? list.slice(0, limit) : list;
  }, [edges, kind, limit, shown]);
  const elements: ElementDefinition[] = useMemo(() => {
    const maxFiles = Math.max(1, ...shown.map((m) => m.files));
    const maxWeight = Math.max(1e-9, ...chosen.map((e) => e.weight));
    return [
      ...shown.map((m) => ({
        group: "nodes" as const,
        data: {
          id: m.module,
          label: m.name,
          kind: "Module",
          size: Math.round(18 + 62 * Math.sqrt(m.files / maxFiles)),
          color: instabilityColor(m.instability),
        },
        classes: "node module",
      })),
      ...chosen.map((e) => ({
        group: "edges" as const,
        data: {
          id: `${e.src}->${e.dst}`,
          source: e.src,
          target: e.dst,
          type: e.kind,
          confidence: 1,
          width: Math.round((1 + 7 * Math.sqrt(e.weight / maxWeight)) * 10) / 10,
          weight: e.weight,
        },
        classes: "edge module-edge",
      })),
    ];
  }, [shown, chosen]);
  const module = selected ? modules.find((m) => m.module === selected) : null;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <KindPicker value={kind} onChange={(value) => update({ tipo: value === "code" ? null : value, sel: null })} />
        <label className="flex items-center gap-2">
          Arestas
          <select
            aria-label="Quantas arestas"
            value={limit}
            onChange={(event) => update({ arestas: event.target.value === "150" ? null : event.target.value })}
            className="h-8 rounded-md border border-input bg-card px-2"
          >
            {EDGE_LIMITS.map((n) => (
              <option key={n} value={n}>
                {n ? `as ${n} mais fortes` : "todas"}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={withNone} onChange={(event) => update({ semmodulo: event.target.checked ? "1" : null })} />
          Incluir “{NO_MODULE}”
        </label>
        <Button variant="outline" size="sm" onClick={() => canvas.current?.fit()}>
          <Maximize className="h-4 w-4" aria-hidden="true" /> Ajustar à tela
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            const uri = canvas.current?.png();
            if (!uri) return;
            const link = document.createElement("a");
            link.href = uri;
            link.download = `modulos-${kind}.png`;
            link.click();
          }}
        >
          <Download className="h-4 w-4" aria-hidden="true" /> Exportar PNG
        </Button>
        <span className="ml-auto text-xs text-muted-foreground" data-testid="mapa-info">
          {shown.length} módulos · {chosen.length} arestas de {KIND_LABELS[kind]}
        </span>
      </div>
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <Card className="relative h-[65vh] overflow-hidden">
          <GraphCanvas
            ref={canvas}
            elements={elements}
            layout="fcose"
            selected={selected}
            onSelect={(id) => update({ sel: id })}
            label={`Mapa de ${shown.length} módulos. A aba Ranking traz os mesmos módulos em tabela.`}
            extraStyle={(tokenOf) => [
              {
                selector: "node.module",
                style: {
                  width: "data(size)",
                  height: "data(size)",
                  "background-color": "data(color)",
                  "border-width": 1,
                  "border-color": tokenOf("--border"),
                  "text-valign": "center",
                  "text-margin-y": 0,
                  "font-size": 11,
                  "text-outline-color": tokenOf("--card"),
                  "text-outline-width": 2,
                },
              },
              { selector: "edge.module-edge", style: { "curve-style": "bezier", opacity: 0.55 } },
            ]}
          />
          <div className="pointer-events-none absolute bottom-2 left-2 rounded-md border bg-card/90 px-3 py-2 text-[11px] shadow-sm" role="note" aria-label="Legenda do mapa">
            <div className="flex items-center gap-2">
              <span>estável (I = 0)</span>
              <span
                className="inline-block h-2.5 w-24 rounded"
                style={{ background: `linear-gradient(to right, ${instabilityColor(0)}, ${instabilityColor(0.5)}, ${instabilityColor(1)})` }}
                aria-hidden="true"
              />
              <span>instável (I = 1)</span>
            </div>
            <p>Tamanho = arquivos · espessura = peso da dependência · seta: quem depende → de quem</p>
          </div>
        </Card>
        <Card className="space-y-2 px-4 py-3 text-sm" aria-label="Módulo selecionado">
          {module ? (
            <>
              <h2 className="font-semibold">{module.name}</h2>
              <ModuleNumbers module={module} />
              <Link className="text-primary hover:underline" to={moduleHref(module.module)}>
                Abrir a ficha do módulo
              </Link>
            </>
          ) : (
            <p className="text-muted-foreground">Clique num módulo para ver os números. A matriz e o ranking trazem os mesmos dados.</p>
          )}
        </Card>
      </div>
    </div>
  );
}

export function ModuleNumbers({ module }: { module: ModuleRow }) {
  const facts: [string, ReactNode][] = [
    ["Arquivos", formatNumber(module.files)],
    ["Telas", formatNumber(module.screens)],
    ["Tabelas próprias", `${formatNumber(module.ownedTables)} (${formatNumber(module.ownedShared)} compartilhadas)`],
    ["Ca (dependem dele)", formatNumber(module.ca)],
    ["Ce (depende de)", formatNumber(module.ce)],
    ["Instabilidade", <InstabilityBadge key="i" value={module.instability} />],
    ["Cobertura", module.coverage === null || module.coverage === undefined ? "—" : formatPercent(module.coverage, 1)],
  ];
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
      {facts.map(([term, value]) => (
        <div key={term} className="contents">
          <dt className="text-muted-foreground">{term}</dt>
          <dd className="tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

// --------------------------------------------------------------------------- matriz

const MATRIX_ORDERS: Record<string, SortKey> = { nome: "name", ca: "ca", ce: "ce", i: "instability" };

function ModuleMatrix({ modules, edges, params, update }: ViewProps & { edges: ModuleEdge[] }) {
  const kindParam = params.get("tipo") ?? "todos";
  const { matrix, max } = useMemo(
    () => buildMatrix(edges, EDGE_KINDS.includes(kindParam as EdgeKind) ? [kindParam as EdgeKind] : EDGE_KINDS),
    [edges, kindParam],
  );
  const orderKey = params.get("ordem") ?? "nome";
  const desc = params.get("desc") === "1";
  const withNone = params.get("semmodulo") === "1";
  const [cell, setCell] = useState<{ src: string; dst: string } | null>(null);
  const shown = withNone ? modules : realModules(modules);
  const order = sortModules(shown, MATRIX_ORDERS[orderKey] ?? "name", desc);
  const picked = cell ? matrix.get(cell.src)?.get(cell.dst) : null;
  const name = new Map(modules.map((m) => [m.module, m.name]));

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <KindPicker value={kindParam} all onChange={(value) => update({ tipo: value === "todos" ? null : value })} />
        <label className="flex items-center gap-2">
          Ordenar por
          <select
            aria-label="Ordenar a matriz por"
            value={orderKey}
            onChange={(event) => update({ ordem: event.target.value === "nome" ? null : event.target.value })}
            className="h-8 rounded-md border border-input bg-card px-2"
          >
            <option value="nome">nome</option>
            <option value="ca">Ca</option>
            <option value="ce">Ce</option>
            <option value="i">instabilidade</option>
          </select>
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={desc} onChange={(event) => update({ desc: event.target.checked ? "1" : null })} />
          Decrescente
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={withNone} onChange={(event) => update({ semmodulo: event.target.checked ? "1" : null })} />
          Incluir “{NO_MODULE}”
        </label>
        <Button variant="outline" size="sm" onClick={() => download(matrixCsv(order, matrix), "matriz-modulos.csv")}>
          <Download className="h-4 w-4" aria-hidden="true" /> Exportar CSV
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">Linha depende da coluna. Clique numa célula para ver as amostras com arquivo e linha.</p>
      <Card className="max-h-[70vh] overflow-auto">
        <table className="border-separate border-spacing-0 text-xs" aria-label="Matriz de acoplamento">
          <thead>
            <tr>
              <th className="sticky left-0 top-0 z-20 bg-card px-2 py-1 text-left">depende de →</th>
              {order.map((col) => (
                <th key={col.module} scope="col" title={col.name} className="sticky top-0 z-10 h-32 bg-card px-0.5 align-bottom font-normal">
                  <span className="inline-block max-h-32 truncate [writing-mode:vertical-rl] rotate-180">{col.name}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {order.map((row) => (
              <tr key={row.module}>
                <th scope="row" title={row.name} className="sticky left-0 z-10 max-w-48 truncate bg-card px-2 py-0.5 text-left font-normal">
                  {row.name}
                </th>
                {order.map((col) => {
                  const value = matrix.get(row.module)?.get(col.module);
                  const alpha = value ? weightAlpha(value.weight, max) : 0;
                  const isPicked = cell?.src === row.module && cell.dst === col.module;
                  return (
                    <td key={col.module} className="border border-border/40 p-0">
                      {value ? (
                        <button
                          type="button"
                          onClick={() => setCell({ src: row.module, dst: col.module })}
                          title={`${row.name} → ${col.name}: peso ${formatWeight(value.weight)} (${value.edges} arestas)`}
                          aria-label={`${row.name} depende de ${col.name}: peso ${formatWeight(value.weight)}`}
                          className={`block h-6 w-8 text-[10px] tabular-nums ${isPicked ? "outline outline-2 outline-primary" : ""}`}
                          style={{ background: `rgba(29, 78, 216, ${alpha})`, color: alpha > 0.55 ? "white" : undefined }}
                        >
                          {formatWeight(value.weight)}
                        </button>
                      ) : (
                        <span className="block h-6 w-8" />
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      {cell && picked ? (
        <Card className="space-y-2 px-4 py-3 text-sm" aria-label="Amostras do par">
          <h2 className="font-semibold">
            {name.get(cell.src)} → {name.get(cell.dst)}: peso {formatWeight(picked.weight)} ({formatNumber(picked.edges)} arestas)
          </h2>
          <PairSamples byKind={picked.byKind} />
        </Card>
      ) : null}
    </div>
  );
}

export function PairSamples({ byKind }: { byKind: Partial<Record<EdgeKind, ModuleEdge>> }) {
  return (
    <div className="space-y-2">
      {EDGE_KINDS.filter((k) => byKind[k]).map((k) => {
        const edge = byKind[k]!;
        return (
          <div key={k}>
            <p className="text-xs font-medium">
              {KIND_LABELS[k]}: peso {formatWeight(edge.weight)}, {formatNumber(edge.edges)} arestas (amostras abaixo)
            </p>
            <ul className="space-y-0.5 text-xs">
              {samplesOf(edge).map((s, i) => (
                <li key={i} className="flex flex-wrap items-center gap-2">
                  <span>
                    {s.src_node ? shortName(s.src_node) : "?"} <span className="text-muted-foreground">{s.type}</span>{" "}
                    {s.dst_node ? shortName(s.dst_node) : "?"}
                  </span>
                  <EvidenceLink path={s.path} line={s.line} />
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </div>
  );
}

// --------------------------------------------------------------------------- ranking

const RANK_COLUMNS: { key: SortKey; title: string }[] = [
  { key: "name", title: "Módulo" },
  { key: "files", title: "Arquivos" },
  { key: "screens", title: "Telas" },
  { key: "ownedTables", title: "Tabelas próprias" },
  { key: "ownedShared", title: "Compartilhadas" },
  { key: "ca", title: "Ca" },
  { key: "ce", title: "Ce" },
  { key: "instability", title: "I" },
  { key: "coverage", title: "Cobertura" },
  { key: "score", title: "Pontuação" },
];

function Ranking({ modules, params, update }: ViewProps) {
  const real = realModules(modules);
  const maxCe = maxCeOf(modules);
  const key = (RANK_COLUMNS.some((c) => c.key === params.get("ordem")) ? params.get("ordem") : "score") as SortKey;
  const desc = params.get("asc") !== "1";
  const onlyReliable = params.get("confiaveis") === "1";
  // Módulo sem nenhum arquivo não se extrai: empataria em 100 no topo (sem tabelas nem Ce).
  const withEmpty = params.get("vazios") === "1";
  const empty = real.filter((m) => m.files === 0).length;
  const [text, setText] = useState("");
  const scores = new Map(real.map((m) => [m.module, candidateScore(m, maxCe)]));
  const wanted = text.trim().toLowerCase();
  const rows = sortModules(
    real.filter(
      (m) =>
        (withEmpty || m.files > 0) &&
        (!onlyReliable || scores.get(m.module)?.reliable) &&
        (!wanted || m.name.toLowerCase().includes(wanted)),
    ),
    key,
    desc,
    (m) => scores.get(m.module)?.score ?? 0,
  );
  const reliable = real.filter((m) => scores.get(m.module)?.reliable).length;

  return (
    <div className="space-y-3">
      <Notice>
        <p className="font-medium">Candidatos a extração (Strangler Fig)</p>
        <p className="text-xs">{SCORE_FORMULA}</p>
        <p className="text-xs">
          {reliable} de {real.length} módulos têm cobertura ≥ 70%. A pontuação é indicativa: abra a explicação de cada linha.
        </p>
      </Notice>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <input
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="Filtrar por nome"
          aria-label="Filtrar módulos por nome"
          className="h-8 min-w-48 rounded-md border border-input bg-card px-2"
        />
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={onlyReliable} onChange={(event) => update({ confiaveis: event.target.checked ? "1" : null })} />
          Só cobertura ≥ 70%
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={withEmpty} onChange={(event) => update({ vazios: event.target.checked ? "1" : null })} />
          Incluir módulos sem arquivos ({empty})
        </label>
        <Button variant="outline" size="sm" onClick={() => download(rankingCsv(rows, maxCe), "ranking-modulos.csv")}>
          <Download className="h-4 w-4" aria-hidden="true" /> Exportar CSV
        </Button>
        <span className="ml-auto text-xs text-muted-foreground">{rows.length} módulo(s)</span>
      </div>
      <Card className="overflow-auto">
        <table className="w-full text-sm" aria-label="Ranking de módulos">
          <thead className="text-left text-xs text-muted-foreground">
            <tr>
              {RANK_COLUMNS.map((c) => (
                <th key={c.key} className="px-3 py-2 font-medium" aria-sort={key === c.key ? (desc ? "descending" : "ascending") : "none"}>
                  <button
                    type="button"
                    className="hover:text-foreground"
                    onClick={() => update({ ordem: c.key === "score" ? null : c.key, asc: key === c.key && desc ? "1" : null })}
                  >
                    {c.title}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y">
            {rows.map((m) => {
              const c = scores.get(m.module)!;
              return (
                <tr key={m.module} data-testid={`ranking-${m.module}`}>
                  <td className="px-3 py-1.5">
                    <Link className="text-primary hover:underline" to={moduleHref(m.module)}>
                      {m.name}
                    </Link>
                  </td>
                  <td className="px-3 py-1.5 tabular-nums">{formatNumber(m.files)}</td>
                  <td className="px-3 py-1.5 tabular-nums">{formatNumber(m.screens)}</td>
                  <td className="px-3 py-1.5 tabular-nums">{formatNumber(m.ownedTables)}</td>
                  <td className="px-3 py-1.5 tabular-nums">{formatNumber(m.ownedShared)}</td>
                  <td className="px-3 py-1.5 tabular-nums">{m.ca}</td>
                  <td className="px-3 py-1.5 tabular-nums">{m.ce}</td>
                  <td className="px-3 py-1.5">
                    <InstabilityBadge value={m.instability} />
                  </td>
                  <td className="px-3 py-1.5 tabular-nums">
                    {m.coverage === null || m.coverage === undefined ? "—" : formatPercent(m.coverage, 1)}
                  </td>
                  <td className="px-3 py-1.5">
                    <details>
                      <summary className="cursor-pointer tabular-nums">
                        <span className="font-semibold">{c.score.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}</span>
                        {c.reliable ? null : (
                          <span className="ml-2 rounded-full border border-warning px-1.5 text-[10px] text-warning">pouco confiável</span>
                        )}
                      </summary>
                      <ul className="mt-1 space-y-0.5 text-xs text-muted-foreground">
                        {c.explanation.map((line) => (
                          <li key={line}>{line}</li>
                        ))}
                      </ul>
                    </details>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
      <p className="text-xs text-muted-foreground">
        Em “{NO_MODULE}” ficam os arquivos sem módulo; ele não entra no ranking. Veja também as{" "}
        <Link className="text-primary underline" to="/tabelas?compartilhadas=1">
          tabelas compartilhadas
        </Link>
        .
      </p>
    </div>
  );
}

