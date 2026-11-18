/**
 * Explorador de vizinhança: `/explorar/:id?abertos=&escondidos=`.
 *
 * Começa pelos vizinhos de entrada e de saída do nó. "Expandir" busca os vizinhos de outro nó e
 * acrescenta ao desenho; "Esconder" tira o nó; "Fixar" prende a posição dele no layout.
 */
import { useQueries } from "@tanstack/react-query";
import { EyeOff, Pin, PinOff, Plus } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router";
import { GraphView } from "../components/graph/GraphView";
import { Button, LoadingRows, Notice } from "../components/ui";
import { graphql } from "../graphql/generated";
import { request } from "../lib/api";
import { formatList, type GEdge, type GNode, type Graph, parseList } from "../lib/graph";
import { withParams } from "../lib/graph-url";
import { shortName } from "../lib/ids";
import { useModuleNames } from "../lib/subgraph";
import { formatNumber } from "../lib/utils";

export const NEIGHBOR_LIMIT = 25;

const ExploreDocument = graphql(`
  query ExploreNeighbors($id: ID!, $limit: Int!) {
    node(id: $id) {
      id
      label
      name
      path
      modules
      attrs
      outgoing: neighbors(direction: OUT, limit: $limit) {
        ...NeighborPage
      }
      incoming: neighbors(direction: IN, limit: $limit) {
        ...NeighborPage
      }
    }
  }

  fragment NeighborPage on EdgePage {
    total
    items {
      src
      dst
      type
      source
      confidence
      evidence
      occurrences
    }
    nodes {
      id
      label
      name
      path
      modules
      attrs
    }
  }
`);

type NodeData = { id: string; label: string; name?: string | null; path?: string | null; modules: string[]; attrs: unknown };

function toNode(n: NodeData, depth: number | null): GNode {
  return { id: n.id, label: n.label, name: n.name, path: n.path, modules: n.modules, attrs: (n.attrs ?? {}) as Record<string, unknown>, depth };
}

export function ExplorePage() {
  const root = useParams().id ?? "";
  const [params, setParams] = useSearchParams();
  const abertos = params.get("abertos");
  const expanded = useMemo(() => [root, ...parseList(abertos).filter((id) => id !== root)], [root, abertos]);
  const extra = expanded.slice(1);
  const hidden = useMemo(() => new Set(parseList(params.get("escondidos"))), [params]);
  const selected = params.get("sel") || null;
  const [locked, setLocked] = useState<string[]>([]);
  const moduleName = useModuleNames();

  const results = useQueries({
    queries: expanded.map((id) => ({
      queryKey: ["explore", id],
      queryFn: () => request(ExploreDocument, { id, limit: NEIGHBOR_LIMIT }),
      meta: { silent: true },
      staleTime: Infinity,
    })),
  });

  const update = useCallback(
    (changes: Record<string, string | null>) => setParams((current) => withParams(current, changes)),
    [setParams],
  );

  const graph: Graph | null = useMemo(() => {
    const first = results[0]?.data?.node;
    if (!first) return null;
    const nodes = new Map<string, GNode>([[root, toNode(first, 0)]]);
    const parentOf: Record<string, string> = {};
    const edges = new Map<string, GEdge>();
    // Na ordem dos expandidos: o nível e o pai vêm do primeiro nó que trouxe o vizinho.
    for (const result of results) {
      const node = result.data?.node;
      if (!node) continue;
      const depth = nodes.get(node.id)?.depth ?? 0;
      if (!nodes.has(node.id)) nodes.set(node.id, toNode(node, depth));
      for (const page of [node.outgoing, node.incoming]) {
        for (const n of page.nodes) {
          if (!nodes.has(n.id)) {
            nodes.set(n.id, toNode(n, (depth ?? 0) + 1));
            parentOf[n.id] = node.id;
          }
        }
        for (const e of page.items) {
          edges.set(`${e.src}|${e.dst}|${e.type}|${e.source}`, {
            src: e.src,
            dst: e.dst,
            type: e.type,
            source: e.source,
            confidence: e.confidence,
            evidence: (e.evidence ?? {}) as Record<string, unknown>,
            occurrences: e.occurrences,
          });
        }
      }
    }
    // Vizinho de entrada: a aresta vai do filho para o pai (`treeEdge` procura nos dois sentidos).
    return { root, nodes: [...nodes.values()], edges: [...edges.values()], parentOf, reverse: false };
  }, [results, root]);

  const visible = useMemo(() => new Set((graph?.nodes ?? []).map((n) => n.id).filter((id) => id === root || !hidden.has(id))), [graph, hidden, root]);

  const first = results[0];
  if (!root) return <Notice>Escolha um nó (pelo detalhe, pela busca ou pelo painel de um diagrama).</Notice>;
  if (first?.isPending) return <LoadingRows rows={8} label="Buscando a vizinhança" />;
  if (first?.isError) return <Notice tone="warning">{first.error.message}</Notice>;
  if (!graph || !first?.data?.node) {
    return <Notice tone="warning">Nó não encontrado no grafo: {root}. Use a busca (Ctrl K).</Notice>;
  }

  const totals = new Map(
    results.flatMap((r) => (r.data?.node ? [[r.data.node.id, r.data.node.outgoing.total + r.data.node.incoming.total] as const] : [])),
  );
  const loading = results.some((r) => r.isFetching);

  function panelExtra(id: string) {
    const isExpanded = expanded.includes(id);
    const isLocked = locked.includes(id);
    const total = totals.get(id);
    return (
      <div className="space-y-2 border-t pt-3">
        {isExpanded && total !== undefined ? (
          <p className="text-xs text-muted-foreground">
            Mostrando até {NEIGHBOR_LIMIT} vizinhos de entrada e {NEIGHBOR_LIMIT} de saída, de {formatNumber(total)} ligações.
          </p>
        ) : null}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" disabled={isExpanded} onClick={() => update({ abertos: formatList([...extra, id]) })}>
            <Plus className="h-4 w-4" aria-hidden="true" /> {isExpanded ? "Expandido" : "Expandir vizinhos"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setLocked(isLocked ? locked.filter((l) => l !== id) : [...locked, id])}
          >
            {isLocked ? <PinOff className="h-4 w-4" aria-hidden="true" /> : <Pin className="h-4 w-4" aria-hidden="true" />}
            {isLocked ? "Soltar" : "Fixar"}
          </Button>
          {id !== root ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => update({ escondidos: formatList([...hidden, id]), sel: null })}
            >
              <EyeOff className="h-4 w-4" aria-hidden="true" /> Esconder
            </Button>
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <header className="space-y-1">
        <p className="text-sm text-muted-foreground">Explorador de vizinhança</p>
        <h1 className="break-all text-2xl font-semibold">{shortName(root, first.data.node.name)}</h1>
        <p className="text-sm text-muted-foreground">
          {formatNumber(expanded.length)} nó(s) expandido(s){loading ? " · carregando…" : ""}.
          Clique num nó e use "Expandir vizinhos" para crescer o desenho.
        </p>
      </header>
      {hidden.size ? (
        <Notice>
          {hidden.size} nó(s) escondido(s): {[...hidden].map((id) => shortName(id)).join(", ")}.{" "}
          <button type="button" className="text-primary underline" onClick={() => update({ escondidos: null })}>
            Mostrar todos
          </button>
        </Notice>
      ) : null}
      <GraphView
        graph={graph}
        visible={visible}
        matches={visible}
        layout="fcose"
        groups={null}
        selected={selected}
        onSelect={(id) => update({ sel: id })}
        moduleName={moduleName}
        fileName={`vizinhanca-${root.replace(/[^\w.-]+/g, "_")}`}
        panelExtra={panelExtra}
        locked={locked}
      />
    </div>
  );
}
