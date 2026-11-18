/**
 * Módulos: consultas, escala de cor da instabilidade, matriz, pontuação de
 * candidato a extração e CSV. Funções puras, testadas sem a página.
 */
import { graphql } from "../graphql/generated";
import type { ModuleFieldsFragment } from "../graphql/generated/graphql";
import { BOM, csvCell } from "./graph";

export const NO_MODULE = "(sem módulo)";

export const ModulesOverviewDocument = graphql(`
  query ModulesOverview {
    modules {
      ...ModuleFields
    }
    moduleEdges {
      src
      dst
      kind
      weight
      edges
      samples
    }
  }

  fragment ModuleFields on ModuleStats {
    module
    name
    files
    screens
    tables
    ca
    ce
    instability
    weightIn
    weightOut
    sharedTables
    ownedTables
    ownedShared
    coverage
  }
`);

export type ModuleRow = ModuleFieldsFragment;
export type EdgeKind = "code" | "table_read" | "table_write";
export const EDGE_KINDS: EdgeKind[] = ["code", "table_read", "table_write"];
export const KIND_LABELS: Record<EdgeKind, string> = {
  code: "código",
  table_read: "tabelas lidas",
  table_write: "tabelas gravadas",
};

export type Sample = { src_node?: string; dst_node?: string; type?: string; path?: string; line?: number };
export type ModuleEdge = { src: string; dst: string; kind: string; weight: number; edges: number; samples: unknown };

export function samplesOf(edge: ModuleEdge): Sample[] {
  return Array.isArray(edge.samples) ? (edge.samples as Sample[]) : [];
}

// --------------------------------------------------------------------------- cores

type Rgb = [number, number, number];
const STABLE: Rgb = [37, 99, 235]; // azul: muitos dependem dele
const NEUTRAL: Rgb = [148, 163, 184]; // cinza: meio a meio
const UNSTABLE: Rgb = [220, 38, 38]; // vermelho: depende de muitos
export const NO_INSTABILITY = "#94a3b8";

function mix(a: Rgb, b: Rgb, t: number): string {
  const c = a.map((v, i) => Math.round(v + (b[i]! - v) * t));
  return `#${c.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

/** Escala divergente: 0 (estável) azul → 0,5 cinza → 1 (instável) vermelho; sem Ca nem Ce, cinza. */
export function instabilityColor(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NO_INSTABILITY;
  const i = Math.min(1, Math.max(0, value));
  return i <= 0.5 ? mix(STABLE, NEUTRAL, i / 0.5) : mix(NEUTRAL, UNSTABLE, (i - 0.5) / 0.5);
}

/** Opacidade da célula da matriz: raiz do peso relativo (pesos pequenos continuam visíveis). */
export function weightAlpha(weight: number, max: number): number {
  if (weight <= 0 || max <= 0) return 0;
  return Math.round(Math.min(1, 0.12 + 0.88 * Math.sqrt(weight / max)) * 100) / 100;
}

/** Peso fracionário (`1/n`): arredonda só na exibição. */
export function formatWeight(weight: number): string {
  return weight.toLocaleString("pt-BR", { maximumFractionDigits: weight < 10 ? 1 : 0 });
}

// --------------------------------------------------------------------------- pontuação

export const COVERAGE_MIN = 0.7;
export const SCORE_FORMULA =
  "Pontuação = 50 × (1 − tabelas próprias compartilhadas ÷ tabelas próprias) + 50 × (1 − Ce ÷ maior Ce). " +
  "Cobertura abaixo de 70% (parte das dependências que vai para módulos conhecidos): pouco confiável.";

export type Candidate = {
  score: number;
  tablesPart: number;
  cePart: number;
  sharedRatio: number | null; // null: não é dono de tabelas
  reliable: boolean;
  explanation: string[];
};

function fmt(value: number, digits = 1): string {
  return value.toLocaleString("pt-BR", { minimumFractionDigits: 0, maximumFractionDigits: digits });
}

/**
 * Candidato a extração (Strangler Fig): tabelas próprias pouco compartilhadas e poucas
 * dependências de saída. A cobertura não muda a nota, só o selo (decisão do usuário).
 */
export function candidateScore(m: ModuleRow, maxCe: number): Candidate {
  const sharedRatio = m.ownedTables > 0 ? Math.min(1, m.ownedShared / m.ownedTables) : null;
  const tablesPart = 50 * (1 - (sharedRatio ?? 0));
  const cePart = maxCe > 0 ? 50 * (1 - m.ce / maxCe) : 50;
  const score = Math.round((tablesPart + cePart) * 10) / 10;
  const reliable = m.coverage !== null && m.coverage !== undefined && m.coverage >= COVERAGE_MIN;
  const explanation = [
    sharedRatio === null
      ? "Tabelas: não é dono de tabelas no dicionário → 50"
      : `Tabelas: 50 × (1 − ${m.ownedShared}/${m.ownedTables}) = ${fmt(tablesPart)}`,
    `Dependências: 50 × (1 − ${m.ce}/${maxCe}) = ${fmt(cePart)}`,
    `Total: ${fmt(score)}`,
    m.coverage === null || m.coverage === undefined
      ? "Cobertura: sem dependências medidas → pouco confiável"
      : `Cobertura: ${fmt(m.coverage * 100, 0)}% das dependências vão para módulos conhecidos${reliable ? "" : " (< 70%: pouco confiável)"}`,
  ];
  return { score, tablesPart, cePart, sharedRatio, reliable, explanation };
}

export function realModules(modules: ModuleRow[]): ModuleRow[] {
  return modules.filter((m) => m.module !== NO_MODULE);
}

export function maxCeOf(modules: ModuleRow[]): number {
  return Math.max(0, ...realModules(modules).map((m) => m.ce));
}

// --------------------------------------------------------------------------- ordenação

export type SortKey = "name" | "files" | "screens" | "ownedTables" | "ownedShared" | "ca" | "ce" | "instability" | "coverage" | "score";

export function sortModules(
  modules: ModuleRow[],
  key: SortKey,
  desc: boolean,
  score: (m: ModuleRow) => number = () => 0,
): ModuleRow[] {
  const value = (m: ModuleRow): number | string | null => {
    if (key === "name") return m.name;
    if (key === "score") return score(m);
    return m[key] ?? null;
  };
  return [...modules].sort((a, b) => {
    const va = value(a);
    const vb = value(b);
    let order = 0;
    if (va === null && vb !== null) return 1; // sem valor sempre por último
    if (vb === null && va !== null) return -1;
    if (typeof va === "string" && typeof vb === "string") order = va.localeCompare(vb, "pt-BR");
    else if (va !== null && vb !== null) order = Number(va) - Number(vb);
    if (desc) order = -order;
    return order || a.name.localeCompare(b.name, "pt-BR");
  });
}

// --------------------------------------------------------------------------- matriz

export type Cell = { weight: number; edges: number; byKind: Partial<Record<EdgeKind, ModuleEdge>> };
export type Matrix = Map<string, Map<string, Cell>>;

/** Linhas dependem das colunas: `matrix.get(src).get(dst)`, somando os tipos escolhidos. */
export function buildMatrix(edges: ModuleEdge[], kinds: EdgeKind[]): { matrix: Matrix; max: number } {
  const matrix: Matrix = new Map();
  let max = 0;
  for (const e of edges) {
    if (!kinds.includes(e.kind as EdgeKind)) continue;
    const row = matrix.get(e.src) ?? new Map<string, Cell>();
    const cell = row.get(e.dst) ?? { weight: 0, edges: 0, byKind: {} };
    cell.weight += e.weight;
    cell.edges += e.edges;
    cell.byKind[e.kind as EdgeKind] = e;
    row.set(e.dst, cell);
    matrix.set(e.src, row);
    max = Math.max(max, cell.weight);
  }
  return { matrix, max };
}

// --------------------------------------------------------------------------- CSV

function decimal(value: number | null | undefined, digits: number): string {
  return value === null || value === undefined ? "" : value.toFixed(digits).replace(".", ",");
}

function csv(lines: (string | number | null)[][]): string {
  return `${BOM}${lines.map((l) => l.map(csvCell).join(";")).join("\r\n")}\r\n`;
}

export const RANKING_COLUMNS = [
  "modulo",
  "nome",
  "arquivos",
  "telas",
  "tabelas_proprias",
  "proprias_compartilhadas",
  "ca",
  "ce",
  "instabilidade",
  "cobertura",
  "parte_tabelas",
  "parte_ce",
  "pontuacao",
  "confiavel",
] as const;

export function rankingCsv(modules: ModuleRow[], maxCe: number): string {
  return csv([
    [...RANKING_COLUMNS],
    ...modules.map((m) => {
      const c = candidateScore(m, maxCe);
      return [
        m.module,
        m.name,
        m.files,
        m.screens,
        m.ownedTables,
        m.ownedShared,
        m.ca,
        m.ce,
        decimal(m.instability, 4),
        decimal(m.coverage, 4),
        decimal(c.tablesPart, 1),
        decimal(c.cePart, 1),
        decimal(c.score, 1),
        c.reliable ? "sim" : "não",
      ];
    }),
  ]);
}

/** Matriz em CSV: 1ª coluna = quem depende; demais = de quem depende (peso com vírgula decimal). */
export function matrixCsv(order: ModuleRow[], matrix: Matrix): string {
  return csv([
    ["depende_de", ...order.map((m) => m.name)],
    ...order.map((row) => [
      row.name,
      ...order.map((col) => {
        const cell = matrix.get(row.module)?.get(col.module);
        return cell ? decimal(cell.weight, 2) : "";
      }),
    ]),
  ]);
}
