import { useQuery } from "@tanstack/react-query";
import { Command } from "cmdk";
import { GitBranch, Radar, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { graphql } from "../graphql/generated";
import { request } from "../lib/api";
import { keyOf, nodeHref, shortName } from "../lib/ids";
import { useNodeNames } from "../lib/names";
import { nodeKind } from "../lib/labels";

const SuggestQuery = graphql(`
  query Suggest($prefix: String!) {
    suggest(prefix: $prefix, limit: 8) {
      nodeId
      label
      modules
    }
  }
`);

function useDebounced(value: string, delay = 200): string {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

const ITEM =
  "flex cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-sm " +
  "data-[selected=true]:bg-accent data-[selected=true]:text-accent-foreground";

/** Busca global: abre com Ctrl+K (⌘K no Mac) ou pelo botão do cabeçalho. */
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const term = useDebounced(text.trim());
  const navigate = useNavigate();

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((current) => !current);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const suggest = useQuery({
    queryKey: ["suggest", term],
    queryFn: () => request(SuggestQuery, { prefix: term }),
    enabled: open && term.length >= 2,
  });

  function go(path: string) {
    setOpen(false);
    setText("");
    void navigate(path);
  }

  const hits = suggest.data?.suggest ?? [];
  const nameOf = useNodeNames(hits.map((hit) => hit.nodeId));
  const first = hits[0]?.nodeId;
  // Quando chegam resultados, o primeiro fica selecionado (senão o cmdk mantém a seleção
  // anterior, "buscar em todos os resultados", e o Enter não abre o primeiro resultado).
  const [selected, setSelected] = useState("");
  const [seenFirst, setSeenFirst] = useState<string | undefined>(undefined);
  if (first !== seenFirst) {
    setSeenFirst(first);
    if (first) setSelected(first);
  }
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex h-9 w-full max-w-md items-center gap-2 rounded-md border border-input bg-card px-3 text-sm text-muted-foreground hover:bg-muted"
        aria-label="Buscar (Ctrl+K)"
      >
        <Search className="h-4 w-4" aria-hidden="true" />
        <span className="flex-1 text-left">Buscar telas, tabelas, menus…</span>
        <kbd className="rounded border bg-muted px-1.5 font-mono text-xs">Ctrl K</kbd>
      </button>
      <Command.Dialog
        open={open}
        onOpenChange={setOpen}
        label="Busca global"
        shouldFilter={false}
        value={selected}
        onValueChange={setSelected}
        overlayClassName="fixed inset-0 z-40 bg-black/40"
        contentClassName="fixed left-1/2 top-24 z-50 w-full max-w-xl -translate-x-1/2 rounded-xl border bg-card p-2 shadow-2xl"
      >
        <Command.Input
          value={text}
          onValueChange={setText}
          placeholder="Digite um nome: anular, empempenho, liquidação…"
          className="mb-2 h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
        />
        <Command.List className="max-h-96 overflow-y-auto">
          {term.length < 2 ? (
            <p className="px-3 py-6 text-center text-sm text-muted-foreground">
              Digite pelo menos 2 letras.
            </p>
          ) : null}
          {term.length >= 2 && suggest.isFetching && hits.length === 0 ? (
            <Command.Loading>
              <p className="px-3 py-2 text-sm text-muted-foreground">Buscando…</p>
            </Command.Loading>
          ) : null}
          {term.length >= 2 && !suggest.isFetching ? (
            <Command.Empty className="px-3 py-6 text-center text-sm text-muted-foreground">
              Nada encontrado para “{term}”.
            </Command.Empty>
          ) : null}
          {hits.length > 0 ? (
            <Command.Group heading="Resultados" className="text-xs text-muted-foreground">
              {hits.map((hit) => {
                const kind = nodeKind(hit.label);
                const Icon = kind.icon;
                return (
                  <Command.Item
                    key={hit.nodeId}
                    value={hit.nodeId}
                    onSelect={() => go(nodeHref(hit.nodeId))}
                    className={ITEM}
                  >
                    <Icon className="h-4 w-4 shrink-0" style={{ color: kind.color }} aria-hidden="true" />
                    <span className="truncate text-foreground">{nameOf(hit.nodeId)}</span>
                    <span className="ml-auto shrink-0 whitespace-nowrap text-xs text-muted-foreground">{kind.singular}</span>
                  </Command.Item>
                );
              })}
            </Command.Group>
          ) : null}
          {hits.length > 0 ? (
            <Command.Group heading="Atalhos" className="text-xs text-muted-foreground">
              {hits
                .filter((hit) => hit.label === "File" || hit.label === "Table")
                .slice(0, 3)
                .map((hit) =>
                  hit.label === "File" ? (
                    <Command.Item
                      key={`trace-${hit.nodeId}`}
                      value={`trace-${hit.nodeId}`}
                      onSelect={() => go(`/trace?entrada=${encodeURIComponent(keyOf(hit.nodeId))}`)}
                      className={ITEM}
                    >
                      <GitBranch className="h-4 w-4" aria-hidden="true" />
                      Ver trace de {shortName(hit.nodeId)}
                    </Command.Item>
                  ) : (
                    <Command.Item
                      key={`impacto-${hit.nodeId}`}
                      value={`impacto-${hit.nodeId}`}
                      onSelect={() => go(`/impacto?tabela=${encodeURIComponent(keyOf(hit.nodeId))}`)}
                      className={ITEM}
                    >
                      <Radar className="h-4 w-4" aria-hidden="true" />
                      Ver impacto de {shortName(hit.nodeId)}
                    </Command.Item>
                  ),
                )}
            </Command.Group>
          ) : null}
          {term.length >= 2 ? (
            <Command.Item
              value="busca-completa"
              onSelect={() => go(`/busca?q=${encodeURIComponent(term)}`)}
              className={ITEM}
            >
              <Search className="h-4 w-4" aria-hidden="true" />
              Buscar “{term}” em todos os resultados
            </Command.Item>
          ) : null}
        </Command.List>
      </Command.Dialog>
    </>
  );
}
