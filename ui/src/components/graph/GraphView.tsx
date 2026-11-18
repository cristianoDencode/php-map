/**
 * Vista comum do impacto, do trace e do explorador: diagrama + painel lateral +
 * legenda + lista sincronizada (a alternativa acessível ao desenho).
 */
import { ArrowDownUp, Compass, Download, FileCode2, Maximize, ScrollText, Radar } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { EvidenceLink } from "../../app/code-panel";
import {
  buildElements,
  type Graph,
  type GroupInfo,
  type GroupState,
  type Row,
  rows as buildRows,
  toCsv,
  toggleGroup,
  treeEdge,
} from "../../lib/graph";
import { keyOf, nodeHref, rulesHref, shortName } from "../../lib/ids";
import { edgeVerb, nodeKind, sourceLabel } from "../../lib/labels";
import { OPERATION_LABELS } from "../../lib/rules";
import { formatNumber } from "../../lib/utils";
import { ConfidenceBadge, LabelBadge } from "../NodeBits";
import { Button, Card } from "../ui";
import { GraphCanvas, type GraphCanvasHandle, type LayoutInfo, type LayoutName, OPERATION_TOKENS } from "./GraphCanvas";

export const LIST_PAGE = 200;

export function GraphView({
  graph,
  visible,
  matches,
  layout,
  groups,
  onGroupsChange,
  selected,
  onSelect,
  moduleName,
  toolbar,
  panelExtra,
  fileName,
  headless,
  locked,
}: {
  graph: Graph;
  visible: Set<string>;
  matches: Set<string>;
  layout: LayoutName;
  groups: GroupState | null; // null: sem agrupamento (explorador)
  onGroupsChange?: (next: GroupState, group: GroupInfo) => void;
  selected: string | null;
  onSelect: (id: string | null) => void;
  moduleName: (id: string) => string;
  toolbar?: ReactNode;
  panelExtra?: (id: string) => ReactNode;
  fileName: string;
  headless?: boolean;
  locked?: string[];
}) {
  const canvas = useRef<GraphCanvasHandle>(null);
  const [info, setInfo] = useState<LayoutInfo | null>(null);
  const built = useMemo(
    () =>
      groups
        ? buildElements(graph, visible, groups, moduleName)
        : buildElements(graph, visible, { open: [], closed: [] }, moduleName, Number.POSITIVE_INFINITY),
    [graph, visible, groups, moduleName],
  );
  const elements = useMemo(
    () =>
      groups
        ? built.elements
        : built.elements.filter((el) => el.data.kind !== "Group").map((el) => ({ ...el, data: { ...el.data, parent: undefined } })),
    [built, groups],
  );
  const listRows = useMemo(() => buildRows(graph, matches, moduleName), [graph, matches, moduleName]);
  const collapsed = built.groups.filter((g) => g.collapsed);

  function toggle(key: string) {
    const group = built.groups.find((g) => g.key === key);
    if (!group || !groups || !onGroupsChange) return;
    onGroupsChange(toggleGroup(groups, key, group.size), group);
  }

  function exportPng() {
    const uri = canvas.current?.png();
    if (!uri) return;
    download(uri, `${fileName}.png`);
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" onClick={() => canvas.current?.fit()}>
          <Maximize className="h-4 w-4" aria-hidden="true" /> Ajustar à tela
        </Button>
        <Button variant="outline" size="sm" onClick={exportPng}>
          <Download className="h-4 w-4" aria-hidden="true" /> Exportar PNG
        </Button>
        {toolbar}
        <span className="ml-auto text-xs text-muted-foreground" data-testid="desenho-info">
          {groups
            ? `${formatNumber(built.nodesBefore)} nós; ${formatNumber(built.nodesDrawn)} no desenho (${collapsed.length} grupo(s) colapsado(s)${built.compound ? "" : ", sem caixas de módulo: desenho grande"})`
            : `${formatNumber(built.nodesDrawn)} nós no desenho`}
          {info ? ` · layout em ${formatNumber(info.ms)} ms${info.worker ? " (fora da página)" : ""}` : ""}
        </span>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <Card className="relative h-[65vh] overflow-hidden" data-layout-ms={info?.ms ?? ""}>
          <GraphCanvas
            ref={canvas}
            elements={elements}
            layout={layout}
            selected={selected}
            onSelect={onSelect}
            onGroupToggle={toggle}
            onLayout={setInfo}
            headless={headless}
            locked={locked}
            label={`Diagrama com ${built.nodesDrawn} nós. A lista abaixo traz os mesmos nós.`}
          />
          <Legend />
        </Card>
        <div className="space-y-4">
          <SidePanel graph={graph} id={selected} moduleName={moduleName} extra={panelExtra} />
          {groups && built.groups.length > 0 ? (
            <Card aria-label="Grupos por módulo">
              <h2 className="border-b px-4 py-2 text-sm font-semibold">Grupos por módulo ({built.groups.length})</h2>
              <ul className="max-h-64 divide-y overflow-auto text-sm">
                {built.groups.map((g) => (
                  <li key={g.key} className="flex items-center gap-2 px-4 py-1.5">
                    <span className="min-w-0 flex-1 truncate" title={g.key}>
                      {g.name}
                    </span>
                    <span className="text-xs tabular-nums text-muted-foreground">{formatNumber(g.size)}</span>
                    <Button variant="ghost" size="sm" onClick={() => toggle(g.key)} aria-label={`${g.collapsed ? "Expandir" : "Colapsar"} ${g.name}`}>
                      {g.collapsed ? "Expandir" : "Colapsar"}
                    </Button>
                  </li>
                ))}
              </ul>
            </Card>
          ) : null}
        </div>
      </div>

      <NodeList rows={listRows} selected={selected} onSelect={onSelect} fileName={fileName} />
    </div>
  );
}

function download(href: string, name: string) {
  const link = document.createElement("a");
  link.href = href;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
}

// --------------------------------------------------------------------------- legenda

const LEGEND_KINDS = ["File", "Method", "Function", "Class", "Table", "PgFunction", "BusinessAction"];
const LEGEND_OPS = ["SELECT", "INSERT", "UPDATE", "DELETE"].map((op) => [op.toLowerCase(), OPERATION_LABELS[op] ?? op] as const);

export function Legend() {
  return (
    <div
      className="pointer-events-none absolute bottom-2 left-2 max-w-[calc(100%-1rem)] rounded-md border bg-card/90 px-3 py-2 text-[11px] shadow-sm backdrop-blur"
      aria-label="Legenda"
      role="note"
    >
      <div className="flex flex-wrap gap-x-3 gap-y-1">
        {LEGEND_KINDS.map((kind) => (
          <span key={kind} className="inline-flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: nodeKind(kind).color }} aria-hidden="true" />
            {nodeKind(kind).singular}
          </span>
        ))}
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2.5 w-4 rounded-sm border-2 border-dashed" style={{ borderColor: "var(--node-module)" }} aria-hidden="true" />
          grupo (módulo)
        </span>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
        {LEGEND_OPS.map(([op, text]) => (
          <span key={op} className="inline-flex items-center gap-1">
            <span className="inline-block h-0.5 w-5" style={{ background: `var(${OPERATION_TOKENS[op]})` }} aria-hidden="true" />
            {text}
          </span>
        ))}
        <span className="inline-flex items-center gap-1">
          <span className="inline-block w-5 border-t-2 border-dashed border-muted-foreground" aria-hidden="true" />
          inferida (confiança &lt; 1; mais fina = menor)
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded-sm border-2" style={{ borderColor: "var(--node-action)" }} aria-hidden="true" />
          tela
        </span>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- painel lateral

function linkClass() {
  return "inline-flex h-8 items-center gap-1.5 rounded-md border border-input px-3 text-sm hover:bg-muted";
}

export function SidePanel({
  graph,
  id,
  moduleName,
  extra,
}: {
  graph: Graph;
  id: string | null;
  moduleName: (id: string) => string;
  extra?: (id: string) => ReactNode;
}) {
  const node = id ? graph.nodes.find((n) => n.id === id) : null;
  if (!node) {
    return (
      <Card className="px-4 py-3 text-sm text-muted-foreground" aria-label="Nó selecionado">
        Clique num nó do diagrama ou da lista para ver o resumo e a evidência. Clique num grupo para expandir ou colapsar.
      </Card>
    );
  }
  const edge = treeEdge(graph, node.id);
  return (
    <Card className="space-y-3 px-4 py-3 text-sm" aria-label="Nó selecionado">
      <div className="space-y-1">
        <LabelBadge label={node.label} />
        <h2 className="break-all font-semibold">{shortName(node.id, node.name)}</h2>
        <p className="break-all font-mono text-xs text-muted-foreground">{keyOf(node.id)}</p>
      </div>
      <dl className="space-y-1 text-xs">
        {node.depth !== null ? (
          <div>
            <dt className="inline text-muted-foreground">Nível: </dt>
            <dd className="inline">{node.depth}</dd>
          </div>
        ) : null}
        <div>
          <dt className="inline text-muted-foreground">Módulos: </dt>
          <dd className="inline">{node.modules.length ? node.modules.map(moduleName).join(", ") : "sem módulo"}</dd>
        </div>
      </dl>
      {edge ? (
        <div className="space-y-1 rounded-md bg-muted/60 px-3 py-2 text-xs">
          <p className="font-medium">Chegou por</p>
          <p>
            {shortName(edge.src)} <strong>{edgeVerb(edge.type, false)}</strong> {shortName(edge.dst)}
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <ConfidenceBadge confidence={edge.confidence} source={edge.source} />
            <span className="text-muted-foreground">{sourceLabel(edge.source)}</span>
          </div>
          <EvidenceLink path={edge.evidence.path} line={edge.evidence.line} />
        </div>
      ) : node.id === graph.root ? (
        <p className="text-xs text-muted-foreground">Ponto de partida do diagrama.</p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Link className={linkClass()} to={nodeHref(node.id)}>
          <FileCode2 className="h-4 w-4" aria-hidden="true" /> Abrir detalhe
        </Link>
        {node.label === "File" ? (
          <Link className={linkClass()} to={rulesHref(node.id)}>
            <ScrollText className="h-4 w-4" aria-hidden="true" /> Ver regras
          </Link>
        ) : null}
        {node.label === "Table" ? (
          <Link className={linkClass()} to={`/impacto?tabela=${encodeURIComponent(keyOf(node.id))}`}>
            <Radar className="h-4 w-4" aria-hidden="true" /> Ver impacto
          </Link>
        ) : null}
        <Link className={linkClass()} to={`/explorar/${encodeURIComponent(node.id)}`}>
          <Compass className="h-4 w-4" aria-hidden="true" /> Explorar daqui
        </Link>
      </div>
      {extra ? extra(node.id) : null}
    </Card>
  );
}

// --------------------------------------------------------------------------- lista

type SortKey = "name" | "label" | "depth" | "modules" | "confidence";

const COLUMNS: { key: SortKey; title: string }[] = [
  { key: "name", title: "Nome" },
  { key: "label", title: "Tipo" },
  { key: "depth", title: "Nível" },
  { key: "modules", title: "Módulos" },
  { key: "confidence", title: "Confiança" },
];

function compare(a: Row, b: Row, key: SortKey): number {
  const va = a[key];
  const vb = b[key];
  if (va === vb) return 0;
  if (va === null) return 1;
  if (vb === null) return -1;
  return typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb), "pt-BR");
}

export function NodeList({
  rows,
  selected,
  onSelect,
  fileName,
}: {
  rows: Row[];
  selected: string | null;
  onSelect: (id: string | null) => void;
  fileName: string;
}) {
  const [text, setText] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "depth", desc: false });
  const [limit, setLimit] = useState(LIST_PAGE);
  const selectedRow = useRef<HTMLTableRowElement>(null);
  const wanted = text.trim().toLowerCase();
  const shown = useMemo(() => {
    const filtered = wanted
      ? rows.filter((r) => `${r.name} ${r.id} ${r.modules} ${r.evidence}`.toLowerCase().includes(wanted))
      : rows;
    const sorted = [...filtered].sort((a, b) => compare(a, b, sort.key) || a.id.localeCompare(b.id));
    return sort.desc ? sorted.reverse() : sorted;
  }, [rows, wanted, sort]);
  const position = selected ? shown.findIndex((r) => r.id === selected) : -1;
  const visibleRows = shown.slice(0, Math.max(limit, position + 1));

  useEffect(() => {
    selectedRow.current?.scrollIntoView?.({ block: "nearest" });
  }, [selected]);

  function exportCsv() {
    const blob = new Blob([toCsv(shown)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    download(url, `${fileName}.csv`);
    URL.revokeObjectURL(url);
    toast.success(`CSV com ${shown.length} linha(s)`);
  }

  return (
    <Card aria-label="Lista de nós">
      <header className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
        <h2 className="text-sm font-semibold">
          Lista <span className="font-normal text-muted-foreground">({formatNumber(shown.length)})</span>
        </h2>
        <input
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="Filtrar a lista"
          aria-label="Filtrar a lista"
          className="h-8 min-w-48 flex-1 rounded-md border border-input bg-card px-2 text-sm"
        />
        <Button variant="outline" size="sm" onClick={exportCsv}>
          <Download className="h-4 w-4" aria-hidden="true" /> Exportar CSV
        </Button>
      </header>
      <div className="max-h-[28rem] overflow-auto">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-card text-left text-xs text-muted-foreground">
            <tr>
              {COLUMNS.map((c) => (
                <th key={c.key} className="px-3 py-2 font-medium" aria-sort={sort.key === c.key ? (sort.desc ? "descending" : "ascending") : "none"}>
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 hover:text-foreground"
                    onClick={() => setSort({ key: c.key, desc: sort.key === c.key ? !sort.desc : false })}
                  >
                    {c.title} <ArrowDownUp className="h-3 w-3" aria-hidden="true" />
                  </button>
                </th>
              ))}
              <th className="px-3 py-2 font-medium">Evidência</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {visibleRows.map((r) => {
              const isSelected = r.id === selected;
              return (
                <tr
                  key={r.id}
                  ref={isSelected ? selectedRow : undefined}
                  aria-selected={isSelected}
                  className={isSelected ? "bg-accent text-accent-foreground" : undefined}
                >
                  <td className="max-w-md px-3 py-1.5">
                    <button type="button" className="truncate text-left hover:underline" title={r.id} onClick={() => onSelect(r.id)}>
                      {r.name}
                    </button>
                  </td>
                  <td className="px-3 py-1.5 text-xs" style={{ color: nodeKind(r.label).color }}>
                    {nodeKind(r.label).singular}
                  </td>
                  <td className="px-3 py-1.5 tabular-nums">{r.depth ?? "—"}</td>
                  <td className="max-w-48 truncate px-3 py-1.5 text-xs text-muted-foreground" title={r.modules}>
                    {r.modules || "—"}
                  </td>
                  <td className="px-3 py-1.5 text-xs tabular-nums">
                    {r.confidence === null ? "—" : r.confidence.toLocaleString("pt-BR", { maximumFractionDigits: 2 })}
                  </td>
                  <td className="px-3 py-1.5">{r.evidencePath ? <EvidenceLink path={r.evidencePath} line={r.evidenceLine} /> : null}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {shown.length > visibleRows.length ? (
        <div className="border-t px-4 py-2">
          <Button variant="ghost" size="sm" onClick={() => setLimit(limit + LIST_PAGE)}>
            Mostrar mais ({formatNumber(shown.length - visibleRows.length)} restantes)
          </Button>
        </div>
      ) : null}
    </Card>
  );
}
