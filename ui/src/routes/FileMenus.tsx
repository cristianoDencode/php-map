/** Menus por fonte: `/menus?fonte=&distancia=&modulo=&texto=`. */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Download, Layers, ListTree, ScrollText } from "lucide-react";
import { type FormEvent, type KeyboardEvent, useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { EvidenceLink } from "../app/code-panel";
import { NodeLink } from "../components/NodeBits";
import { Badge, Button, Card, CardHeader, LoadingRows, Notice } from "../components/ui";
import { request } from "../lib/api";
import {
  csvName,
  DEFAULT_DISTANCE,
  distanceChips,
  distanceLabel,
  type FileMenuItem,
  FileMenusDocument,
  FindFilesDocument,
  MAX_DISTANCE,
  menusCsv,
  parseDistance,
  screenCount,
  stepText,
  type ViaStep,
} from "../lib/file-menus";
import { keyOf, nodeHref, rulesHref } from "../lib/ids";
import { ModuleNameMapDocument } from "../lib/subgraph";
import { formatNumber } from "../lib/utils";

const PAGE = 100;
const EXAMPLES = ["forms/db_frmempanularempenho.php", "classes/db_empempenho_classe.php"];

function download(text: string, name: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/** Campo do fonte com o autocompletar do `findFiles` (setas, Enter e Esc). */
function FilePicker({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const [draft, setDraft] = useState(value);
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  useEffect(() => {
    const timer = setTimeout(() => setText(draft.trim()), 250);
    return () => clearTimeout(timer);
  }, [draft]);
  const found = useQuery({
    queryKey: ["findFiles", text],
    queryFn: () => request(FindFilesDocument, { text }),
    enabled: open && text.length >= 2 && text !== value,
    meta: { silent: true },
  });
  const items = open ? (found.data?.findFiles ?? []) : [];

  function choose(next: string) {
    setDraft(next);
    setOpen(false);
    setActive(-1);
    onChange(next);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const picked = active >= 0 ? items[active] : undefined;
    if (picked) choose(keyOf(picked));
    else if (draft.trim()) choose(draft.trim());
  }

  function keyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown" && items.length) {
      event.preventDefault();
      setActive((i) => (i + 1) % items.length);
    } else if (event.key === "ArrowUp" && items.length) {
      event.preventDefault();
      setActive((i) => (i <= 0 ? items.length - 1 : i - 1));
    } else if (event.key === "Escape") {
      setOpen(false);
      setActive(-1);
    }
  }

  return (
    <form onSubmit={submit} className="relative flex min-w-72 flex-1 items-end gap-2" role="search">
      <label className="flex flex-1 flex-col gap-1 text-sm">
        Fonte
        <input
          value={draft}
          onChange={(event) => {
            setDraft(event.target.value);
            setOpen(true);
            setActive(-1);
          }}
          onKeyDown={keyDown}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          placeholder="nome do arquivo ou trecho do caminho (ex.: db_frmempanularempenho)"
          role="combobox"
          aria-expanded={items.length > 0}
          aria-autocomplete="list"
          aria-controls="sugestoes-fonte"
          aria-activedescendant={active >= 0 ? `sugestao-fonte-${active}` : undefined}
          className="h-9 rounded-md border border-input bg-card px-3 font-mono text-sm"
        />
      </label>
      <Button type="submit">
        <ListTree className="h-4 w-4" aria-hidden="true" /> Listar menus
      </Button>
      {items.length ? (
        <ul
          id="sugestoes-fonte"
          role="listbox"
          aria-label="Arquivos"
          className="absolute top-full z-30 mt-1 max-h-72 w-full overflow-auto rounded-md border bg-card shadow-lg"
        >
          {items.map((id, index) => (
            <li
              key={id}
              id={`sugestao-fonte-${index}`}
              role="option"
              aria-selected={index === active}
              className={index === active ? "bg-muted" : undefined}
            >
              <button
                type="button"
                className="w-full truncate px-3 py-1.5 text-left font-mono text-xs hover:bg-muted"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => choose(keyOf(id))}
              >
                {keyOf(id)}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </form>
  );
}

/** Caminho do arquivo de entrada até o fonte; com mais de 3 passos, recolhido. */
function ViaChain({ via }: { via: ViaStep[] }) {
  const [expanded, setExpanded] = useState(false);
  const steps = via.slice(1);
  const hidden = !expanded && steps.length > 3 ? steps.length - 2 : 0;
  const shown = hidden ? steps.slice(-2) : steps;
  if (steps.length === 0) return <span className="text-xs text-muted-foreground">é a entrada do menu</span>;
  return (
    <ol className="space-y-0.5 text-xs" aria-label="Como chega">
      {hidden ? (
        <li>
          <button type="button" className="text-primary hover:underline" onClick={() => setExpanded(true)}>
            +{hidden} passos
          </button>
        </li>
      ) : null}
      {shown.map((step, index) => (
        <li key={`${step.node}-${index}`} className="flex flex-wrap items-center gap-1">
          <span>{stepText(step)}</span>
          {step.path ? <EvidenceLink path={step.path} line={step.line} /> : null}
        </li>
      ))}
    </ol>
  );
}

function MenuRow({ item }: { item: FileMenuItem }) {
  return (
    <tr className="align-top">
      <td className="px-5 py-2">
        {item.paths.length ? (
          <ul className="space-y-0.5">
            {item.paths.map((path) => (
              <li key={path}>{path}</li>
            ))}
          </ul>
        ) : (
          <span>
            {item.name ?? keyOf(item.menu)} <span className="text-xs text-muted-foreground">(sem caminho no menu)</span>
          </span>
        )}
        <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-muted-foreground">{keyOf(item.menu)}</span>
          {item.active ? null : <Badge className="border-warning text-warning">inativo</Badge>}
          {item.params ? <code className="rounded bg-muted px-1 text-xs">{item.params}</code> : null}
        </div>
      </td>
      <td className="px-2 py-2 text-muted-foreground">{item.modules.map((m) => m.name).join(", ") || "—"}</td>
      <td className="px-2 py-2">
        <div className="flex flex-col gap-0.5">
          <NodeLink id={item.entry} className="font-mono text-xs" />
          <span className="flex flex-wrap items-center gap-2">
            <Link
              to={rulesHref(item.entry)}
              className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
            >
              <ScrollText className="h-3 w-3" aria-hidden="true" /> regras
            </Link>
            <EvidenceLink path={keyOf(item.entry)} />
          </span>
        </div>
      </td>
      <td className="px-2 py-2 text-center tabular-nums">{item.distance}</td>
      <td className="px-5 py-2">
        <ViaChain via={item.via} />
      </td>
    </tr>
  );
}

export function FileMenusPage() {
  const [params, setParams] = useSearchParams();
  const fonte = params.get("fonte") ?? "";
  const distance = parseDistance(params.get("distancia"));
  const module = params.get("modulo") || null;
  const text = params.get("texto") ?? "";

  const update = useCallback(
    (changes: Record<string, string | null>, replace = false) => {
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          for (const [key, value] of Object.entries(changes)) {
            if (value) next.set(key, value);
            else next.delete(key);
          }
          return next;
        },
        { replace },
      );
    },
    [setParams],
  );

  const modules = useQuery({
    queryKey: ["moduleNameMap"],
    queryFn: () => request(ModuleNameMapDocument),
    meta: { silent: true },
    staleTime: Infinity,
  });
  const query = useQuery({
    queryKey: ["fileMenus", fonte, distance, module, text],
    queryFn: () =>
      request(FileMenusDocument, {
        file: fonte,
        maxDistance: distance,
        module,
        text: text || null,
      }),
    enabled: fonte.length > 0,
    placeholderData: keepPreviousData,
    meta: { silent: true },
  });
  const data = query.data?.menusForFile ?? null;
  // Nome parcial resolvido: a URL passa a ter o caminho completo (link estável, sem histórico).
  const resolved = data && !query.isPlaceholderData && data.file ? keyOf(data.file) : null;
  useEffect(() => {
    if (resolved && resolved !== fonte) update({ fonte: resolved }, true);
  }, [resolved, fonte, update]);

  const items = data?.items ?? [];
  const filtered = Boolean(module || text);

  return (
    <div className="space-y-5">
      <header className="space-y-3">
        <h1 className="text-2xl font-semibold">Menus por fonte</h1>
        <p className="text-sm text-muted-foreground">
          Os itens de menu do e-cidade que levam a um fonte: os que abrem o próprio arquivo e os que chegam a ele pelo
          código (includes, chamadas, classes), com a distância e o caminho de cada um.
        </p>
        <FilePicker key={fonte} value={fonte} onChange={(value) => update({ fonte: value })} />
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Distância máxima
            <select
              value={distance}
              onChange={(event) => {
                const value = Number(event.target.value);
                update({
                  distancia: value === DEFAULT_DISTANCE ? null : String(value),
                });
              }}
              className="h-9 rounded-md border border-input bg-card px-2"
            >
              {Array.from({ length: MAX_DISTANCE + 1 }, (_, d) => (
                <option key={d} value={d}>
                  {distanceLabel(d)}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Módulo
            <select
              value={module ?? ""}
              onChange={(event) => update({ modulo: event.target.value || null })}
              className="h-9 max-w-64 rounded-md border border-input bg-card px-2"
            >
              <option value="">Todos</option>
              {[...(modules.data?.modules ?? [])]
                .sort((a, b) => a.name.localeCompare(b.name, "pt-BR"))
                .map((m) => (
                  <option key={m.module} value={m.module}>
                    {m.name}
                  </option>
                ))}
            </select>
          </label>
          <TextFilter key={text} value={text} onChange={(value) => update({ texto: value || null })} />
        </div>
      </header>

      {!fonte ? (
        <Notice>
          Digite o nome de um arquivo PHP. Exemplos:{" "}
          {EXAMPLES.map((example, index) => (
            <span key={example}>
              {index ? " e " : null}
              <button
                type="button"
                className="font-mono text-primary hover:underline"
                onClick={() => update({ fonte: example })}
              >
                {example}
              </button>
            </span>
          ))}
          .
        </Notice>
      ) : query.isPending ? (
        <LoadingRows rows={6} label="Procurando os menus" />
      ) : query.isError ? (
        <Notice tone="warning">{query.error.message}</Notice>
      ) : data && !data.file && data.candidates.length ? (
        <Card>
          <CardHeader title="Mais de um arquivo com esse nome: escolha um" />
          <ul className="divide-y" aria-label="Arquivos candidatos">
            {data.candidates.map((id) => (
              <li key={id}>
                <button
                  type="button"
                  className="w-full px-5 py-2 text-left font-mono text-xs text-primary hover:bg-muted"
                  onClick={() => update({ fonte: keyOf(id) })}
                >
                  {keyOf(id)}
                </button>
              </li>
            ))}
          </ul>
        </Card>
      ) : data && !data.file ? (
        <Notice tone="warning">Nenhum fonte com esse nome no grafo: {fonte}</Notice>
      ) : data ? (
        <Card>
          <CardHeader
            title={
              <span aria-live="polite">
                {formatNumber(data.total)} {data.total === 1 ? "item de menu" : "itens de menu"} em{" "}
                {formatNumber(screenCount(items))} {screenCount(items) === 1 ? "tela" : "telas"}
                {filtered && data.total !== data.unfiltered ? ` (de ${formatNumber(data.unfiltered)})` : ""}
              </span>
            }
            description={
              <span className="flex flex-wrap gap-1.5 pt-1" aria-label="Por distância">
                {distanceChips(data.byDistance).map((chip) => (
                  <Badge key={chip.label}>
                    {chip.label}: {formatNumber(chip.count)}
                  </Badge>
                ))}
              </span>
            }
            action={
              <div className="flex flex-wrap gap-2">
                {data.file ? (
                  <Link
                    className="inline-flex h-8 items-center gap-1.5 rounded-md border border-input px-3 text-sm hover:bg-muted"
                    to={`${nodeHref(data.file)}?aba=camadas`}
                  >
                    <Layers className="h-4 w-4" aria-hidden="true" /> Ver camadas
                  </Link>
                ) : null}
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!items.length}
                  onClick={() => download(menusCsv(items), csvName(data.file ?? fonte))}
                >
                  <Download className="h-4 w-4" aria-hidden="true" /> Exportar CSV
                </Button>
              </div>
            }
          />
          {data.truncated ? (
            <div className="px-5 pt-3">
              <Notice tone="warning">
                A busca parou no limite de nós: diminua a distância para ver a lista completa.
              </Notice>
            </div>
          ) : null}
          {items.length === 0 ? (
            <div className="space-y-1 px-5 py-4 text-sm text-muted-foreground">
              <p>
                {filtered && data.unfiltered
                  ? "Nenhum menu com estes filtros."
                  : `Nenhum menu chega a este fonte até ${data.maxDistance} ${data.maxDistance === 1 ? "salto" : "saltos"}${data.maxDistance < MAX_DISTANCE ? ": aumente a distância." : "."}`}
              </p>
              <p className="text-xs">
                Um fonte chamado só por JavaScript ou RPC que o mapeamento não resolveu fica sem menus no grafo.
              </p>
            </div>
          ) : (
            <MenuTable key={`${fonte}|${distance}|${module}|${text}`} items={items} />
          )}
        </Card>
      ) : null}
    </div>
  );
}

/** Texto no caminho do menu: vai para a URL no Enter ou ao sair do campo. */
function TextFilter({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const [draft, setDraft] = useState(value);
  const commit = () => {
    if (draft.trim() !== value) onChange(draft.trim());
  };
  return (
    <label className="flex min-w-56 flex-col gap-1 text-sm">
      Texto no caminho do menu
      <input
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") commit();
        }}
        placeholder="ex.: Procedimentos"
        className="h-9 rounded-md border border-input bg-card px-3"
      />
    </label>
  );
}

/** A lista, de 100 em 100 (a paginação recomeça quando a consulta muda: `key`). */
function MenuTable({ items }: { items: FileMenuItem[] }) {
  const [limit, setLimit] = useState(PAGE);
  return (
    <>
      <table className="w-full text-sm" aria-label="Menus do fonte">
        <thead className="text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-5 py-2 font-medium">Caminho do menu</th>
            <th className="px-2 py-2 font-medium">Módulo</th>
            <th className="px-2 py-2 font-medium">Tela de entrada</th>
            <th className="px-2 py-2 text-center font-medium">Distância</th>
            <th className="px-5 py-2 font-medium">Como chega</th>
          </tr>
        </thead>
        <tbody className="divide-y border-t">
          {items.slice(0, limit).map((item) => (
            <MenuRow key={`${item.menu}|${item.entry}`} item={item} />
          ))}
        </tbody>
      </table>
      {items.length > limit ? (
        <div className="border-t px-5 py-2">
          <Button variant="outline" size="sm" onClick={() => setLimit((n) => n + PAGE)}>
            Mostrar mais {formatNumber(Math.min(PAGE, items.length - limit))} de {formatNumber(items.length - limit)}
          </Button>
        </div>
      ) : null}
    </>
  );
}
