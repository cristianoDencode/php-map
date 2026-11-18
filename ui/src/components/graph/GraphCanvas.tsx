/**
 * Cytoscape.js para o impacto, o trace e o explorador.
 *
 * Recebe os elementos já prontos (`lib/graph.ts`), desenha com `dagre` (hierárquico) ou
 * `fcose` (explorador) e avisa a seleção. As cores vêm dos tokens CSS (`--node-*`), lidas de
 * novo quando o tema muda: o Cytoscape não entende `var()`.
 */
import cytoscape, { type Core, type ElementDefinition, type LayoutOptions, type NodeSingular, type StylesheetJson } from "cytoscape";
import dagre from "cytoscape-dagre";
import fcose from "cytoscape-fcose";
import { type Ref, useEffect, useImperativeHandle, useRef, useState } from "react";
import { useTheme } from "../../app/theme";
import type { DagreInput, Positions } from "../../lib/dagre-layout";
import { GROUP_PREFIX, LARGE_DRAWING } from "../../lib/graph";
import { cn } from "../../lib/utils";

let registered = false;
function register() {
  if (registered) return;
  cytoscape.use(dagre);
  cytoscape.use(fcose);
  registered = true;
}

export type LayoutName = "dagre-lr" | "dagre-rl" | "fcose";

export type GraphCanvasHandle = {
  /** PNG (data URL) do desenho inteiro, com o fundo do tema. */
  png: () => string | null;
  fit: () => void;
  core: () => Core | null;
};

/** `worker`: o `dagre` rodou fora da página (desenho grande). */
export type LayoutInfo = { ms: number; nodes: number; edges: number; worker: boolean };

type WorkerReply = { id: number; positions: Positions; ms: number };

function workerInput(core: Core, layout: LayoutName): DagreInput {
  return {
    rankdir: layout === "dagre-lr" ? "LR" : "RL",
    nodesep: 12,
    ranksep: 90,
    nodes: core.nodes().map((n) => {
      const size = n.layoutDimensions({ nodeDimensionsIncludeLabels: false });
      return { id: n.id(), width: size.w, height: size.h };
    }),
    edges: core.edges().map((e) => ({ source: e.source().id(), target: e.target().id() })),
  };
}

const FALLBACK: Record<string, string> = {
  "--node-file": "#2563eb",
  "--node-table": "#047857",
  "--node-function": "#7c3aed",
  "--node-method": "#9333ea",
  "--node-class": "#a21caf",
  "--node-pgfunction": "#b45309",
  "--node-module": "#0e7490",
  "--node-action": "#be123c",
  "--node-other": "#475569",
  "--foreground": "#0f172a",
  "--muted-foreground": "#475569",
  "--muted": "#f1f5f9",
  "--border": "#e2e8f0",
  "--card": "#ffffff",
  "--primary": "#1d4ed8",
  "--confidence-certa": "#047857",
  "--confidence-media": "#b45309",
  "--destructive": "#b91c1c",
  "--confidence-alta": "#1d4ed8",
};

function token(name: string): string {
  const value = typeof window === "undefined" ? "" : getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || FALLBACK[name] || "#64748b";
}

const KIND_TOKEN: Record<string, string> = {
  File: "--node-file",
  Table: "--node-table",
  Function: "--node-function",
  Method: "--node-method",
  Class: "--node-class",
  PgFunction: "--node-pgfunction",
  RpcCall: "--node-pgfunction",
  Module: "--node-module",
  BusinessAction: "--node-action",
};

/** Cor por operação na aresta: lê, insere, altera, apaga (a mesma da legenda). */
export const OPERATION_TOKENS: Record<string, string> = {
  select: "--confidence-alta",
  insert: "--confidence-certa",
  update: "--confidence-media",
  delete: "--destructive",
};

export function graphStyle(): StylesheetJson {
  const kinds = Object.entries(KIND_TOKEN).map(([kind, name]) => ({
    selector: `node.kind-${kind}`,
    style: { "background-color": token(name), "border-color": token(name) },
  }));
  return [
    {
      selector: "node",
      style: {
        label: "data(label)",
        "font-size": 10,
        color: token("--foreground"),
        "text-valign": "bottom",
        "text-halign": "center",
        "text-margin-y": 4,
        "text-wrap": "ellipsis",
        "text-max-width": "160px",
        width: 18,
        height: 18,
        "background-color": token("--node-other"),
      },
    },
    ...kinds,
    { selector: "node.kind-Table", style: { shape: "barrel", width: 24, height: 22 } },
    { selector: "node.kind-PgFunction, node.kind-RpcCall", style: { shape: "diamond" } },
    { selector: "node.kind-File", style: { shape: "round-rectangle", width: 22, height: 16 } },
    { selector: "node.kind-Class", style: { shape: "hexagon" } },
    { selector: "node.screen", style: { "border-width": 3, "border-color": token("--node-action") } },
    {
      selector: "node.root",
      style: { width: 32, height: 32, "border-width": 4, "border-color": token("--foreground"), "font-weight": "bold", "font-size": 12 },
    },
    {
      selector: "node.group",
      style: {
        shape: "round-rectangle",
        "background-color": token("--muted"),
        "background-opacity": 0.5,
        "border-width": 1,
        "border-color": token("--border"),
        "border-style": "dashed",
        label: "data(label)",
        "text-valign": "top",
        "text-halign": "center",
        "font-size": 11,
        "font-weight": "bold",
        color: token("--muted-foreground"),
        padding: "12px",
      },
    },
    {
      selector: "node.group.collapsed",
      style: {
        width: 150,
        height: 44,
        "text-valign": "center",
        "text-wrap": "wrap",
        "text-max-width": "140px",
        "border-style": "solid",
        "border-width": 2,
        "border-color": token("--node-module"),
        "background-color": token("--card"),
        "background-opacity": 1,
        color: token("--foreground"),
        "font-size": 10,
      },
    },
    {
      selector: "edge",
      style: {
        width: "data(width)",
        "line-color": token("--muted-foreground"),
        "target-arrow-color": token("--muted-foreground"),
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.8,
        "curve-style": "bezier",
        opacity: 0.8,
      },
    },
    ...Object.entries(OPERATION_TOKENS).map(([op, name]) => ({
      selector: `edge.op-${op}`,
      style: { "line-color": token(name), "target-arrow-color": token(name) },
    })),
    { selector: "edge.inferred", style: { "line-style": "dashed" } },
    { selector: "edge.aggregated", style: { label: "data(count)", "font-size": 9, color: token("--muted-foreground") } },
    {
      selector: ":selected",
      style: { "overlay-color": token("--primary"), "overlay-opacity": 0.25, "overlay-padding": 6 },
    },
    { selector: ".faded", style: { opacity: 0.25 } },
  ] as StylesheetJson;
}

function layoutOptions(name: LayoutName, incremental: boolean): LayoutOptions {
  if (name === "fcose") {
    return {
      name: "fcose",
      animate: false,
      randomize: !incremental, // ao expandir, os nós que já estavam ficam perto de onde estavam
      nodeRepulsion: () => 8000,
      idealEdgeLength: () => 90,
      fit: !incremental,
      padding: 30,
    } as LayoutOptions;
  }
  return {
    name: "dagre",
    rankDir: name === "dagre-lr" ? "LR" : "RL",
    nodeSep: 12,
    rankSep: 90,
    fit: true,
    padding: 30,
    animate: false,
  } as LayoutOptions;
}

/**
 * Troca os elementos por diferença: o que continua (mesmo ID e mesmo pai) mantém a posição e
 * só recebe os dados e as classes novos. Devolve os IDs dos nós acrescentados.
 */
function updateElements(core: Core, elements: ElementDefinition[]): string[] {
  const wanted = new Map(elements.map((el) => [String(el.data.id), el]));
  const added: string[] = [];
  core.batch(() => {
    core.elements().forEach((el) => {
      const next = wanted.get(el.id());
      const sameParent = !el.isNode() || (el.data("parent") ?? null) === (next?.data.parent ?? null);
      if (!next || !sameParent) el.remove();
    });
    const fresh: ElementDefinition[] = [];
    for (const [id, el] of wanted) {
      const current = core.getElementById(id);
      if (current.nonempty()) {
        current.data(el.data);
        current.classes(el.classes ?? "");
      } else {
        fresh.push(el);
        if (el.group !== "edges") added.push(id);
      }
    }
    // Nós antes das arestas: a aresta precisa das duas pontas.
    core.add(fresh.filter((el) => el.group !== "edges"));
    core.add(fresh.filter((el) => el.group === "edges"));
  });
  return added;
}

/** Nó novo começa ao lado de um vizinho que já tinha posição (o layout incremental parte daí). */
function placeNearNeighbors(core: Core, added: string[]) {
  const fresh = new Set(added);
  for (const id of added) {
    const node = core.getElementById(id) as NodeSingular;
    const anchor = node.neighborhood().nodes().filter((n) => !fresh.has(n.id())).first() as NodeSingular;
    if (anchor.nonempty()) {
      const p = anchor.position();
      node.position({ x: p.x + (Math.random() - 0.5) * 80, y: p.y + (Math.random() - 0.5) * 80 });
    }
  }
}

export function GraphCanvas({
  elements,
  layout,
  selected,
  onSelect,
  onGroupToggle,
  onLayout,
  locked,
  extraStyle,
  headless = import.meta.env.MODE === "test",
  className,
  label,
  ref,
}: {
  elements: ElementDefinition[];
  layout: LayoutName;
  selected?: string | null;
  onSelect?: (id: string | null) => void;
  onGroupToggle?: (key: string) => void;
  onLayout?: (info: LayoutInfo) => void;
  /** Nós fixados: o layout não os move (explorador). */
  locked?: string[];
  /** Estilo a mais, depois do padrão (o mapa de módulos usa cor e tamanho dos dados). */
  extraStyle?: (tokenOf: (name: string) => string) => StylesheetJson;
  /** Sem desenho (padrão nos testes, jsdom não tem canvas): classes e seleção funcionam. */
  headless?: boolean;
  className?: string;
  label: string;
  ref?: Ref<GraphCanvasHandle>;
}) {
  const container = useRef<HTMLDivElement>(null);
  const cy = useRef<Core | null>(null);
  const handlers = useRef({ onSelect, onGroupToggle, onLayout });
  const extra = useRef(extraStyle);
  useEffect(() => {
    extra.current = extraStyle;
  });
  const fullStyle = () => [...graphStyle(), ...(extra.current?.(token) ?? [])] as StylesheetJson;
  useEffect(() => {
    handlers.current = { onSelect, onGroupToggle, onLayout };
  });
  const { theme } = useTheme();
  const worker = useRef<Worker | null>(null);
  const request = useRef(0);
  const [computing, setComputing] = useState<number | null>(null); // nós no layout em andamento
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    register();
    const core = cytoscape({
      container: headless ? undefined : container.current,
      headless,
      styleEnabled: true,
      style: fullStyle(),
      minZoom: 0.05,
      maxZoom: 3,
      wheelSensitivity: 0.3,
      boxSelectionEnabled: false,
    });
    core.on("tap", "node", (event) => {
      const node = event.target;
      const id = String(node.id());
      if (id.startsWith(GROUP_PREFIX)) {
        handlers.current.onGroupToggle?.(String(node.data("groupKey")));
        return;
      }
      handlers.current.onSelect?.(id);
    });
    core.on("tap", (event) => {
      if (event.target === core) handlers.current.onSelect?.(null);
    });
    cy.current = core;
    return () => {
      core.destroy();
      cy.current = null;
    };
  }, [headless]);

  useEffect(() => {
    const core = cy.current;
    if (!core) return;
    const started = performance.now();
    const hadNodes = core.nodes().length > 0;
    const added = updateElements(core, elements);
    core.nodes().unlock();
    for (const id of locked ?? []) core.getElementById(id).lock();
    const incremental = layout === "fcose" && hadNodes && added.length < core.nodes().length;
    const report = (worker: boolean) =>
      handlers.current.onLayout?.({
        ms: Math.round(performance.now() - started),
        nodes: core.nodes().length,
        edges: core.edges().length,
        worker,
      });
    const id = ++request.current;
    const large = layout !== "fcose" && core.nodes().length > LARGE_DRAWING && typeof Worker !== "undefined";
    if (headless || core.nodes().length === 0) {
      report(false);
    } else if (large) {
      // Desenho grande: o `dagre` roda no worker e as posições entram por `preset`.
      worker.current ??= new Worker(new URL("../../lib/dagre.worker.ts", import.meta.url), { type: "module" });
      const current = worker.current;
      setComputing(core.nodes().length);
      setFailure(null);
      current.onmessage = (event: MessageEvent<WorkerReply>) => {
        if (event.data.id !== request.current || cy.current !== core) return; // resposta antiga
        core.layout({ name: "preset", positions: event.data.positions, fit: true, padding: 30 } as LayoutOptions).run();
        setComputing(null);
        report(true);
      };
      current.onerror = (event: ErrorEvent) => {
        event.preventDefault();
        if (cy.current !== core) return;
        // Sem o `dagre`, um layout em camadas simples: o desenho aparece e a página avisa.
        core.layout({ name: "breadthfirst", directed: true, fit: true, padding: 30, animate: false } as LayoutOptions).run();
        setComputing(null);
        setFailure(event.message || "erro no layout");
        report(true);
      };
      current.postMessage({ id, input: workerInput(core, layout) });
    } else {
      setComputing(null);
      setFailure(null);
      if (incremental) placeNearNeighbors(core, added);
      core.layout(layoutOptions(layout, incremental)).run();
      report(false);
    }
  }, [elements, layout, headless, locked]);

  useEffect(() => () => worker.current?.terminate(), []);

  useEffect(() => {
    cy.current?.style(fullStyle());
  }, [theme]);

  useEffect(() => {
    const core = cy.current;
    if (!core) return;
    core.$(":selected").unselect();
    if (!selected) return;
    const node = core.getElementById(selected);
    if (node.nonempty()) {
      node.select();
      if (!headless) core.animate({ center: { eles: node } }, { duration: 200 });
    }
  }, [selected, elements, headless]);

  useImperativeHandle(
    ref,
    () => ({
      png: () => cy.current?.png({ full: true, scale: 2, bg: token("--card"), output: "base64uri" }) ?? null,
      fit: () => cy.current?.fit(undefined, 30),
      core: () => cy.current,
    }),
    [],
  );

  return (
    <>
      {computing !== null ? (
        <div role="status" className="absolute inset-x-0 top-0 z-10 bg-warning-bg px-4 py-1.5 text-xs text-warning">
          Calculando o layout de {computing.toLocaleString("pt-BR")} nós fora da página (o desenho aparece em seguida)…
        </div>
      ) : null}
      {failure ? (
        <div role="alert" className="absolute inset-x-0 top-0 z-10 bg-warning-bg px-4 py-1.5 text-xs text-warning">
          O layout hierárquico falhou ({failure}); o desenho usa camadas simples.
        </div>
      ) : null}
      <div
      ref={container}
      role="img"
      aria-label={label}
      className={cn("h-full w-full bg-card", className)}
      data-testid="graph-canvas"
      />
    </>
  );
}
