/**
 * Diagramas: o subgrafo da API vira elementos do Cytoscape. Funções puras,
 * testadas sem o Cytoscape.
 *
 * - Filtros de operação, módulo e "só telas" são do lado do cliente. O nó que passa leva junto
 *   o caminho até a raiz (`parentOf`), para o desenho continuar ligado.
 * - Agrupamento por módulo é só visual (nó composto): o Cytoscape não aceita aresta do pai
 *   para os próprios filhos. Grupo com mais de `COLLAPSE_LIMIT` filhos começa colapsado num
 *   nó "N arquivos"; as arestas dos filhos passam a apontar para ele.
 */
import type { ElementDefinition } from "cytoscape";
import { keyOf, shortName } from "./ids";
import { nodeKind } from "./labels";

export const COLLAPSE_LIMIT = 30;
/**
 * Acima disto o desenho é "grande": sem caixas de módulo (o `dagre` com nós compostos não
 * termina com ~2.000 nós) e com o layout num web worker (`GraphCanvas`).
 */
export const LARGE_DRAWING = 250;
export const NO_MODULE = "(sem módulo)";
export const GROUP_PREFIX = "group:";

export const TABLE_OPERATIONS: Record<string, string> = {
  QUERIES: "SELECT",
  INSERTS: "INSERT",
  UPDATES: "UPDATE",
  DELETES: "DELETE",
};
export const OPERATIONS = ["SELECT", "INSERT", "UPDATE", "DELETE"] as const;
export type Operation = (typeof OPERATIONS)[number];

export type GNode = {
  id: string;
  label: string;
  name?: string | null;
  path?: string | null;
  modules: string[];
  attrs: Record<string, unknown>;
  depth: number | null;
};

export type GEdge = {
  src: string;
  dst: string;
  type: string;
  source: string;
  confidence: number;
  evidence: Record<string, unknown>;
  occurrences?: number;
};

/** `reverse`: impacto (as arestas apontam para a raiz); `false`: trace e explorador. */
export type Graph = {
  root: string;
  nodes: GNode[];
  edges: GEdge[];
  parentOf: Record<string, string>;
  reverse: boolean;
};

type ApiSubgraph = {
  root: string;
  nodes: { id: string; label: string; name?: string | null; path?: string | null; modules: string[]; attrs: unknown }[];
  edges: {
    src: string;
    dst: string;
    type: string;
    source: string;
    confidence: number;
    evidence: unknown;
    occurrences?: number;
  }[];
  depthOf: unknown;
  parentOf: unknown;
};

export function fromSubgraph(sub: ApiSubgraph, reverse: boolean): Graph {
  const depthOf = (sub.depthOf ?? {}) as Record<string, number>;
  return {
    root: sub.root,
    reverse,
    parentOf: (sub.parentOf ?? {}) as Record<string, string>,
    nodes: sub.nodes.map((n) => ({
      id: n.id,
      label: n.label,
      name: n.name,
      path: n.path,
      modules: n.modules,
      attrs: (n.attrs ?? {}) as Record<string, unknown>,
      depth: depthOf[n.id] ?? null,
    })),
    edges: sub.edges.map((e) => ({
      src: e.src,
      dst: e.dst,
      type: e.type,
      source: e.source,
      confidence: e.confidence,
      evidence: (e.evidence ?? {}) as Record<string, unknown>,
      occurrences: e.occurrences,
    })),
  };
}

export function isScreen(node: GNode): boolean {
  return node.label === "File" && typeof node.attrs.module_hint === "string" && node.attrs.module_hint !== "";
}

function firstModule(modules: string[]): string | undefined {
  return [...modules].sort()[0];
}

/**
 * Grupo (visual) de cada nó: o 1º módulo dele ou, sem módulo (método, classe), o do arquivo que
 * o declara, quando esse arquivo está no subgrafo. O filtro de módulo continua olhando só o nó.
 */
export function groupKeys(graph: Graph): Map<string, string> {
  const fileModule = new Map<string, string>();
  for (const n of graph.nodes) {
    const module = firstModule(n.modules);
    if (n.label === "File" && module && n.path) fileModule.set(n.path, module);
  }
  const keys = new Map<string, string>();
  for (const n of graph.nodes) {
    const own = firstModule(n.modules);
    const declared = typeof n.attrs.path === "string" ? fileModule.get(n.attrs.path) : undefined;
    keys.set(n.id, own ?? declared ?? NO_MODULE);
  }
  return keys;
}

/** Nós do caminho até a raiz (sem o próprio nó). */
export function ancestors(graph: Graph, id: string): string[] {
  const chain: string[] = [];
  const seen = new Set([id]);
  let current = graph.parentOf[id];
  while (current && !seen.has(current)) {
    chain.push(current);
    seen.add(current);
    current = graph.parentOf[current];
  }
  return chain;
}

/** A aresta que trouxe o nó para a busca (a da árvore da BFS). */
export function treeEdge(graph: Graph, id: string): GEdge | null {
  const parent = graph.parentOf[id];
  if (!parent) return null;
  const [src, dst] = graph.reverse ? [id, parent] : [parent, id];
  const best = (list: GEdge[]) => [...list].sort((a, b) => b.confidence - a.confidence)[0] ?? null;
  // No sentido da busca; no explorador, o vizinho de entrada tem a aresta ao contrário.
  return (
    best(graph.edges.filter((e) => e.src === src && e.dst === dst)) ??
    best(graph.edges.filter((e) => e.src === dst && e.dst === src))
  );
}

/** Operações das arestas de tabela que chegam em `table` (trace) ou saem de `node` para a raiz
 * (impacto). */
export function operationsOf(graph: Graph, id: string): Set<Operation> {
  const ops = new Set<Operation>();
  for (const e of graph.edges) {
    const op = TABLE_OPERATIONS[e.type] as Operation | undefined;
    if (!op) continue;
    if (graph.reverse ? e.src === id && e.dst === graph.root : e.dst === id) ops.add(op);
  }
  return ops;
}

/** Quem toca a raiz diretamente, por operação (impacto), ou as tabelas por operação (trace). */
export function countByOperation(graph: Graph): Record<Operation, number> {
  const sets: Record<Operation, Set<string>> = {
    SELECT: new Set(),
    INSERT: new Set(),
    UPDATE: new Set(),
    DELETE: new Set(),
  };
  for (const e of graph.edges) {
    const op = TABLE_OPERATIONS[e.type] as Operation | undefined;
    if (!op) continue;
    if (graph.reverse) {
      if (e.dst === graph.root) sets[op].add(e.src);
    } else sets[op].add(e.dst);
  }
  return {
    SELECT: sets.SELECT.size,
    INSERT: sets.INSERT.size,
    UPDATE: sets.UPDATE.size,
    DELETE: sets.DELETE.size,
  };
}

export type ClientFilters = {
  ops: Operation[]; // vazio = todas
  module: string | null;
  onlyScreens: boolean;
};

export const NO_CLIENT_FILTERS: ClientFilters = { ops: [], module: null, onlyScreens: false };

export function hasClientFilters(filters: ClientFilters): boolean {
  return filters.ops.length > 0 || filters.module !== null || filters.onlyScreens;
}

/**
 * `matches`: os nós que passam nos filtros (a lista mostra só eles). `visible`: os mesmos, mais
 * o caminho de cada um até a raiz (o diagrama).
 */
export function filterGraph(graph: Graph, filters: ClientFilters): { matches: Set<string>; visible: Set<string> } {
  const all = graph.nodes.map((n) => n.id);
  if (!hasClientFilters(filters)) return { matches: new Set(all), visible: new Set(all) };
  const wanted = new Set(filters.ops);
  const firstLevel = new Map<string, Set<Operation>>();
  const opsOfFirstLevel = (id: string) => {
    let ops = firstLevel.get(id);
    if (!ops) {
      ops = operationsOf(graph, id);
      firstLevel.set(id, ops);
    }
    return ops;
  };
  const matches = new Set<string>();
  for (const node of graph.nodes) {
    if (node.id === graph.root) continue;
    if (filters.module !== null && !node.modules.includes(filters.module)) continue;
    if (filters.onlyScreens && !isScreen(node)) continue;
    if (wanted.size > 0) {
      let ops: Set<Operation>;
      if (graph.reverse) {
        // Impacto: a operação é a do nó do 1º nível (quem toca a tabela) no caminho dele.
        const chain = [node.id, ...ancestors(graph, node.id)];
        const first = chain.find((id) => graph.parentOf[id] === graph.root);
        ops = first ? opsOfFirstLevel(first) : new Set();
      } else {
        // Trace: a operação filtra as tabelas; o código passa.
        ops = node.label === "Table" ? operationsOf(graph, node.id) : new Set(wanted);
      }
      if (![...ops].some((op) => wanted.has(op))) continue;
    }
    matches.add(node.id);
  }
  const visible = new Set<string>([graph.root]);
  for (const id of matches) {
    visible.add(id);
    for (const up of ancestors(graph, id)) visible.add(up);
  }
  return { matches, visible };
}

// --------------------------------------------------------------------------- grupos

export type GroupState = { open: string[]; closed: string[] };
export const NO_GROUP_STATE: GroupState = { open: [], closed: [] };

export type GroupInfo = { key: string; name: string; size: number; collapsed: boolean; counts: Record<string, number> };

export function isCollapsed(key: string, size: number, state: GroupState, limit = COLLAPSE_LIMIT): boolean {
  return size > limit ? !state.open.includes(key) : state.closed.includes(key);
}

/** Inverte o estado do grupo, guardando só o que difere do padrão. */
export function toggleGroup(state: GroupState, key: string, size: number, limit = COLLAPSE_LIMIT): GroupState {
  const without = (list: string[]) => list.filter((k) => k !== key);
  if (size > limit) {
    return state.open.includes(key)
      ? { ...state, open: without(state.open) }
      : { ...state, open: [...state.open, key] };
  }
  return state.closed.includes(key)
    ? { ...state, closed: without(state.closed) }
    : { ...state, closed: [...state.closed, key] };
}

function countsText(counts: Record<string, number>): string {
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .map(([label, n]) => `${n.toLocaleString("pt-BR")} ${(n === 1 ? nodeKind(label).singular : nodeKind(label).plural).toLowerCase()}`)
    .join(" · ");
}

export function displayName(node: GNode): string {
  if (node.label === "BusinessAction" && typeof node.attrs.menu_path === "string") return node.attrs.menu_path;
  return shortName(node.id, node.name);
}

export type Built = {
  elements: ElementDefinition[];
  groups: GroupInfo[];
  /** Nós visíveis antes de colapsar e elementos-nó desenhados depois. */
  nodesBefore: number;
  nodesDrawn: number;
  /** Grupos expandidos desenhados como caixa (só em desenhos até `LARGE_DRAWING`). */
  compound: boolean;
};

/** Elementos do Cytoscape: classes por tipo (`kind-File`), por confiança (`inferred`) e por
 * operação (`op-insert`), grupos por módulo colapsados ou compostos. */
export function buildElements(
  graph: Graph,
  visible: Set<string>,
  state: GroupState,
  moduleName: (id: string) => string = (id) => (id === NO_MODULE ? NO_MODULE : keyOf(id)),
  limit = COLLAPSE_LIMIT,
): Built {
  const nodes = graph.nodes.filter((n) => visible.has(n.id));
  const keyOfNode = groupKeys(graph);
  const members = new Map<string, GNode[]>();
  for (const node of nodes) {
    if (node.id === graph.root) continue;
    const key = keyOfNode.get(node.id) ?? NO_MODULE;
    const list = members.get(key) ?? [];
    list.push(node);
    members.set(key, list);
  }
  const groups: GroupInfo[] = [...members.entries()]
    .map(([key, list]) => {
      const counts: Record<string, number> = {};
      for (const n of list) counts[n.label] = (counts[n.label] ?? 0) + 1;
      return { key, name: moduleName(key), size: list.length, collapsed: isCollapsed(key, list.length, state, limit), counts };
    })
    .sort((a, b) => b.size - a.size || a.name.localeCompare(b.name, "pt-BR"));
  const collapsedOf = new Map(groups.map((g) => [g.key, g.collapsed]));
  const rep = new Map<string, string>(); // nó → elemento que o representa
  const elements: ElementDefinition[] = [];

  for (const g of groups) {
    const id = `${GROUP_PREFIX}${g.key}`;
    if (g.collapsed) {
      elements.push({
        group: "nodes",
        data: { id, label: `${g.name}\n${countsText(g.counts)}`, kind: "Group", groupKey: g.key, size: g.size },
        classes: "group collapsed",
      });
    } else {
      elements.push({ group: "nodes", data: { id, label: g.name, kind: "Group", groupKey: g.key, size: g.size }, classes: "group" });
    }
  }
  let drawn = groups.length;
  for (const node of nodes) {
    const key = node.id === graph.root ? null : (keyOfNode.get(node.id) ?? NO_MODULE);
    if (key !== null && collapsedOf.get(key)) {
      rep.set(node.id, `${GROUP_PREFIX}${key}`);
      continue;
    }
    rep.set(node.id, node.id);
    drawn += 1;
    const classes = ["node", `kind-${node.label}`];
    if (node.id === graph.root) classes.push("root");
    if (isScreen(node)) classes.push("screen");
    elements.push({
      group: "nodes",
      data: {
        id: node.id,
        label: displayName(node),
        kind: node.label,
        depth: node.depth,
        ...(key !== null ? { parent: `${GROUP_PREFIX}${key}` } : {}),
      },
      classes: classes.join(" "),
    });
  }

  const merged = new Map<string, { src: string; dst: string; type: string; confidence: number; count: number }>();
  for (const e of graph.edges) {
    const src = rep.get(e.src);
    const dst = rep.get(e.dst);
    if (!src || !dst || src === dst) continue;
    const id = `${src}->${dst}:${e.type}`;
    const current = merged.get(id);
    if (current) {
      current.confidence = Math.max(current.confidence, e.confidence);
      current.count += 1;
    } else merged.set(id, { src, dst, type: e.type, confidence: e.confidence, count: 1 });
  }
  for (const [id, e] of merged) {
    const classes = ["edge", `type-${e.type}`];
    const op = TABLE_OPERATIONS[e.type];
    if (op) classes.push(`op-${op.toLowerCase()}`);
    if (e.confidence < 1) classes.push("inferred");
    if (e.count > 1) classes.push("aggregated");
    elements.push({
      group: "edges",
      data: {
        id,
        source: e.src,
        target: e.dst,
        type: e.type,
        confidence: e.confidence,
        count: e.count,
        width: 1 + 3 * e.confidence,
      },
      classes: classes.join(" "),
    });
  }
  const expandedGroups = groups.filter((g) => !g.collapsed).length;
  const compound = drawn - expandedGroups <= LARGE_DRAWING;
  if (!compound) {
    // Desenho grande: os filhos ficam soltos e as caixas dos grupos expandidos saem.
    const flat = elements
      .filter((el) => !(el.group === "nodes" && el.data.kind === "Group" && !el.classes?.includes("collapsed")))
      .map((el) => (el.data.parent ? { ...el, data: { ...el.data, parent: undefined } } : el));
    return { elements: flat, groups, nodesBefore: nodes.length, nodesDrawn: drawn - expandedGroups, compound };
  }
  // Grupo expandido sem filhos visíveis não é desenhado (só acontece com filtros).
  const used = new Set(elements.filter((el) => el.group === "nodes").map((el) => el.data.parent as string | undefined));
  const final = elements.filter(
    (el) => !(el.group === "nodes" && el.data.kind === "Group" && !el.classes?.includes("collapsed") && !used.has(el.data.id)),
  );
  return { elements: final, groups, nodesBefore: nodes.length, nodesDrawn: drawn, compound };
}

// --------------------------------------------------------------------------- lista e CSV

export type Row = {
  id: string;
  label: string;
  name: string;
  depth: number | null;
  modules: string;
  via: string;
  confidence: number | null;
  evidence: string;
  evidencePath: string | null;
  evidenceLine: number | null;
};

export function rows(graph: Graph, ids: Set<string>, moduleName: (id: string) => string): Row[] {
  return graph.nodes
    .filter((n) => ids.has(n.id))
    .map((n) => {
      const edge = treeEdge(graph, n.id);
      const path = typeof edge?.evidence.path === "string" ? edge.evidence.path : null;
      const lineNumber = typeof edge?.evidence.line === "number" ? edge.evidence.line : null;
      return {
        id: n.id,
        label: n.label,
        name: displayName(n),
        depth: n.depth,
        modules: n.modules.map(moduleName).join(", "),
        via: edge?.type ?? "",
        confidence: edge?.confidence ?? null,
        evidence: path ? `${path}${lineNumber ? `:${lineNumber}` : ""}` : "",
        evidencePath: path,
        evidenceLine: lineNumber,
      };
    });
}

export const BOM = String.fromCharCode(0xfeff); // o Excel lê o CSV como UTF-8

export const CSV_COLUMNS = ["tipo", "id", "nome", "nivel", "modulos", "chegou_por", "confianca", "evidencia"] as const;

export function csvCell(value: string | number | null): string {
  const text = value === null ? "" : String(value);
  return /[";\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** CSV com `;` (o Excel em português abre direto) e BOM para os acentos. */
export function toCsv(list: Row[]): string {
  const lines = [CSV_COLUMNS.join(";")];
  for (const r of list) {
    lines.push(
      [nodeKind(r.label).singular, r.id, r.name, r.depth, r.modules, r.via, r.confidence === null ? "" : r.confidence.toFixed(2).replace(".", ","), r.evidence]
        .map(csvCell)
        .join(";"),
    );
  }
  return `${BOM}${lines.join("\r\n")}\r\n`;
}

// --------------------------------------------------------------------------- URL

export function parseList(value: string | null): string[] {
  return value ? value.split("|").filter(Boolean) : [];
}

export function formatList(values: string[]): string | null {
  return values.length ? values.join("|") : null;
}
