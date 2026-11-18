import { useQuery } from "@tanstack/react-query";
import { Copy, ScrollText, Search as SearchIcon } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { LabelBadge, NodeLink } from "../components/NodeBits";
import { Button, Card, LoadingRows, Notice } from "../components/ui";
import { graphql } from "../graphql/generated";
import { request } from "../lib/api";
import { rulesHref } from "../lib/ids";
import { LABEL_FILTERS, nodeKind } from "../lib/labels";
import { useNodeNames } from "../lib/names";
import { renderSnippet } from "../lib/snippet";

const SearchQuery = graphql(`
  query SearchPage($text: String!, $label: String, $module: String, $anyTerm: Boolean!) {
    search(text: $text, label: $label, module: $module, limit: 50, anyTerm: $anyTerm) {
      nodeId
      label
      modules
      score
      field
      snippet
      nextCommand
    }
  }
`);

const ModulesQuery = graphql(`
  query ModuleNames {
    modules {
      module
      name
    }
  }
`);

export function SearchPage() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const label = params.get("label") || null;
  const module = params.get("module") || null;
  const anyTerm = params.get("any") === "1";
  const [draft, setDraft] = useState(q);

  const results = useQuery({
    queryKey: ["search", q, label, module, anyTerm],
    queryFn: () => request(SearchQuery, { text: q, label, module, anyTerm }),
    enabled: q.trim().length > 0,
    meta: { silent: true },
  });
  const modules = useQuery({
    queryKey: ["moduleNames"],
    queryFn: () => request(ModulesQuery),
    meta: { silent: true },
  });

  function update(changes: Record<string, string | null>) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setParams(next);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    update({ q: draft.trim() || null });
  }

  const hits = results.data?.search ?? [];
  const nameOf = useNodeNames(hits.map((hit) => hit.nodeId));
  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold">Busca</h1>
      <form onSubmit={submit} className="flex flex-wrap items-end gap-3" role="search">
        <label className="flex min-w-72 flex-1 flex-col gap-1 text-sm">
          Termos de negócio
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="anular empenho"
            className="h-9 rounded-md border border-input bg-card px-3"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Tipo
          <select
            value={label ?? ""}
            onChange={(event) => update({ label: event.target.value || null })}
            className="h-9 rounded-md border border-input bg-card px-2"
          >
            <option value="">Todos</option>
            {LABEL_FILTERS.map((value) => (
              <option key={value} value={value}>
                {nodeKind(value).plural}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Módulo
          <select
            value={module ?? ""}
            onChange={(event) => update({ module: event.target.value || null })}
            className="h-9 max-w-56 rounded-md border border-input bg-card px-2"
          >
            <option value="">Todos</option>
            {(modules.data?.modules ?? [])
              .filter((m) => m.module !== "(sem módulo)")
              .sort((a, b) => a.name.localeCompare(b.name, "pt-BR"))
              .map((m) => (
                <option key={m.module} value={m.name}>
                  {m.name}
                </option>
              ))}
          </select>
        </label>
        <label className="flex h-9 items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={anyTerm}
            onChange={(event) => update({ any: event.target.checked ? "1" : null })}
          />
          Qualquer termo
        </label>
        <Button type="submit">
          <SearchIcon className="h-4 w-4" aria-hidden="true" /> Buscar
        </Button>
      </form>

      {!q ? (
        <Notice>
          Busque por termos de negócio (“anular empenho”, “liquidação”) ou por nomes de arquivos e
          tabelas. Dica: <kbd className="font-mono">Ctrl K</kbd> abre a busca de qualquer página.
        </Notice>
      ) : results.isPending ? (
        <LoadingRows rows={6} label="Buscando" />
      ) : results.isError ? (
        <Notice tone="warning">{results.error.message}</Notice>
      ) : hits.length === 0 ? (
        <Notice>
          <p className="font-medium">Nada encontrado para “{q}”.</p>
          <ul className="mt-2 list-disc pl-5">
            <li>Tente sem preposições: “anular empenho” em vez de “anulação de empenho”.</li>
            <li>Marque “Qualquer termo” para aceitar resultados com só parte das palavras.</li>
            <li>Tire o filtro de tipo ou de módulo.</li>
          </ul>
        </Notice>
      ) : (
        <Card>
          <p className="border-b px-5 py-2 text-xs text-muted-foreground" aria-live="polite">
            {hits.length} resultado(s)
          </p>
          <ol className="divide-y">
            {hits.map((hit) => (
              <li key={hit.nodeId} className="space-y-1 px-5 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <LabelBadge label={hit.label} />
                  <NodeLink id={hit.nodeId} name={nameOf(hit.nodeId)} className="font-medium" />
                  {hit.modules.map((name) => (
                    <span key={name} className="text-xs text-muted-foreground">
                      {name}
                    </span>
                  ))}
                  {hit.label === "File" ? (
                    <Link
                      to={rulesHref(hit.nodeId)}
                      className="ml-auto inline-flex items-center gap-1 text-xs text-primary hover:underline"
                    >
                      <ScrollText className="h-3.5 w-3.5" aria-hidden="true" /> Ver regras
                    </Link>
                  ) : null}
                </div>
                <p className="text-sm text-muted-foreground" data-testid="snippet">
                  {renderSnippet(hit.snippet)}
                </p>
                {hit.nextCommand ? (
                  <div className="flex items-center gap-2 text-xs">
                    <code className="rounded bg-muted px-2 py-0.5 font-mono">{hit.nextCommand}</code>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => void navigator.clipboard?.writeText(hit.nextCommand)}
                      aria-label={`Copiar o comando ${hit.nextCommand}`}
                    >
                      <Copy className="h-3.5 w-3.5" aria-hidden="true" /> Copiar
                    </Button>
                  </div>
                ) : null}
              </li>
            ))}
          </ol>
        </Card>
      )}
    </div>
  );
}
