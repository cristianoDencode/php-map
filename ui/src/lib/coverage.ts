import type { Stats } from "../app/Layout";

/**
 * Cobertura de módulo do scan: arquivos com módulo (catálogo ou regra de nome) e o
 * total; telas e telas no menu. `null` quando o scan não trouxe o dado.
 */
export function coverage(stats: Stats | undefined) {
  const modules = stats?.catalog?.modules;
  const withModule = modules
    ? (modules.files_with_catalog ?? 0) + (modules.screens_classified ?? 0) + (modules.daos_classified ?? 0)
    : null;
  const total = withModule != null && modules ? withModule + (modules.files_without_module ?? 0) : null;
  const screens = stats?.analytics?.screens ?? null;
  const inMenu = stats?.analytics?.screens_in_menu ?? null;
  return { withModule, total, screens, inMenu };
}
