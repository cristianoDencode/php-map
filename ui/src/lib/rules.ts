/**
 * Ficha de regras de negócio: a consulta, os filtros da página e o Markdown
 * exportado. Funções puras, testadas junto com a página.
 */
import { graphql } from "../graphql/generated";
import type { BusinessRulesQuery } from "../graphql/generated/graphql";
import { keyOf, shortName } from "./ids";
import { edgeVerb } from "./labels";

export const BusinessRulesDocument = graphql(`
  query BusinessRules($entry: String!, $depth: Int!) {
    businessRules(entry: $entry, depth: $depth) {
      entry
      depth
      minConfidence
      clamped
      modules {
        module
        name
        source
        confidence
      }
      menus {
        id
        path
        paths
        help
        desctec
        active
        params
      }
      actions {
        file
        cases
      }
      participants {
        node
        label
        name
        depth
        via
        parent
        confidence
        evidence
      }
      writes {
        ...RuleTableFields
      }
      reads {
        ...RuleTableFields
      }
      rpc {
        function
        name
        resolved
        evidence {
          path
          line
          from
        }
      }
      triggers {
        table
        function
        evidence {
          path
          line
          from
        }
      }
      messages {
        text
        kind
        category
        file
        line
        scope
      }
      notes {
        text
        file
        line
        scope
      }
      stats {
        files
        methods
        tables
        messages
        notes
        truncated
      }
    }
  }

  fragment RuleTableFields on RuleTable {
    table
    name
    operations
    catalog
    occurrences
    columnsLabeled {
      column
      label
      description
    }
    evidence {
      path
      line
      from
    }
  }
`);

export type Rules = BusinessRulesQuery["businessRules"];
export type RuleMessage = Rules["messages"][number];
export type RuleNote = Rules["notes"][number];
export type RuleTable = Rules["writes"][number];
export type RuleParticipant = Rules["participants"][number];

export const DEFAULT_DEPTH = 4;
export const DEPTHS = [1, 2, 3, 4, 5, 6, 8] as const;

/** Categorias na ordem de exibição; `mensagem` é o texto neutro. */
export const CATEGORIES = ["validação", "erro", "mensagem"] as const;
export type Category = (typeof CATEGORIES)[number];

export const CATEGORY_STYLE: Record<string, string> = {
  validação: "var(--warning)",
  erro: "var(--destructive)",
  mensagem: "var(--muted-foreground)",
};

export const OPERATION_LABELS: Record<string, string> = {
  INSERT: "insere",
  UPDATE: "altera",
  DELETE: "exclui",
  SELECT: "lê",
};

/** Minúsculas e sem acento: o filtro de texto casa "anulacao" com "Anulação". */
export function fold(text: string): string {
  return text.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

/** `method:empenho::anularempenho` → `empenho::anularempenho()`; `''` → topo do arquivo. */
export function scopeLabel(scope: string): string {
  if (!scope) return "topo do arquivo";
  const at = scope.indexOf(":");
  const kind = scope.slice(0, at);
  if (kind === "method" || kind === "function") return `${scope.slice(at + 1)}()`;
  return scope;
}

export type Filters = { text: string; category: Category | null };

export function hasFilter(filters: Filters): boolean {
  return filters.text.trim().length > 0 || filters.category !== null;
}

function matches(filters: Filters, ...parts: (string | null | undefined)[]): boolean {
  const wanted = fold(filters.text.trim());
  if (!wanted) return true;
  return parts.some((part) => part != null && fold(part).includes(wanted));
}

/** A ficha com os filtros aplicados. A categoria só filtra as mensagens. */
export function applyFilters(rules: Rules, filters: Filters): Rules {
  const table = (t: RuleTable) =>
    matches(
      filters,
      t.name,
      t.table,
      catalogText(t.catalog, "description"),
      ...t.columnsLabeled.flatMap((c) => [c.column, c.label]),
    );
  return {
    ...rules,
    messages: rules.messages.filter(
      (m) =>
        (filters.category === null || m.category === filters.category) &&
        matches(filters, m.text, m.file, m.scope),
    ),
    notes: rules.notes.filter((n) => matches(filters, n.text, n.file, n.scope)),
    writes: rules.writes.filter(table),
    reads: rules.reads.filter(table),
    participants: rules.participants.filter((p) => matches(filters, keyOf(p.node), p.name)),
    rpc: rules.rpc.filter((r) => matches(filters, r.name, r.function, r.evidence.path)),
    triggers: rules.triggers.filter((t) => matches(filters, t.table, t.function)),
  };
}

export function catalogText(catalog: unknown, key: "description" | "label" | "prefix"): string | null {
  if (catalog === null || typeof catalog !== "object") return null;
  const value = (catalog as Record<string, unknown>)[key];
  return typeof value === "string" && value ? value : null;
}

/** Agrupa por arquivo e, dentro dele, por escopo (método, função ou topo), na ordem da API. */
export function groupByFile<T extends { file: string; scope: string }>(
  items: T[],
): { file: string; scopes: { scope: string; items: T[] }[] }[] {
  const files = new Map<string, Map<string, T[]>>();
  for (const item of items) {
    const scopes = files.get(item.file) ?? new Map<string, T[]>();
    const list = scopes.get(item.scope) ?? [];
    list.push(item);
    scopes.set(item.scope, list);
    files.set(item.file, scopes);
  }
  return [...files.entries()].map(([file, scopes]) => ({
    file,
    scopes: [...scopes.entries()].map(([scope, list]) => ({ scope, items: list })),
  }));
}

export function countByCategory(messages: RuleMessage[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const m of messages) counts[m.category] = (counts[m.category] ?? 0) + 1;
  return counts;
}

/** "requer (a partir de emp4_x.php)": por que o nó entrou no fluxo. */
export function participantReason(p: RuleParticipant): string {
  if (!p.via || !p.parent) return "entrada";
  return `${edgeVerb(p.via, true)} ${shortName(p.parent)}`;
}

function evidenceText(path: string, line: number | null | undefined): string {
  return `\`${path}${line ? `:${line}` : ""}\``;
}

function oneLine(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

/** A ficha (já filtrada) em Markdown, com as mesmas seções da página e as evidências. */
export function rulesMarkdown(rules: Rules, filters?: Filters): string {
  const entry = keyOf(rules.entry);
  const out: string[] = [`# Regras de negócio: ${entry}`, ""];
  const s = rules.stats;
  out.push(
    `Fluxo com profundidade ${rules.depth} e confiança ≥ ${rules.minConfidence.toLocaleString("pt-BR")}: ${s.files} arquivo(s), ` +
      `${s.methods} método(s)/função(ões), ${s.tables} tabela(s), ${s.messages} mensagem(ns).` +
      (s.truncated ? " O fluxo foi cortado no limite de nós." : ""),
  );
  if (rules.modules.length) out.push(`Módulo: ${rules.modules.map((m) => m.name).join(", ")}.`);
  if (filters && hasFilter(filters)) {
    const parts = [
      filters.text.trim() ? `texto “${filters.text.trim()}”` : null,
      filters.category ? `categoria ${filters.category}` : null,
    ].filter(Boolean);
    out.push(`Filtro aplicado: ${parts.join(", ")}.`);
  }
  out.push("");

  out.push("## O que a tela faz", "");
  if (!rules.menus.length && !rules.actions.length) out.push("Nenhum item de menu nem ação despachada.");
  for (const menu of rules.menus) {
    const extra = [menu.help, menu.desctec].filter((t) => t && t !== menu.path).join(" — ");
    out.push(
      `- Menu: **${menu.path ?? keyOf(menu.id)}**${menu.active ? "" : " (inativo)"}${extra ? ` — ${extra}` : ""}` +
        (menu.params ? ` (parâmetros: \`${menu.params}\`)` : ""),
    );
  }
  for (const action of rules.actions) {
    out.push(`- Ações despachadas por ${evidenceText(action.file, null)}: ${action.cases.map((c) => `\`${c}\``).join(", ")}`);
  }
  out.push("");

  out.push("## Regras e validações", "", "_Categoria: classificação heurística por palavra-chave._", "");
  if (!rules.messages.length) out.push("Nenhuma mensagem.");
  for (const group of groupByFile(rules.messages)) {
    out.push(`### ${group.file}`, "");
    for (const scope of group.scopes) {
      out.push(`- ${scopeLabel(scope.scope)}`);
      for (const m of scope.items) {
        out.push(`  - [${m.category}] ${oneLine(m.text)} (${evidenceText(m.file, m.line)})`);
      }
    }
    out.push("");
  }

  const tables = (title: string, list: RuleTable[]) => {
    out.push(`## ${title}`, "");
    if (!list.length) out.push("Nenhuma tabela.");
    for (const t of list) {
      const description = catalogText(t.catalog, "description");
      const ops = t.operations.map((op) => OPERATION_LABELS[op] ?? op).join(", ");
      out.push(`- **${t.name}** (${ops})${description ? ` — ${description}` : ""}`);
      const labeled = t.columnsLabeled.filter((c) => c.label);
      if (labeled.length) {
        out.push(`  - Colunas: ${labeled.map((c) => `${c.column} (${c.label})`).join(", ")}`);
      }
      out.push(`  - Evidência: ${t.evidence.map((e) => evidenceText(e.path, e.line)).join(", ")}`);
    }
    out.push("");
  };
  tables("Dados que altera", rules.writes);
  tables("Dados que consulta", rules.reads);

  out.push("## Regras no banco", "");
  if (!rules.rpc.length && !rules.triggers.length) out.push("Nenhuma função do banco nem trigger.");
  for (const r of rules.rpc) {
    out.push(
      `- Função \`${r.name}\`${r.resolved ? "" : " (fora do schema)"}, chamada por ${shortName(r.evidence.from)} ` +
        `(${evidenceText(r.evidence.path, r.evidence.line)})`,
    );
  }
  for (const t of rules.triggers) {
    out.push(
      `- Trigger: \`${shortName(t.table)}\` dispara \`${keyOf(t.function)}\`` +
        (t.evidence ? ` (${evidenceText(t.evidence.path, t.evidence.line)})` : ""),
    );
  }
  out.push("");

  out.push("## Arquivos envolvidos", "");
  for (const p of rules.participants.filter((item) => item.label === "File")) {
    const evidence = p.evidence as { path?: unknown; line?: unknown } | null;
    const where =
      evidence && typeof evidence.path === "string"
        ? ` (${evidenceText(evidence.path, typeof evidence.line === "number" ? evidence.line : null)})`
        : "";
    out.push(`- ${evidenceText(keyOf(p.node), null)} — nível ${p.depth}, ${participantReason(p)}${where}`);
  }
  out.push("");

  out.push("## Anotações do código", "");
  if (!rules.notes.length) out.push("Nenhuma anotação.");
  for (const n of rules.notes) out.push(`- ${oneLine(n.text)} (${evidenceText(n.file, n.line)})`);
  out.push("");
  return out.join("\n");
}
