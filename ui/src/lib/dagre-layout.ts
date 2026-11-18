/**
 * Layout hierárquico (`dagre`) sem o Cytoscape, para rodar num web worker.
 *
 * Medido no e-cidade real: com 1.201 nós (trace do piloto, tudo aberto) o `dagre` leva ~4 s, e
 * com 2.000 (impacto de `empempenho`) ~1,2 s. Na thread da página isso trava a aba; no worker a
 * página continua respondendo e o desenho aparece quando as posições chegam.
 */
import dagre from "@dagrejs/dagre";

export type DagreInput = {
  nodes: { id: string; width: number; height: number }[];
  edges: { source: string; target: string }[];
  rankdir: "LR" | "RL";
  nodesep: number;
  ranksep: number;
};

export type Positions = Record<string, { x: number; y: number }>;

export function dagrePositions(input: DagreInput): Positions {
  // Grafo simples: uma aresta por sentido basta para o layout. Arestas paralelas (o mesmo par
  // com `REQUIRES` e `INSTANTIATES`) num multigrafo fazem o `dagre` lançar "Not possible to find
  // intersection inside of the rectangle" (achado no impacto real de `empempenho`).
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: input.rankdir, nodesep: input.nodesep, ranksep: input.ranksep });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of input.nodes) g.setNode(n.id, { width: n.width, height: n.height });
  for (const e of input.edges) {
    if (e.source !== e.target && g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
  }
  dagre.layout(g);
  const positions: Positions = {};
  for (const id of g.nodes()) {
    const node = g.node(id);
    positions[id] = { x: node.x ?? 0, y: node.y ?? 0 };
  }
  return positions;
}
