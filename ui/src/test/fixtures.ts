/** Dados do piloto (anulação de empenho) para os testes da interface. */

export const SCREEN = "File:emp4_anularempenho001.php";
export const TABLE = "Table:public.empempenho";

export function screenNode() {
  return {
    node: {
      id: SCREEN,
      label: "File",
      name: "emp4_anularempenho001.php",
      path: "emp4_anularempenho001.php",
      modules: ["Module:empenho"],
      attrs: {
        path: "emp4_anularempenho001.php",
        lines: 119,
        parse_status: "ok",
        module_hint: "emp",
        is_infra: false,
        resolved: true,
      },
      memberships: [
        { module: "Module:empenho", name: "Empenho", source: "ECIDADE_CATALOG", confidence: 1 },
      ],
    },
  };
}

type Edge = {
  src: string;
  dst: string;
  type: string;
  source: string;
  confidence: number;
  evidence: Record<string, unknown>;
  occurrences: number;
};

function edge(dst: string, type: string, line: number, confidence = 1): Edge {
  return {
    src: SCREEN,
    dst,
    type,
    source: type === "QUERIES" ? "SQL_AST" : "PHP_AST",
    confidence,
    evidence: { path: "emp4_anularempenho001.php", line },
    occurrences: 1,
  };
}

/** 120 arestas de saída (3 páginas de 50), agrupadas por tipo na ordem do servidor. */
export const OUT_EDGES: Edge[] = [
  edge("File:libs/db_utils.php", "INCLUDES", 3),
  edge("File:classes/db_empempenho_classe.php", "INCLUDES", 5),
  ...Array.from({ length: 116 }, (_, i) => edge(`File:tela${String(i).padStart(3, "0")}.php`, "REFERENCES", 20 + i, 0.9)),
  edge(TABLE, "QUERIES", 12),
  edge("Table:public.empanulado", "QUERIES", 14, 0.7),
];

export function relations(variables: Record<string, unknown>) {
  const incoming = variables.direction === "IN";
  const offset = Number(variables.offset ?? 0);
  const limit = Number(variables.limit ?? 50);
  const items = incoming ? [] : OUT_EDGES.slice(offset, offset + limit);
  return {
    node: {
      id: SCREEN,
      neighbors: {
        total: incoming ? 0 : OUT_EDGES.length,
        items,
        nodes: items.map((item) => ({ id: item.dst, name: null })),
      },
    },
  };
}

export function sourceFile(overrides: Record<string, unknown> = {}) {
  const lines = Array.from({ length: 30 }, (_, i) => `<?php // linha ${i + 1}`);
  return {
    sourceFile: {
      path: "emp4_anularempenho001.php",
      totalLines: 30,
      startLine: 1,
      lines,
      highlight: 12,
      truncated: false,
      sizeBytes: 900,
      ...overrides,
    },
  };
}
