import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ClipboardCopy, GitBranch, Radar, Search as SearchIcon, TriangleAlert } from "lucide-react";
import { type ReactNode, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { toast } from "sonner";
import { EvidenceLink } from "../app/code-panel";
import { LabelBadge, NodeLink } from "../components/NodeBits";
import { Badge, Button, Card, CardHeader, LoadingRows, Notice } from "../components/ui";
import { request } from "../lib/api";
import { keyOf, nodeHref, shortName } from "../lib/ids";
import { nodeKind } from "../lib/labels";
import {
  applyFilters,
  BusinessRulesDocument,
  CATEGORIES,
  CATEGORY_STYLE,
  type Category,
  catalogText,
  countByCategory,
  DEFAULT_DEPTH,
  DEPTHS,
  type Filters,
  groupByFile,
  hasFilter,
  OPERATION_LABELS,
  participantReason,
  type RuleParticipant,
  type Rules,
  type RuleTable,
  rulesMarkdown,
  scopeLabel,
} from "../lib/rules";
import { plural } from "../lib/utils";

/** Quantos itens cada lista mostra antes do "Mostrar mais" (o e-cidade real tem milhares). */
export const PAGE_ITEMS = 200;

export function RulesPage() {
  const id = useParams().id ?? "";
  const entry = keyOf(id);
  const [params, setParams] = useSearchParams();
  const depth = Number(params.get("profundidade")) || DEFAULT_DEPTH;
  const [filters, setFilters] = useState<Filters>({ text: "", category: null });

  const query = useQuery({
    queryKey: ["businessRules", entry, depth],
    queryFn: () => request(BusinessRulesDocument, { entry, depth }),
    enabled: entry.length > 0,
    placeholderData: keepPreviousData,
    meta: { silent: true }, // o erro (entrada inexistente, banco antigo) aparece na página
  });
  const rules = query.data?.businessRules;
  const shown = useMemo(() => (rules ? applyFilters(rules, filters) : null), [rules, filters]);

  function setDepth(value: number) {
    const next = new URLSearchParams(params);
    if (value === DEFAULT_DEPTH) next.delete("profundidade");
    else next.set("profundidade", String(value));
    setParams(next);
  }

  async function copyMarkdown() {
    if (!shown) return;
    try {
      await navigator.clipboard.writeText(rulesMarkdown(shown, filters));
      toast.success("Ficha copiada em Markdown");
    } catch {
      toast.error("Não foi possível copiar: o navegador bloqueou a área de transferência");
    }
  }

  return (
    <div className="space-y-5">
      <header className="space-y-2">
        <p className="text-sm text-muted-foreground">
          <Link to="/regras" className="hover:underline">
            Regras de negócio
          </Link>
        </p>
        <h1 className="break-all text-2xl font-semibold">{shortName(`File:${entry}`)}</h1>
        <p className="break-all font-mono text-xs text-muted-foreground">{entry}</p>
        <div className="flex flex-wrap items-center gap-2 pt-1">
          <label className="flex items-center gap-2 text-sm">
            Profundidade
            <select
              aria-label="Profundidade do fluxo"
              value={depth}
              onChange={(event) => setDepth(Number(event.target.value))}
              className="h-8 rounded-md border border-input bg-card px-2"
            >
              {DEPTHS.map((value) => (
                <option key={value} value={value}>
                  {value}
                  {value === DEFAULT_DEPTH ? " (padrão)" : ""}
                </option>
              ))}
            </select>
          </label>
          <Button variant="outline" size="sm" onClick={() => void copyMarkdown()} disabled={!shown}>
            <ClipboardCopy className="h-4 w-4" aria-hidden="true" /> Copiar como Markdown
          </Button>
          <Link
            className="inline-flex h-8 items-center gap-1.5 rounded-md border border-input px-3 text-sm hover:bg-muted"
            to={`/trace?entrada=${encodeURIComponent(entry)}`}
          >
            <GitBranch className="h-4 w-4" aria-hidden="true" /> Ver trace
          </Link>
          <Link className="text-sm text-primary hover:underline" to={nodeHref(`File:${entry}`)}>
            Detalhe do arquivo
          </Link>
        </div>
      </header>

      {query.isPending ? (
        <LoadingRows rows={10} label="Montando a ficha" />
      ) : query.isError ? (
        <Notice tone="warning">{query.error.message}</Notice>
      ) : rules && shown ? (
        <RulesSheet rules={rules} shown={shown} filters={filters} setFilters={setFilters} loading={query.isFetching} />
      ) : null}
    </div>
  );
}

function RulesSheet({
  rules,
  shown,
  filters,
  setFilters,
  loading,
}: {
  rules: Rules;
  shown: Rules;
  filters: Filters;
  setFilters: (filters: Filters) => void;
  loading: boolean;
}) {
  const s = rules.stats;
  const counts = countByCategory(applyFilters(rules, { ...filters, category: null }).messages);
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  return (
    <div className="space-y-5" aria-busy={loading}>
      <p className="text-sm text-muted-foreground" data-testid="resumo-ficha">
        Fluxo com profundidade {rules.depth} e confiança ≥ {rules.minConfidence.toLocaleString("pt-BR")}, sem
        infraestrutura: {plural(s.files, "arquivo")}, {plural(s.methods, "método ou função", "métodos ou funções")},{" "}
        {plural(s.tables, "tabela")}, {plural(s.messages, "mensagem", "mensagens")}.
        {rules.modules.length ? ` Módulo: ${rules.modules.map((m) => m.name).join(", ")}.` : ""}
      </p>
      {s.truncated ? (
        <Notice tone="warning">
          <TriangleAlert className="mr-1 inline h-4 w-4" aria-hidden="true" />O fluxo foi cortado no limite de nós:
          diminua a profundidade para ver o que está mais perto da tela.
        </Notice>
      ) : null}
      {rules.clamped.length ? <Notice>Parâmetros cortados: {rules.clamped.join(", ")}.</Notice> : null}

      <div className="sticky top-14 z-20 flex flex-wrap items-center gap-2 rounded-lg border bg-background/95 px-3 py-2 backdrop-blur">
        <label className="flex min-w-60 flex-1 items-center gap-2 text-sm">
          <SearchIcon className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          <span className="sr-only">Filtrar na ficha</span>
          <input
            value={filters.text}
            onChange={(event) => setFilters({ ...filters, text: event.target.value })}
            placeholder="Filtrar na ficha (texto, arquivo, tabela, coluna)"
            className="h-8 w-full rounded-md border border-input bg-card px-2"
          />
        </label>
        <div role="group" aria-label="Categoria das mensagens" className="flex flex-wrap gap-1">
          <CategoryChip active={filters.category === null} onClick={() => setFilters({ ...filters, category: null })}>
            Todas ({total})
          </CategoryChip>
          {CATEGORIES.map((category) => (
            <CategoryChip
              key={category}
              category={category}
              active={filters.category === category}
              onClick={() => setFilters({ ...filters, category: filters.category === category ? null : category })}
            >
              {category} ({counts[category] ?? 0})
            </CategoryChip>
          ))}
        </div>
      </div>

      <Section title="O que a tela faz" count={rules.menus.length + rules.actions.length}>
        <WhatItDoes rules={shown} />
      </Section>
      <Section
        title="Regras e validações"
        count={shown.messages.length}
        description="Mensagens do código que roda no fluxo, por arquivo e método. A categoria é uma classificação heurística por palavra-chave."
      >
        <Messages rules={shown} />
      </Section>
      <Section title="Dados que altera" count={shown.writes.length}>
        <Tables tables={shown.writes} empty="O fluxo não grava em nenhuma tabela." />
      </Section>
      <Section title="Dados que consulta" count={shown.reads.length}>
        <Tables tables={shown.reads} empty="O fluxo não lê nenhuma tabela." compact />
      </Section>
      <Section
        title="Regras no banco"
        count={shown.rpc.length + shown.triggers.length}
        description="Funções do banco chamadas pelo código e triggers das tabelas gravadas."
      >
        <Database rules={shown} />
      </Section>
      <Section
        title="Arquivos envolvidos"
        count={shown.participants.filter((p) => p.label === "File").length}
        description="Na ordem do fluxo, com o nível e o motivo de cada um estar aqui."
      >
        <Participants participants={shown.participants} />
      </Section>
      <Section
        title="Anotações do código"
        count={shown.notes.length}
        description="Comentários dos mesmos trechos, com pelo menos 4 palavras. Podem ser código comentado."
      >
        <Notes rules={shown} />
      </Section>
      {hasFilter(filters) ? (
        <p className="text-xs text-muted-foreground">O filtro vale também para o Markdown copiado.</p>
      ) : null}
    </div>
  );
}

// --------------------------------------------------------------------------- peças

function CategoryChip({
  category,
  active,
  onClick,
  children,
}: {
  category?: Category;
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  const color = category ? CATEGORY_STYLE[category] : undefined;
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`rounded-full border px-2.5 py-0.5 text-xs font-medium ${active ? "bg-accent text-accent-foreground" : "hover:bg-muted"}`}
      style={color ? { color: active ? undefined : color, borderColor: color } : undefined}
    >
      {children}
    </button>
  );
}

function CategoryBadge({ category }: { category: string }) {
  const color = CATEGORY_STYLE[category] ?? "var(--muted-foreground)";
  return (
    <Badge style={{ color, borderColor: color }} title="Classificação heurística por palavra-chave">
      {category}
    </Badge>
  );
}

function Section({
  title,
  count,
  description,
  children,
}: {
  title: string;
  count: number;
  description?: string;
  children: ReactNode;
}) {
  return (
    <Card aria-label={title}>
      <CardHeader
        title={
          <>
            {title} <span className="font-normal text-muted-foreground">({count.toLocaleString("pt-BR")})</span>
          </>
        }
        description={description}
      />
      {children}
    </Card>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="px-5 py-4 text-sm text-muted-foreground">{children}</p>;
}

function useLimit<T>(items: T[]): { visible: T[]; more: ReactNode } {
  const [limit, setLimit] = useState(PAGE_ITEMS);
  const visible = items.slice(0, limit);
  const more =
    items.length > limit ? (
      <div className="border-t px-5 py-2">
        <Button variant="ghost" size="sm" onClick={() => setLimit(limit + PAGE_ITEMS)}>
          Mostrar mais ({(items.length - limit).toLocaleString("pt-BR")} restantes)
        </Button>
      </div>
    ) : null;
  return { visible, more };
}

// --------------------------------------------------------------------------- seções

function WhatItDoes({ rules }: { rules: Rules }) {
  if (!rules.menus.length && !rules.actions.length) {
    return <Empty>Nenhum item de menu abre esta tela e ela não despacha ações.</Empty>;
  }
  return (
    <div className="divide-y">
      {rules.menus.map((menu) => (
        <div key={menu.id} className="space-y-1 px-5 py-3 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <NodeLink id={menu.id} name={menu.path ?? keyOf(menu.id)} className="font-medium" />
            {menu.active ? null : <Badge className="text-muted-foreground">inativo</Badge>}
          </div>
          {menu.paths.length > 1 ? (
            <p className="text-xs text-muted-foreground">Também em: {menu.paths.slice(1).join(" · ")}</p>
          ) : null}
          {menu.help && menu.help !== menu.path ? <p>{menu.help}</p> : null}
          {menu.desctec && menu.desctec !== menu.help ? (
            <p className="text-muted-foreground">Descrição técnica: {menu.desctec}</p>
          ) : null}
          {menu.params ? (
            <p className="text-xs">
              Parâmetros: <code className="font-mono">{menu.params}</code>
            </p>
          ) : null}
        </div>
      ))}
      {rules.actions.map((action) => (
        <div key={action.file} className="space-y-1 px-5 py-3 text-sm">
          <p>
            Ações despachadas por <EvidenceLink path={action.file} />
          </p>
          <div className="flex flex-wrap gap-1">
            {action.cases.map((name) => (
              <code key={name} className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
                {name}
              </code>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function Messages({ rules }: { rules: Rules }) {
  const { visible, more } = useLimit(rules.messages);
  if (!rules.messages.length) return <Empty>Nenhuma mensagem com este filtro.</Empty>;
  return (
    <>
      <div className="divide-y">
        {groupByFile(visible).map((group) => (
          <section key={group.file} aria-label={`Mensagens de ${group.file}`} className="px-5 py-3">
            <h3 className="mb-1 break-all font-mono text-xs font-semibold">{group.file}</h3>
            {group.scopes.map((scope) => (
              <div key={scope.scope} className="mb-2">
                <p className="text-xs text-muted-foreground">{scopeLabel(scope.scope)}</p>
                <ul className="space-y-1">
                  {scope.items.map((m) => (
                    <li key={`${m.line}-${m.text}`} className="flex flex-wrap items-center gap-2 text-sm">
                      <CategoryBadge category={m.category} />
                      <span className="min-w-0 break-words">{m.text}</span>
                      <EvidenceLink path={m.file} line={m.line} />
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </section>
        ))}
      </div>
      {more}
    </>
  );
}

function Tables({ tables, empty, compact }: { tables: RuleTable[]; empty: string; compact?: boolean }) {
  const { visible, more } = useLimit(tables);
  if (!tables.length) return <Empty>{empty}</Empty>;
  return (
    <>
      <ul className="divide-y">
        {visible.map((t) => {
          const description = catalogText(t.catalog, "description");
          const labeled = t.columnsLabeled.filter((c) => c.label);
          return (
            <li key={t.table} className="space-y-1 px-5 py-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <NodeLink id={t.table} className="font-medium" />
                {t.operations.map((op) => (
                  <Badge key={op} style={{ color: "var(--node-table)", borderColor: "var(--node-table)" }}>
                    {OPERATION_LABELS[op] ?? op}
                  </Badge>
                ))}
                {description ? <span className="text-muted-foreground">{description}</span> : null}
                <Link
                  className="ml-auto inline-flex items-center gap-1 text-xs text-primary hover:underline"
                  to={`/impacto?tabela=${encodeURIComponent(keyOf(t.table))}`}
                >
                  <Radar className="h-3.5 w-3.5" aria-hidden="true" /> Ver impacto
                </Link>
              </div>
              {labeled.length && !compact ? (
                <details>
                  <summary className="cursor-pointer text-xs text-muted-foreground">
                    Colunas com rótulo no dicionário ({labeled.length})
                  </summary>
                  <ul className="mt-1 grid gap-1 sm:grid-cols-2">
                    {labeled.map((c) => (
                      <li key={c.column} title={c.description ?? undefined}>
                        <code className="font-mono text-xs">{c.column}</code>
                        <span className="text-muted-foreground"> — {c.label}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}
              <div className="flex flex-wrap gap-x-3">
                {t.evidence.map((e) => (
                  <EvidenceLink key={`${e.path}:${e.line}`} path={e.path} line={e.line} />
                ))}
                {t.occurrences > t.evidence.length ? (
                  <span className="text-xs text-muted-foreground">
                    ×{t.occurrences.toLocaleString("pt-BR")} no código
                  </span>
                ) : null}
              </div>
            </li>
          );
        })}
      </ul>
      {more}
    </>
  );
}

function Database({ rules }: { rules: Rules }) {
  if (!rules.rpc.length && !rules.triggers.length) {
    return <Empty>O fluxo não chama funções do banco e as tabelas gravadas não têm triggers conhecidas.</Empty>;
  }
  return (
    <ul className="divide-y">
      {rules.rpc.map((r) => (
        <li key={`${r.function}-${r.evidence.from}-${r.evidence.line}`} className="flex flex-wrap items-center gap-2 px-5 py-2 text-sm">
          <LabelBadge label={r.resolved ? "PgFunction" : "RpcCall"} />
          <NodeLink id={r.function} name={r.name} className="font-medium" />
          <span className="text-muted-foreground">chamada por {shortName(r.evidence.from)}</span>
          <EvidenceLink path={r.evidence.path} line={r.evidence.line} />
        </li>
      ))}
      {rules.triggers.map((t) => (
        <li key={`${t.table}-${t.function}`} className="flex flex-wrap items-center gap-2 px-5 py-2 text-sm">
          <Badge style={{ color: "var(--node-pgfunction)", borderColor: "var(--node-pgfunction)" }}>trigger</Badge>
          <NodeLink id={t.table} />
          <span className="text-muted-foreground">dispara</span>
          <NodeLink id={t.function} className="font-medium" />
          {t.evidence ? <EvidenceLink path={t.evidence.path} line={t.evidence.line} /> : null}
        </li>
      ))}
    </ul>
  );
}

function evidenceOf(p: RuleParticipant): { path?: unknown; line?: unknown } {
  return (p.evidence ?? {}) as { path?: unknown; line?: unknown };
}

function Participants({ participants }: { participants: RuleParticipant[] }) {
  const files = participants.filter((p) => p.label === "File");
  const code = participants.filter((p) => p.label !== "File");
  const { visible, more } = useLimit(files);
  return (
    <>
      {files.length ? (
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-5 py-2 font-medium">Nível</th>
              <th className="px-2 py-2 font-medium">Arquivo</th>
              <th className="px-2 py-2 font-medium">Motivo</th>
              <th className="px-5 py-2 font-medium">Evidência</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {visible.map((p) => {
              const evidence = evidenceOf(p);
              return (
                <tr key={p.node}>
                  <td className="px-5 py-1.5 tabular-nums">{p.depth}</td>
                  <td className="max-w-md px-2 py-1.5">
                    <NodeLink id={p.node} name={keyOf(p.node)} />
                  </td>
                  <td className="px-2 py-1.5 text-muted-foreground">{participantReason(p)}</td>
                  <td className="px-5 py-1.5">
                    {evidence.path ? <EvidenceLink path={evidence.path} line={evidence.line} /> : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <Empty>Nenhum arquivo com este filtro.</Empty>
      )}
      {more}
      {code.length ? (
        <details className="border-t px-5 py-2 text-sm">
          <summary className="cursor-pointer text-xs text-muted-foreground">
            Classes, métodos e funções no fluxo ({code.length})
          </summary>
          <ul className="mt-2 space-y-1">
            {code.map((p) => (
              <li key={p.node} className="flex flex-wrap items-center gap-2">
                <span className="text-xs tabular-nums text-muted-foreground">{p.depth}</span>
                <span className="text-xs" style={{ color: nodeKind(p.label).color }}>
                  {nodeKind(p.label).singular}
                </span>
                <NodeLink id={p.node} />
                <span className="text-xs text-muted-foreground">{participantReason(p)}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </>
  );
}

function Notes({ rules }: { rules: Rules }) {
  const { visible, more } = useLimit(rules.notes);
  if (!rules.notes.length) return <Empty>Nenhuma anotação com este filtro.</Empty>;
  return (
    <>
      <ul className="divide-y">
        {visible.map((n) => (
          <li key={`${n.file}:${n.line}:${n.text}`} className="flex flex-wrap items-center gap-2 px-5 py-2 text-sm">
            <span className="min-w-0 flex-1 break-words">{n.text}</span>
            <span className="text-xs text-muted-foreground">{scopeLabel(n.scope)}</span>
            <EvidenceLink path={n.file} line={n.line} />
          </li>
        ))}
      </ul>
      {more}
    </>
  );
}
