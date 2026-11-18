import { useQuery } from "@tanstack/react-query";
import { ScrollText, Search as SearchIcon } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { Badge, Button, Card, CardHeader, LoadingRows, Notice } from "../components/ui";
import { graphql } from "../graphql/generated";
import { request } from "../lib/api";
import { keyOf, rulesHref } from "../lib/ids";
import { renderSnippet } from "../lib/snippet";

const RulesSearchQuery = graphql(`
  query RulesSearch($text: String!) {
    search(text: $text, label: "File", limit: 20) {
      nodeId
      modules
      snippet
    }
  }
`);

const RulesScreensQuery = graphql(`
  query RulesScreens($module: String, $inMenu: Boolean) {
    modules {
      module
      name
    }
    screens(module: $module, inMenu: $inMenu, limit: 100) {
      fileId
      module
      inMenu
      menuItems
      tablesWritten
      tablesRead
    }
  }
`);

const MENU_FILTERS: Record<string, boolean | null> = { todas: null, sim: true, nao: false };

export function RulesIndexPage() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const module = params.get("modulo") || null;
  const menu = params.get("menu") ?? "todas";
  const [draft, setDraft] = useState(q);

  function update(changes: Record<string, string | null>) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setParams(next);
  }

  const found = useQuery({
    queryKey: ["rulesSearch", q],
    queryFn: () => request(RulesSearchQuery, { text: q }),
    enabled: q.trim().length > 0,
    meta: { silent: true },
  });
  const screens = useQuery({
    queryKey: ["rulesScreens", module, menu],
    queryFn: () => request(RulesScreensQuery, { module, inMenu: MENU_FILTERS[menu] ?? null }),
    meta: { silent: true }, // banco sem agregados: aviso na lista, sem toast
  });
  const names = new Map((screens.data?.modules ?? []).map((m) => [m.module, m.name]));

  function submit(event: FormEvent) {
    event.preventDefault();
    update({ q: draft.trim() || null });
  }

  return (
    <div className="space-y-5">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">Regras de negócio</h1>
        <p className="text-sm text-muted-foreground">
          Escolha uma tela para ver o que ela valida, grava, lê e calcula, com a evidência de cada item no código.
        </p>
      </header>

      <Card>
        <CardHeader title="Buscar uma tela" />
        <form onSubmit={submit} role="search" className="flex flex-wrap items-end gap-3 px-5 py-3">
          <label className="flex min-w-72 flex-1 flex-col gap-1 text-sm">
            Nome ou termos de negócio
            <input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="anular empenho"
              className="h-9 rounded-md border border-input bg-card px-3"
            />
          </label>
          <Button type="submit">
            <SearchIcon className="h-4 w-4" aria-hidden="true" /> Buscar
          </Button>
        </form>
        {!q ? null : found.isPending ? (
          <LoadingRows rows={3} label="Buscando" />
        ) : found.isError ? (
          <div className="px-5 pb-4">
            <Notice tone="warning">{found.error.message}</Notice>
          </div>
        ) : found.data.search.length === 0 ? (
          <p className="px-5 pb-4 text-sm text-muted-foreground">Nenhum arquivo para “{q}”.</p>
        ) : (
          <ol className="divide-y border-t" aria-label="Arquivos encontrados">
            {found.data.search.map((hit) => (
              <li key={hit.nodeId} className="space-y-0.5 px-5 py-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Link to={rulesHref(hit.nodeId)} className="inline-flex items-center gap-1.5 font-medium text-primary hover:underline">
                    <ScrollText className="h-4 w-4" aria-hidden="true" />
                    {keyOf(hit.nodeId)}
                  </Link>
                  {hit.modules.map((name) => (
                    <span key={name} className="text-xs text-muted-foreground">
                      {name}
                    </span>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">{renderSnippet(hit.snippet)}</p>
              </li>
            ))}
          </ol>
        )}
      </Card>

      <Card>
        <CardHeader
          title="Telas por módulo"
          description="As mais alcançadas por outras telas primeiro. “Fora do menu” são candidatas a código morto ou a telas abertas por outras."
        />
        <div className="flex flex-wrap gap-3 px-5 py-3">
          <label className="flex flex-col gap-1 text-sm">
            Módulo
            <select
              value={module ?? ""}
              onChange={(event) => update({ modulo: event.target.value || null })}
              className="h-9 max-w-64 rounded-md border border-input bg-card px-2"
            >
              <option value="">Todos</option>
              {[...(screens.data?.modules ?? [])]
                .sort((a, b) => a.name.localeCompare(b.name, "pt-BR"))
                .map((m) => (
                  <option key={m.module} value={m.module}>
                    {m.name}
                  </option>
                ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            No menu
            <select
              value={menu}
              onChange={(event) => update({ menu: event.target.value === "todas" ? null : event.target.value })}
              className="h-9 rounded-md border border-input bg-card px-2"
            >
              <option value="todas">Todas</option>
              <option value="sim">No menu</option>
              <option value="nao">Fora do menu</option>
            </select>
          </label>
        </div>
        {screens.isPending ? (
          <LoadingRows rows={6} label="Carregando as telas" />
        ) : screens.isError ? (
          <div className="px-5 pb-4">
            <Notice tone="warning">{screens.error.message}</Notice>
          </div>
        ) : screens.data.screens.length === 0 ? (
          <p className="px-5 pb-4 text-sm text-muted-foreground">Nenhuma tela com estes filtros.</p>
        ) : (
          <table className="w-full text-sm" aria-label="Telas">
            <thead className="border-t text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-5 py-2 font-medium">Tela</th>
                <th className="px-2 py-2 font-medium">Módulo</th>
                <th className="px-2 py-2 font-medium">Menu</th>
                <th className="px-5 py-2 text-right font-medium">Tabelas gravadas / lidas</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {screens.data.screens.map((s) => (
                <tr key={s.fileId}>
                  <td className="px-5 py-1.5">
                    <Link to={rulesHref(s.fileId)} className="font-mono text-xs text-primary hover:underline">
                      {keyOf(s.fileId)}
                    </Link>
                  </td>
                  <td className="px-2 py-1.5 text-muted-foreground">
                    {s.module
                      .split(",")
                      .map((id) => names.get(id) ?? keyOf(id))
                      .join(", ")}
                  </td>
                  <td className="px-2 py-1.5">
                    {s.inMenu ? (
                      <Badge>no menu</Badge>
                    ) : (
                      <Badge className="text-muted-foreground">fora do menu</Badge>
                    )}
                  </td>
                  <td className="px-5 py-1.5 text-right tabular-nums">
                    {s.tablesWritten} / {s.tablesRead}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
