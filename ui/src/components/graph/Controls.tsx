/** Controles comuns do impacto e do trace: alvo com sugestões e filtros. */
import { useQuery } from "@tanstack/react-query";
import { Search as SearchIcon } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { request } from "../../lib/api";
import { OPERATIONS, type Operation } from "../../lib/graph";
import { MAX_DEPTH, MAX_NODES, type ViewParams } from "../../lib/graph-url";
import { keyOf } from "../../lib/ids";
import { OPERATION_LABELS } from "../../lib/rules";
import { TargetSuggestDocument } from "../../lib/subgraph";
import { Button } from "../ui";

/**
 * Campo do alvo (tabela ou tela) com as sugestões do `suggest` filtradas pelo tipo. O rascunho
 * recomeça quando o valor da URL muda: quem usa passa `key={value}`.
 */
export function TargetPicker({
  label,
  kind,
  value,
  placeholder,
  onChange,
}: {
  label: string;
  kind: "Table" | "File";
  value: string;
  placeholder: string;
  onChange: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  const [prefix, setPrefix] = useState("");
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setPrefix(draft.trim()), 200);
    return () => clearTimeout(timer);
  }, [draft]);
  const suggestions = useQuery({
    queryKey: ["targetSuggest", kind, prefix],
    queryFn: () => request(TargetSuggestDocument, { prefix, label: kind }),
    enabled: open && prefix.length >= 2 && prefix !== value,
    meta: { silent: true },
  });
  const items = open ? (suggestions.data?.suggest ?? []) : [];

  function submit(event: FormEvent) {
    event.preventDefault();
    setOpen(false);
    if (draft.trim()) onChange(draft.trim());
  }

  return (
    <form onSubmit={submit} className="relative flex min-w-72 flex-1 items-end gap-2" role="search">
      <label className="flex flex-1 flex-col gap-1 text-sm">
        {label}
        <input
          value={draft}
          onChange={(event) => {
            setDraft(event.target.value);
            setOpen(true);
          }}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          placeholder={placeholder}
          aria-autocomplete="list"
          aria-controls="sugestoes-alvo"
          className="h-9 rounded-md border border-input bg-card px-3"
        />
      </label>
      <Button type="submit">
        <SearchIcon className="h-4 w-4" aria-hidden="true" /> Abrir
      </Button>
      {items.length ? (
        <ul
          id="sugestoes-alvo"
          role="listbox"
          aria-label="Sugestões"
          className="absolute top-full z-30 mt-1 max-h-72 w-full overflow-auto rounded-md border bg-card shadow-lg"
        >
          {items.map((item) => (
            <li key={item.nodeId} role="option" aria-selected={false}>
              <button
                type="button"
                className="flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-sm hover:bg-muted"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  const key = keyOf(item.nodeId);
                  setDraft(key);
                  setOpen(false);
                  onChange(key);
                }}
              >
                <span className="truncate font-mono text-xs">{keyOf(item.nodeId)}</span>
                <span className="text-xs text-muted-foreground">{item.modules.join(", ")}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </form>
  );
}

/** Filtros: os da API (profundidade, confiança, infraestrutura, limite) e os do navegador. */
export function FilterBar({
  view,
  modules,
  moduleName,
  showPgFunctions,
  onChange,
}: {
  view: ViewParams;
  modules: string[];
  moduleName: (id: string) => string;
  showPgFunctions?: boolean;
  onChange: (changes: Record<string, string | null>, server: boolean) => void;
}) {
  const ops = new Set(view.filters.ops);
  const toggleOp = (op: Operation) => {
    const next = OPERATIONS.filter((o) => (o === op ? !ops.has(o) : ops.has(o)));
    onChange({ op: next.length ? next.join("|") : null }, false);
  };
  return (
    <div className="flex flex-wrap items-end gap-3 rounded-lg border bg-card px-4 py-3 text-sm" role="group" aria-label="Filtros">
      <label className="flex flex-col gap-1">
        Profundidade
        <select
          value={view.depth}
          onChange={(event) => onChange({ prof: event.target.value }, true)}
          className="h-8 rounded-md border border-input bg-card px-2"
        >
          {Array.from({ length: MAX_DEPTH }, (_, i) => i + 1).map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
      </label>
      <ConfidenceInput
        key={view.minConfidence}
        value={view.minConfidence}
        onCommit={(conf) => onChange({ conf: conf === 0 ? null : String(conf) }, true)}
      />
      <label className="flex flex-col gap-1">
        Limite de nós
        <select
          value={view.maxNodes}
          onChange={(event) => onChange({ limite: event.target.value === String(MAX_NODES) ? null : event.target.value }, true)}
          className="h-8 rounded-md border border-input bg-card px-2"
        >
          {[200, 500, 1000, MAX_NODES].map((n) => (
            <option key={n} value={n}>
              {n.toLocaleString("pt-BR")}
            </option>
          ))}
        </select>
      </label>
      <label className="flex h-8 items-center gap-2">
        <input type="checkbox" checked={view.includeInfra} onChange={(event) => onChange({ infra: event.target.checked ? "1" : null }, true)} />
        Infraestrutura
      </label>
      {showPgFunctions ? (
        <label className="flex h-8 items-center gap-2">
          <input type="checkbox" checked={view.expandPgFunctions} onChange={(event) => onChange({ pg: event.target.checked ? "1" : null }, true)} />
          Seguir funções do banco
        </label>
      ) : null}
      <fieldset className="flex flex-col gap-1">
        <legend className="mb-1">Operação</legend>
        <div className="flex gap-2">
          {OPERATIONS.map((op) => (
            <label key={op} className="flex h-8 items-center gap-1">
              <input type="checkbox" checked={ops.has(op)} onChange={() => toggleOp(op)} />
              {OPERATION_LABELS[op]}
            </label>
          ))}
        </div>
      </fieldset>
      <label className="flex flex-col gap-1">
        Módulo
        <select
          value={view.filters.module ?? ""}
          onChange={(event) => onChange({ modulo: event.target.value || null }, false)}
          className="h-8 max-w-56 rounded-md border border-input bg-card px-2"
        >
          <option value="">Todos</option>
          {modules.map((m) => (
            <option key={m} value={m}>
              {moduleName(m)}
            </option>
          ))}
        </select>
      </label>
      <label className="flex h-8 items-center gap-2">
        <input type="checkbox" checked={view.filters.onlyScreens} onChange={(event) => onChange({ telas: event.target.checked ? "1" : null }, false)} />
        Só telas
      </label>
    </div>
  );
}

/** Confiança mínima: vai para a URL ao sair do campo ou com Enter (`key` = valor da URL). */
function ConfidenceInput({ value, onCommit }: { value: number; onCommit: (value: number) => void }) {
  const [draft, setDraft] = useState(String(value));
  const commit = () => {
    const parsed = Number(draft.replace(",", "."));
    if (Number.isFinite(parsed) && parsed !== value) onCommit(Math.min(1, Math.max(0, parsed)));
  };
  return (
    <label className="flex flex-col gap-1">
      Confiança mínima
      <input
        type="number"
        min={0}
        max={1}
        step={0.1}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") commit();
        }}
        className="h-8 w-24 rounded-md border border-input bg-card px-2"
      />
    </label>
  );
}
