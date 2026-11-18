/** Trace de uma tela até o banco: `/trace?entrada=`. */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ScrollText } from "lucide-react";
import { useMemo } from "react";
import { Link } from "react-router";
import { TargetPicker } from "../components/graph/Controls";
import { LoadingRows, Notice } from "../components/ui";
import { request } from "../lib/api";
import { fromSubgraph } from "../lib/graph";
import { rulesHref } from "../lib/ids";
import { OPERATION_LABELS } from "../lib/rules";
import { TraceDocument } from "../lib/subgraph";
import { formatNumber } from "../lib/utils";
import { DiagramBody, Metric, useView } from "./Diagram";

export const TRACE_DEPTH = 8; // o padrão do `php-map trace`
const OPERATION_ORDER = ["INSERT", "UPDATE", "DELETE", "SELECT"]; // a ordem da CLI

type Summary = { nodes?: number; files?: number; screens?: number; tables?: Record<string, number>; rpc?: number };

export function TracePage() {
  const { params, view, update } = useView(TRACE_DEPTH);
  const entry = params.get("entrada") ?? "";
  const query = useQuery({
    queryKey: ["trace", entry, view.depth, view.minConfidence, view.includeInfra, view.expandPgFunctions, view.maxNodes],
    queryFn: () =>
      request(TraceDocument, {
        entry,
        depth: view.depth,
        minConfidence: view.minConfidence,
        includeInfra: view.includeInfra,
        expandPgFunctions: view.expandPgFunctions,
        maxNodes: view.maxNodes,
      }),
    enabled: entry.length > 0,
    placeholderData: keepPreviousData,
    meta: { silent: true },
  });
  const graph = useMemo(() => (query.data ? fromSubgraph(query.data.trace, false) : null), [query.data]);
  const summary = (query.data?.trace.summary ?? {}) as Summary;
  const tables = summary.tables ?? {};

  return (
    <div className="space-y-5">
      <header className="space-y-3">
        <h1 className="text-2xl font-semibold">Trace</h1>
        <p className="text-sm text-muted-foreground">
          O caminho de uma tela até o banco: includes e forms, backends AJAX, classes e métodos, tabelas e funções do banco.
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <TargetPicker
          key={entry}
            label="Arquivo de entrada"
            kind="File"
            value={entry}
            placeholder="emp4_anularempenho001.php"
            onChange={(value) => update({ entrada: value, op: null, modulo: null, telas: null }, true)}
          />
          {entry ? (
            <Link className="inline-flex h-9 items-center gap-1.5 rounded-md border border-input px-3 text-sm hover:bg-muted" to={rulesHref(entry)}>
              <ScrollText className="h-4 w-4" aria-hidden="true" /> Ver regras
            </Link>
          ) : null}
        </div>
      </header>
      {!entry ? (
        <Notice>Escolha uma tela (ex.: emp4_anularempenho001.php). O diagrama vai da esquerda (a tela) para a direita (o banco).</Notice>
      ) : query.isPending ? (
        <LoadingRows rows={8} label="Rastreando" />
      ) : query.isError ? (
        <Notice tone="warning">{query.error.message}</Notice>
      ) : graph ? (
        <>
          <section aria-label="Resumo do trace" className="grid gap-3 sm:grid-cols-3 xl:grid-cols-5">
            <Metric label="Nós" value={formatNumber(summary.nodes)} />
            <Metric label="Arquivos" value={formatNumber(summary.files)} hint={`${formatNumber(summary.screens)} telas`} />
            <div className="sm:col-span-2">
              <Metric
                label="Tabelas por operação"
                value={
                  OPERATION_ORDER.filter((op) => tables[op])
                    .map((op) => `${OPERATION_LABELS[op]} ${formatNumber(tables[op])}`)
                    .join(" · ") || "nenhuma"
                }
              />
            </div>
            <Metric label="Funções do banco (RPC)" value={formatNumber(summary.rpc)} />
          </section>
          <DiagramBody
            graph={graph}
            view={view}
            update={update}
            layout="dagre-lr"
            kind="trace"
            mermaidRoot={entry}
            truncated={query.data?.trace.truncated ?? false}
            fileName={`trace-${entry.replace(/[/\\]/g, "_")}`}
            showPgFunctions
          />
        </>
      ) : null}
    </div>
  );
}
