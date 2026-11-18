import { useQuery } from "@tanstack/react-query";
import { graphql } from "../graphql/generated";
import { request } from "./api";
import { labelOf, shortName } from "./ids";

const NodeNamesQuery = graphql(`
  query NodeNames($ids: [ID!]!) {
    nodes(ids: $ids) {
      id
      name
      attrs
    }
  }
`);

/** Nome legível: o caminho no menu para item de menu, o nome para módulo, senão o nome curto. */
export function displayName(id: string, name?: string | null, attrs?: unknown): string {
  if (labelOf(id) === "BusinessAction") {
    const path = (attrs as { menu_path?: unknown } | null | undefined)?.menu_path;
    if (typeof path === "string" && path) return path;
  }
  return shortName(id, name);
}

/**
 * A busca e as sugestões devolvem só o ID (`BusinessAction:menu:7650`): os nomes vêm numa
 * consulta só, em lote. Enquanto não chegam, o nome curto do ID.
 */
export function useNodeNames(ids: string[]): (id: string) => string {
  const wanted = ids.filter((id) => ["BusinessAction", "Module"].includes(labelOf(id)));
  const query = useQuery({
    queryKey: ["nodeNames", wanted],
    queryFn: () => request(NodeNamesQuery, { ids: wanted }),
    enabled: wanted.length > 0,
    meta: { silent: true },
  });
  const names = new Map((query.data?.nodes ?? []).map((n) => [n.id, displayName(n.id, n.name, n.attrs)]));
  return (id) => names.get(id) ?? shortName(id);
}
