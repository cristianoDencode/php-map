/** Módulos: escala de cor, ordenação da matriz, pontuação de candidato e CSV. */
import { describe, expect, test } from "vitest";
import {
  buildMatrix,
  candidateScore,
  instabilityColor,
  matrixCsv,
  type ModuleEdge,
  type ModuleRow,
  NO_INSTABILITY,
  RANKING_COLUMNS,
  rankingCsv,
  sortModules,
  weightAlpha,
} from "./modules";

function module(id: string, fields: Partial<ModuleRow> = {}): ModuleRow {
  return {
    module: id,
    name: id.replace("Module:", ""),
    files: 10,
    screens: 2,
    tables: 1,
    ca: 1,
    ce: 1,
    instability: 0.5,
    weightIn: 1,
    weightOut: 1,
    sharedTables: 0,
    ownedTables: 0,
    ownedShared: 0,
    coverage: 0.8,
    ...fields,
  } as ModuleRow;
}

describe("escala de cor da instabilidade", () => {
  test("divergente: azul estável, cinza no meio, vermelho instável", () => {
    expect(instabilityColor(0)).toBe("#2563eb");
    expect(instabilityColor(0.5)).toBe("#94a3b8");
    expect(instabilityColor(1)).toBe("#dc2626");
    expect(instabilityColor(0.25)).toBe("#5d83d2"); // metade do caminho azul → cinza
    expect(instabilityColor(0.75)).toBe("#b8656f"); // metade do caminho cinza → vermelho
  });

  test("sem Ca nem Ce fica cinza; fora de 0–1 é cortado", () => {
    expect(instabilityColor(null)).toBe(NO_INSTABILITY);
    expect(instabilityColor(undefined)).toBe(NO_INSTABILITY);
    expect(instabilityColor(-2)).toBe(instabilityColor(0));
    expect(instabilityColor(7)).toBe(instabilityColor(1));
  });

  test("opacidade da célula pela raiz do peso", () => {
    expect(weightAlpha(0, 10)).toBe(0);
    expect(weightAlpha(10, 10)).toBe(1);
    expect(weightAlpha(2.5, 10)).toBe(0.56); // 0,12 + 0,88 × 0,5
  });
});

describe("ordenação (matriz e ranking)", () => {
  const list = [
    module("Module:caixa", { ca: 5, ce: 2, instability: 0.29 }),
    module("Module:almox", { ca: 1, ce: 11, instability: 0.92 }),
    module("Module:biblioteca", { ca: 5, ce: 3, instability: null }),
  ];

  test("por Ca decrescente, empate pelo nome", () => {
    expect(sortModules(list, "ca", true).map((m) => m.name)).toEqual(["biblioteca", "caixa", "almox"]);
  });

  test("por instabilidade, sem valor por último nos dois sentidos", () => {
    expect(sortModules(list, "instability", false).map((m) => m.name)).toEqual(["caixa", "almox", "biblioteca"]);
    expect(sortModules(list, "instability", true).map((m) => m.name)).toEqual(["almox", "caixa", "biblioteca"]);
  });

  test("por nome e pela pontuação calculada", () => {
    expect(sortModules(list, "name", false).map((m) => m.name)).toEqual(["almox", "biblioteca", "caixa"]);
    const score = (m: ModuleRow) => ({ caixa: 3, almox: 9, biblioteca: 1 })[m.name] ?? 0;
    expect(sortModules(list, "score", true, score).map((m) => m.name)).toEqual(["almox", "caixa", "biblioteca"]);
  });
});

describe("pontuação de candidato", () => {
  test("média de 2 partes, com a explicação de cada número", () => {
    const empenho = module("Module:empenho", { ownedTables: 10, ownedShared: 3, ce: 17, coverage: 0.47 });
    const c = candidateScore(empenho, 37);
    expect(c.tablesPart).toBeCloseTo(35);
    expect(c.cePart).toBeCloseTo(50 * (1 - 17 / 37));
    expect(c.score).toBe(62); // 35 + 27,03 → uma casa
    expect(c.reliable).toBe(false);
    expect(c.explanation).toEqual([
      "Tabelas: 50 × (1 − 3/10) = 35",
      "Dependências: 50 × (1 − 17/37) = 27",
      "Total: 62",
      "Cobertura: 47% das dependências vão para módulos conhecidos (< 70%: pouco confiável)",
    ]);
  });

  test("sem tabelas próprias, Ce máximo e cobertura desconhecida", () => {
    const c = candidateScore(module("Module:x", { ownedTables: 0, ce: 4, coverage: null }), 4);
    expect(c.sharedRatio).toBeNull();
    expect(c.score).toBe(50);
    expect(c.explanation[0]).toBe("Tabelas: não é dono de tabelas no dicionário → 50");
    expect(c.explanation[3]).toBe("Cobertura: sem dependências medidas → pouco confiável");
    expect(candidateScore(module("Module:y", { coverage: 0.7 }), 1).reliable).toBe(true); // 70% já é confiável
  });

  test("a cobertura não muda a nota, só o selo", () => {
    const a = candidateScore(module("Module:a", { coverage: 0.1 }), 5);
    const b = candidateScore(module("Module:b", { coverage: 0.9 }), 5);
    expect(a.score).toBe(b.score);
    expect([a.reliable, b.reliable]).toEqual([false, true]);
  });
});

describe("matriz e CSV", () => {
  const edges: ModuleEdge[] = [
    { src: "Module:a", dst: "Module:b", kind: "code", weight: 2.5, edges: 3, samples: [] },
    { src: "Module:a", dst: "Module:b", kind: "table_read", weight: 1, edges: 1, samples: [] },
    { src: "Module:b", dst: "Module:a", kind: "table_write", weight: 0.333333, edges: 1, samples: [] },
  ];

  test("soma os tipos escolhidos; linha depende da coluna", () => {
    const all = buildMatrix(edges, ["code", "table_read", "table_write"]);
    expect(all.matrix.get("Module:a")?.get("Module:b")).toMatchObject({ weight: 3.5, edges: 4 });
    expect(all.max).toBe(3.5);
    const code = buildMatrix(edges, ["code"]);
    expect(code.matrix.get("Module:b")).toBeUndefined();
    expect(code.max).toBe(2.5);
  });

  test("CSV da matriz com vírgula decimal e BOM", () => {
    const order = [module("Module:a"), module("Module:b")];
    const text = matrixCsv(order, buildMatrix(edges, ["code", "table_read", "table_write"]).matrix);
    expect(text.charCodeAt(0)).toBe(0xfeff);
    expect(text.slice(1).trim().split("\r\n")).toEqual(["depende_de;a;b", "a;;3,50", "b;0,33;"]);
  });

  test("CSV do ranking com as colunas e a pontuação", () => {
    const rows = [module("Module:empenho", { ownedTables: 10, ownedShared: 3, ce: 17, coverage: 0.47, instability: 0.4359 })];
    const [header, line] = rankingCsv(rows, 37).slice(1).trim().split("\r\n");
    expect(header).toBe(RANKING_COLUMNS.join(";"));
    expect(line).toBe("Module:empenho;empenho;10;2;10;3;1;17;0,4359;0,4700;35,0;27,0;62,0;não");
  });
});
