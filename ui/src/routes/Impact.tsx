/** Impacto de uma tabela: `/impacto?tabela=&schema=`. */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";
import { Link } from "react-router";
import { TargetPicker } from "../components/graph/Controls";
import { LoadingRows, Notice } from "../components/ui";
import { request } from "../lib/api";
import { countByOperation, fromSubgraph, NO_MODULE } from "../lib/graph";
import { keyOf, nodeHref } from "../lib/ids";
import { ImpactDocument } from "../lib/subgraph";
import { OPERATION_LABELS } from "../lib/rules";
import { formatNumber } from "../lib/utils";
import { DiagramBody, Metric, useView } from "./Diagram";

export const IMPACT_DEPTH = 10; // o padrão do `php-map impact`

/**
 * `tabela=empenho.empempenho` (link do detalhe), `tabela=empempenho&schema=empenho` ou só
 * `tabela=empempenho`: sem schema, a API acha a tabela pelo nome (as tabelas
 * moram no schema do módulo, e não em `public`).
 */
export function parseTable(tabela: string, schema: string | null): { table: string; schema: string | null } {
  if (!schema && tabela.includes(".")) {
    const at = tabela.lastIndexOf(".");
    return { schema: tabela.slice(0, at), table: tabela.slice(at + 1) };
  }
  return { table: tabela, schema: schema || null };
}

type Summary = {
  direct?: number;
  dao_users?: number;
  files?: number;
  files_via_method?: number;
  screens?: number;
  code?: number;
  nodes?: number;
  rpc_files?: number;
};

const DAO_USERS_SHOWN = 8;

export function ImpactPage() {
  const { params, view, update } = useView(IMPACT_DEPTH);
  const raw = params.get("tabela") ?? "";
  const schemaParam = params.get("schema");
  const target = useMemo(() => (raw ? parseTable(raw, schemaParam) : null), [raw, schemaParam]);
  const query = useQuery({
    queryKey: ["impact", target?.schema, target?.table, view.depth, view.minConfidence, view.includeInfra, view.maxNodes],
    queryFn: () =>
      request(ImpactDocument, {
        table: target?.table ?? "",
        schema: target?.schema ?? null,
        depth: view.depth,
        minConfidence: view.minConfidence,
        includeInfra: view.includeInfra,
        maxNodes: view.maxNodes,
      }),
    enabled: target !== null,
    placeholderData: keepPreviousData,
    meta: { silent: true },
  });
  const graph = useMemo(() => (query.data ? fromSubgraph(query.data.impact, true) : null), [query.data]);
  // Pedido sem schema: a URL passa a ter o schema achado pela API (link estável).
  const root = query.data && !query.isPlaceholderData ? query.data.impact.root : null;
  const resolved = root ? keyOf(root) : null;
  useEffect(() => {
    if (target && target.schema === null && resolved && resolved.includes(".")) {
      update({ tabela: resolved, schema: null }, false, true);
    }
  }, [target, resolved, update]);
  const rootKey = resolved ?? (target ? (target.schema ? `${target.schema}.${target.table}` : target.table) : "");
  const summary = (query.data?.impact.summary ?? {}) as Summary;
  const daoUsers = query.data?.impact.daoUsers ?? [];
  const byOp = graph ? countByOperation(graph) : null;
  const modules = useMemo(() => {
    const all = new Set<string>();
    for (const n of graph?.nodes ?? []) if (n.id !== graph?.root) for (const m of n.modules) all.add(m);
    all.delete(NO_MODULE);
    return all.size;
  }, [graph]);

  return (
    <div className="space-y-5">
      <header className="space-y-3">
        <h1 className="text-2xl font-semibold">Impacto</h1>
        <p className="text-sm text-muted-foreground">
          O que é atingido por uma mudança numa tabela: quem a lê ou altera, quem chega nesses e as telas no fim do caminho.
        </p>
        <TargetPicker
          key={raw}
          label="Tabela"
          kind="Table"
          value={raw}
          placeholder="empempenho"
          onChange={(value) => update({ tabela: value, schema: null, op: null, modulo: null, telas: null }, true)}
        />
      </header>
      {!target ? (
        <Notice>Escolha uma tabela (ex.: empempenho). O diagrama vai da tabela, à esquerda, até as telas.</Notice>
      ) : query.isPending ? (
        <LoadingRows rows={8} label="Calculando o impacto" />
      ) : query.isError ? (
        <Notice tone="warning">{query.error.message}</Notice>
      ) : graph && byOp ? (
        <>
          <section aria-label="Resumo do impacto" className="grid gap-3 sm:grid-cols-3 xl:grid-cols-6">
            <Metric
              label="Tocam direto"
              value={formatNumber(summary.direct)}
              hint={(["SELECT", "INSERT", "UPDATE", "DELETE"] as const)
                .filter((op) => byOp[op] > 0)
                .map((op) => `${OPERATION_LABELS[op]} ${byOp[op]}`)
                .join(" · ") || "ninguém"}
            />
            <Metric
              label="Arquivos afetados"
              value={formatNumber(summary.files)}
              hint={summary.files_via_method ? `${formatNumber(summary.files_via_method)} via método` : undefined}
            />
            <Metric label="Telas" value={formatNumber(summary.screens)} />
            <Metric label="Métodos e funções" value={formatNumber(summary.code)} />
            <Metric label="Módulos afetados" value={formatNumber(modules)} />
            <Metric label="Nós no resultado" value={formatNumber(summary.nodes)} hint={`backends RPC: ${formatNumber(summary.rpc_files)}`} />
          </section>
          {daoUsers.length > 0 ? (
            <section aria-label="Quem usa pelo DAO" className="space-y-1.5">
              <h2 className="text-sm font-semibold">
                Quem usa pelo DAO <span className="font-normal text-muted-foreground">({formatNumber(summary.dao_users)})</span>
              </h2>
              <p className="text-xs text-muted-foreground">Chamam um método do DAO (`cl_…`) que toca a tabela.</p>
              <ul className="space-y-0.5 text-sm">
                {daoUsers.slice(0, DAO_USERS_SHOWN).map((u) => (
                  <li key={`${u.origin}|${u.operation}`}>
                    <Link className="font-mono text-xs underline-offset-2 hover:underline" to={nodeHref(u.origin)}>
                      {keyOf(u.origin)}
                    </Link>{" "}
                    <span className="text-muted-foreground">
                      {OPERATION_LABELS[u.operation] ?? u.operation} · {u.daoMethod}()
                    </span>
                  </li>
                ))}
              </ul>
              {daoUsers.length > DAO_USERS_SHOWN ? (
                <p className="text-xs text-muted-foreground">+{daoUsers.length - DAO_USERS_SHOWN} no relatório (`php-map impact`).</p>
              ) : null}
            </section>
          ) : null}
          <DiagramBody
            graph={graph}
            view={view}
            update={update}
            layout="dagre-rl"
            kind="impact"
            mermaidRoot={rootKey}
            truncated={query.data?.impact.truncated ?? false}
            fileName={`impacto-${rootKey}`}
          />
        </>
      ) : null}
    </div>
  );
}
