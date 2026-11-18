/** IDs do grafo: `<Label>:<chave>` (`File:forms/db_frm.php`, `Table:public.empempenho`). */

export function labelOf(id: string): string {
  return id.slice(0, Math.max(0, id.indexOf(":")));
}

export function keyOf(id: string): string {
  const at = id.indexOf(":");
  return at < 0 ? id : id.slice(at + 1);
}

/** Rota do detalhe: o ID tem `:` e `/`, então vai codificado. */
export function nodeHref(id: string): string {
  return `/no/${encodeURIComponent(id)}`;
}

/** Nome curto: o arquivo sem diretório, a tabela sem `public.`, o módulo/menu pelo nome. */
export function shortName(id: string, name?: string | null): string {
  const label = labelOf(id);
  const key = keyOf(id);
  if (label === "File") return key.split("/").pop() ?? key;
  if (label === "Table") return key.startsWith("public.") ? key.slice("public.".length) : key;
  if (label === "BusinessAction" || label === "Module") return name ?? key;
  return key;
}

/** Caminho de evidência que é arquivo do e-cidade (o catálogo usa `db:<tabela>`). */
export function isSourcePath(path: unknown): path is string {
  return typeof path === "string" && path.length > 0 && !path.startsWith("db:");
}

/** Ficha de regras de um arquivo: `/regras/File%3Aemp4_x.php`. */
export function rulesHref(id: string): string {
  return `/regras/${encodeURIComponent(id.startsWith("File:") ? id : `File:${id}`)}`;
}
