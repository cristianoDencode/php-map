/**
 * Estado dos diagramas na URL: o link reproduz a vista (alvo, filtros, grupos
 * abertos e fechados, nó selecionado).
 *
 * `prof`, `conf`, `infra`, `pg` e `limite` vão para a API (a mesma semântica de `--depth`,
 * `--min-confidence`, `--include-infra` e `--expand-pgfunctions` da CLI). `op`, `modulo` e
 * `telas` filtram no navegador.
 */
import { type ClientFilters, formatList, type GroupState, OPERATIONS, type Operation, parseList } from "./graph";
import { numberParam } from "./subgraph";

export const MAX_NODES = 2000;
export const MAX_DEPTH = 10;

export type ViewParams = {
  depth: number;
  minConfidence: number;
  includeInfra: boolean;
  expandPgFunctions: boolean;
  maxNodes: number;
  filters: ClientFilters;
  groups: GroupState;
  selected: string | null;
};

export function readView(params: URLSearchParams, defaultDepth: number): ViewParams {
  return {
    depth: numberParam(params.get("prof"), defaultDepth, 1, MAX_DEPTH),
    minConfidence: numberParam(params.get("conf"), 0, 0, 1),
    includeInfra: params.get("infra") === "1",
    expandPgFunctions: params.get("pg") === "1",
    maxNodes: numberParam(params.get("limite"), MAX_NODES, 1, MAX_NODES),
    filters: {
      ops: parseList(params.get("op")).filter((op): op is Operation => (OPERATIONS as readonly string[]).includes(op)),
      module: params.get("modulo") || null,
      onlyScreens: params.get("telas") === "1",
    },
    groups: { open: parseList(params.get("abertos")), closed: parseList(params.get("fechados")) },
    selected: params.get("sel") || null,
  };
}

/** Novos parâmetros a partir dos atuais; `null` (ou o valor padrão) tira a chave. */
export function withParams(params: URLSearchParams, changes: Record<string, string | null>): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const [key, value] of Object.entries(changes)) {
    if (value === null || value === "") next.delete(key);
    else next.set(key, value);
  }
  return next;
}

export function groupParams(groups: GroupState): Record<string, string | null> {
  return { abertos: formatList(groups.open), fechados: formatList(groups.closed) };
}

/** Mudou o que a API devolve: os grupos e a seleção da vista anterior não valem mais. */
export function resetView(): Record<string, string | null> {
  return { abertos: null, fechados: null, sel: null };
}
