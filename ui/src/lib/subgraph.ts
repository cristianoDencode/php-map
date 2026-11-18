/** Consultas dos diagramas e os nomes dos módulos. */
import { useQuery } from "@tanstack/react-query";
import { graphql } from "../graphql/generated";
import { request } from "./api";
import { NO_MODULE } from "./graph";
import { keyOf } from "./ids";

export const TraceDocument = graphql(`
  query TraceView(
    $entry: String!
    $depth: Int!
    $minConfidence: Float!
    $includeInfra: Boolean!
    $expandPgFunctions: Boolean!
    $maxNodes: Int!
  ) {
    trace(
      entry: $entry
      depth: $depth
      minConfidence: $minConfidence
      includeInfra: $includeInfra
      expandPgFunctions: $expandPgFunctions
      maxNodes: $maxNodes
    ) {
      ...SubgraphFields
    }
  }

  fragment SubgraphFields on Subgraph {
    root
    truncated
    summary
    depthOf
    parentOf
    nodes {
      id
      label
      name
      path
      modules
      attrs
    }
    edges {
      src
      dst
      type
      source
      confidence
      evidence
      occurrences
    }
  }
`);

export const ImpactDocument = graphql(`
  query ImpactView(
    $table: String!
    $schema: String
    $depth: Int!
    $minConfidence: Float!
    $includeInfra: Boolean!
    $maxNodes: Int!
  ) {
    impact(
      table: $table
      schema: $schema
      depth: $depth
      minConfidence: $minConfidence
      includeInfra: $includeInfra
      maxNodes: $maxNodes
    ) {
      ...SubgraphFields
      daoUsers {
        origin
        label
        operation
        daoMethod
        confidence
        path
        line
      }
    }
  }
`);

export const MermaidDocument = graphql(`
  query MermaidExport(
    $kind: String!
    $root: String!
    $depth: Int!
    $minConfidence: Float!
    $includeInfra: Boolean!
    $expandPgFunctions: Boolean!
  ) {
    mermaid(
      kind: $kind
      root: $root
      depth: $depth
      maxNodes: 80
      minConfidence: $minConfidence
      includeInfra: $includeInfra
      expandPgFunctions: $expandPgFunctions
    )
  }
`);

export const TargetSuggestDocument = graphql(`
  query TargetSuggest($prefix: String!, $label: String!) {
    suggest(prefix: $prefix, label: $label, limit: 8) {
      nodeId
      modules
    }
  }
`);

export const ModuleNameMapDocument = graphql(`
  query ModuleNameMap {
    modules {
      module
      name
    }
  }
`);

/** Nome do módulo pelo ID (`Module:empenho` → `Empenho`); sem agregados, a chave do ID. */
export function useModuleNames(): (id: string) => string {
  const query = useQuery({
    queryKey: ["moduleNameMap"],
    queryFn: () => request(ModuleNameMapDocument),
    meta: { silent: true },
    staleTime: Infinity,
  });
  const names = new Map((query.data?.modules ?? []).map((m) => [m.module, m.name]));
  return (id) => names.get(id) ?? (id === NO_MODULE ? NO_MODULE : keyOf(id));
}

/** Números da URL com limites: o que a pessoa digitou não quebra a consulta. */
export function numberParam(value: string | null, fallback: number, min: number, max: number): number {
  const parsed = value === null || value === "" ? Number.NaN : Number(value.replace(",", "."));
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, parsed));
}
