/** Parte comum das páginas de impacto e de trace. */
import { ClipboardCopy, TriangleAlert } from "lucide-react";
import { type ReactNode, useCallback, useMemo } from "react";
import { useSearchParams } from "react-router";
import { toast } from "sonner";
import { FilterBar } from "../components/graph/Controls";
import type { LayoutName } from "../components/graph/GraphCanvas";
import { GraphView } from "../components/graph/GraphView";
import { Button, Card, Notice } from "../components/ui";
import { request } from "../lib/api";
import { filterGraph, type Graph, type GroupState } from "../lib/graph";
import { groupParams, readView, resetView, type ViewParams, withParams } from "../lib/graph-url";
import { MermaidDocument, useModuleNames } from "../lib/subgraph";
import { formatNumber } from "../lib/utils";

/**
 * Vista na URL e a função que a altera. Filtro da API zera grupos e seleção. `replace` troca a
 * URL sem nova entrada no histórico (a tabela resolvida pela API no impacto).
 */
export function useView(defaultDepth: number) {
  const [params, setParams] = useSearchParams();
  const view = useMemo(() => readView(params, defaultDepth), [params, defaultDepth]);
  const update = useCallback(
    (changes: Record<string, string | null>, server = false, replace = false) =>
      setParams((current) => withParams(current, server ? { ...changes, ...resetView() } : changes), {
        replace,
      }),
    [setParams],
  );
  return { params, view, update };
}

export function Metric({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <Card className="px-4 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-0.5 text-xl font-semibold tabular-nums">{value}</p>
      {hint ? <p className="mt-0.5 text-xs text-muted-foreground">{hint}</p> : null}
    </Card>
  );
}

export function DiagramBody({
  graph,
  view,
  update,
  layout,
  kind,
  mermaidRoot,
  truncated,
  fileName,
  showPgFunctions,
  headless,
}: {
  graph: Graph;
  view: ViewParams;
  update: (changes: Record<string, string | null>, server?: boolean) => void;
  layout: LayoutName;
  kind: "trace" | "impact";
  mermaidRoot: string;
  truncated: boolean;
  fileName: string;
  showPgFunctions?: boolean;
  headless?: boolean;
}) {
  const moduleName = useModuleNames();
  const { matches, visible } = useMemo(() => filterGraph(graph, view.filters), [graph, view.filters]);
  const modules = useMemo(() => {
    const all = new Set<string>();
    for (const n of graph.nodes) for (const m of n.modules) all.add(m);
    return [...all].sort((a, b) => moduleName(a).localeCompare(moduleName(b), "pt-BR"));
  }, [graph, moduleName]);

  async function copyMermaid() {
    try {
      const data = await request(MermaidDocument, {
        kind,
        root: mermaidRoot,
        depth: view.depth,
        minConfidence: view.minConfidence,
        includeInfra: view.includeInfra,
        expandPgFunctions: view.expandPgFunctions,
      });
      await navigator.clipboard.writeText(`\`\`\`mermaid\n${data.mermaid}\n\`\`\`\n`);
      toast.success("Mermaid copiado (bloco ```mermaid, até 80 nós)");
    } catch (error) {
      toast.error(`Não foi possível copiar o Mermaid: ${(error as Error).message}`);
    }
  }

  return (
    <div className="space-y-4">
      <FilterBar view={view} modules={modules} moduleName={moduleName} showPgFunctions={showPgFunctions} onChange={update} />
      {truncated ? (
        <Notice tone="warning">
          <TriangleAlert className="mr-1 inline h-4 w-4" aria-hidden="true" />
          Resultado cortado em {formatNumber(view.maxNodes)} nós: aumente o limite ou filtre (profundidade, confiança).
        </Notice>
      ) : null}
      {matches.size === 0 && graph.nodes.length > 1 ? <Notice>Nenhum nó passa nos filtros de operação, módulo e telas.</Notice> : null}
      <GraphView
        graph={graph}
        visible={visible}
        matches={matches}
        layout={layout}
        groups={view.groups}
        onGroupsChange={(next: GroupState) => update(groupParams(next))}
        selected={view.selected}
        onSelect={(id) => update({ sel: id })}
        moduleName={moduleName}
        fileName={fileName}
        headless={headless}
        toolbar={
          <Button variant="outline" size="sm" onClick={() => void copyMermaid()}>
            <ClipboardCopy className="h-4 w-4" aria-hidden="true" /> Copiar Mermaid
          </Button>
        }
      />
    </div>
  );
}
