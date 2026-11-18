/**
 * Menus por fonte: consultas, distância na URL, caminho em texto e CSV.
 * Funções puras, testadas sem a página.
 */
import { graphql } from "../graphql/generated";
import type { FileMenusQuery } from "../graphql/generated/graphql";
import { BOM, csvCell } from "./graph";
import { keyOf, shortName } from "./ids";
import { edgeVerb } from "./labels";

// 5: tela → JS → rota → controller → service são 4 saltos; igual ao servidor.
export const DEFAULT_DISTANCE = 5;
export const MAX_DISTANCE = 10;

export const FindFilesDocument = graphql(`
  query FindFiles($text: String!) {
    findFiles(text: $text, limit: 10)
  }
`);

export const FileMenusDocument = graphql(`
  query FileMenus($file: String!, $maxDistance: Int!, $module: String, $text: String) {
    menusForFile(file: $file, maxDistance: $maxDistance, module: $module, text: $text) {
      file
      candidates
      maxDistance
      total
      unfiltered
      truncated
      byDistance {
        distance
        count
      }
      items {
        menu
        name
        paths
        active
        params
        entry
        distance
        modules {
          module
          name
        }
        via {
          node
          label
          edgeType
          confidence
          path
          line
          incoming
        }
      }
    }
  }
`);

export type FileMenus = FileMenusQuery["menusForFile"];
export type FileMenuItem = FileMenus["items"][number];
export type ViaStep = FileMenuItem["via"][number];

/** `distancia` da URL: inteiro de 0 a 10; ausente ou inválido → o padrão (5). */
export function parseDistance(value: string | null): number {
  if (value === null || !/^\d+$/.test(value.trim())) return DEFAULT_DISTANCE;
  return Math.min(MAX_DISTANCE, Number(value.trim()));
}

export function distanceLabel(distance: number): string {
  if (distance === 0) return "0 — só se o fonte for a entrada do menu";
  if (distance === 1) return "1 — direto";
  return `${distance} saltos`;
}

/** Chips do resumo: "direto (0–1)" junta 0 e 1; os demais, um por distância. */
export function distanceChips(byDistance: FileMenus["byDistance"]): { label: string; count: number }[] {
  const direct = byDistance.filter((d) => d.distance <= 1).reduce((sum, d) => sum + d.count, 0);
  const chips = direct ? [{ label: "direto (0–1)", count: direct }] : [];
  for (const d of byDistance) if (d.distance > 1) chips.push({ label: String(d.distance), count: d.count });
  return chips;
}

/** Telas (arquivos de entrada) distintas na lista. */
export function screenCount(items: FileMenuItem[]): number {
  return new Set(items.map((i) => i.entry)).size;
}

/** Um passo em linguagem simples: "requer db_x.php", "é declarado em x.php". */
export function stepText(step: ViaStep): string {
  const name = shortName(step.node);
  return step.edgeType ? `${edgeVerb(step.edgeType, step.incoming)} ${name}` : name;
}

/** `a -[INCLUDES]-> b <-[DECLARES]- c` (a aresta real de `DECLARES` vai do arquivo à classe). */
export function viaText(via: ViaStep[]): string {
  return via
    .map((step) => {
      const key = keyOf(step.node);
      if (!step.edgeType) return key;
      return step.incoming ? ` <-[${step.edgeType}]- ${key}` : ` -[${step.edgeType}]-> ${key}`;
    })
    .join("");
}

export const MENU_CSV_COLUMNS = [
  "menu",
  "caminho_menu",
  "modulos",
  "ativo",
  "parametros",
  "entrada",
  "distancia",
  "como_chega",
] as const;

/** CSV com `;` e BOM (o Excel em português abre direto, com os acentos). */
export function menusCsv(items: FileMenuItem[]): string {
  const lines = [MENU_CSV_COLUMNS.join(";")];
  for (const item of items) {
    lines.push(
      [
        keyOf(item.menu),
        item.paths.length ? item.paths.join(" | ") : (item.name ?? ""),
        item.modules.map((m) => m.name).join(" | "),
        item.active ? "sim" : "não",
        item.params ?? "",
        keyOf(item.entry),
        item.distance,
        viaText(item.via),
      ]
        .map(csvCell)
        .join(";"),
    );
  }
  return `${BOM}${lines.join("\r\n")}\r\n`;
}

/** `menus-db_empempenho_classe.csv`. */
export function csvName(file: string): string {
  const base = shortName(file.startsWith("File:") ? file : `File:${file}`).replace(/\.php$/i, "");
  return `menus-${base.replace(/[^\w.-]+/g, "_")}.csv`;
}
