import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useStats } from "../app/Layout";
import { coverage } from "../lib/coverage";
import { ConfidenceBadge, LabelBadge, NodeLink } from "../components/NodeBits";
import { Card, CardHeader, LoadingRows, Notice, Skeleton } from "../components/ui";
import { graphql } from "../graphql/generated";
import { request } from "../lib/api";
import { keyOf } from "../lib/ids";
import { formatNumber, formatPercent, plural } from "../lib/utils";

const HomeQuery = graphql(`
  query Home {
    tables(shared: true, limit: 10) {
      tableId
      ownerModules
      readers
      writers
    }
    hotspots(limit: 10) {
      nodeId
      label
      module
      inDegree
      fanInModules
    }
    screens(inMenu: false, limit: 10) {
      fileId
      module
      reachedBy
    }
  }
`);

function Metric({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <Card className="px-5 py-4">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
      {hint ? <p className="mt-1 text-xs text-muted-foreground">{hint}</p> : null}
    </Card>
  );
}


export function HomePage() {
  const stats = useStats();
  const lists = useQuery({
    queryKey: ["home"],
    queryFn: () => request(HomeQuery),
    meta: { silent: true }, // banco sem agregados: aviso no lugar das listas, sem toast
  });
  const nodes = stats.data?.nodes ?? {};
  const cov = coverage(stats.data);
  const cards: [string, number | null | undefined][] = [
    ["Arquivos", nodes.File],
    ["Telas", cov.screens],
    ["Tabelas", nodes.Table],
    ["Funções do banco", nodes.PgFunction],
    ["Itens de menu", nodes.BusinessAction],
    ["Módulos", nodes.Module],
  ];
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Mapeamento do e-cidade</h1>
        <p className="text-sm text-muted-foreground">
          O que o scan encontrou: telas, tabelas, menus, módulos e como eles se ligam.
        </p>
      </div>
      <section aria-label="Contagens" className="grid grid-cols-2 gap-4 lg:grid-cols-6">
        {cards.map(([label, value]) => (
          <Metric
            key={label}
            label={label}
            value={stats.isPending ? <Skeleton className="h-7 w-20" /> : formatNumber(value)}
          />
        ))}
      </section>
      <section aria-label="Cobertura" className="grid gap-4 md:grid-cols-2">
        <Metric
          label="Arquivos com módulo"
          value={cov.total ? formatPercent(cov.withModule ?? 0, cov.total) : "—"}
          hint={
            cov.total
              ? `${formatNumber(cov.withModule)} de ${formatNumber(cov.total)} (catálogo do e-cidade e regras de nome)`
              : "Rode o scan com o catálogo (--catalog-db) para ver a cobertura."
          }
        />
        <Metric
          label="Telas no menu"
          value={cov.screens ? formatPercent(cov.inMenu ?? 0, cov.screens) : "—"}
          hint={
            cov.screens
              ? `${formatNumber(cov.inMenu)} de ${formatNumber(cov.screens)}; as outras são candidatas a código morto`
              : undefined
          }
        />
      </section>
      {lists.isError ? (
        <Notice tone="warning">{lists.error.message}</Notice>
      ) : (
        <div className="grid gap-4 xl:grid-cols-3">
          <Card>
            <CardHeader title="Tabelas mais compartilhadas" description="Usadas por módulos que não são donos" />
            {lists.isPending ? (
              <LoadingRows />
            ) : lists.data.tables.length === 0 ? (
              <p className="px-5 py-4 text-sm text-muted-foreground">
                Nenhuma tabela usada por outro módulo além do dono.
              </p>
            ) : (
              <ol className="divide-y">
                {lists.data.tables.map((table) => (
                  <li key={table.tableId} className="flex items-center gap-2 px-5 py-2 text-sm">
                    <NodeLink id={table.tableId} className="flex-1" />
                    <span className="text-xs text-muted-foreground" title="Módulos que leem · que gravam">
                      {table.readers} leem · {table.writers} gravam
                    </span>
                  </li>
                ))}
              </ol>
            )}
          </Card>
          <Card>
            <CardHeader title="Pontos quentes" description="Mais módulos diferentes chegando neles" />
            {lists.isPending ? (
              <LoadingRows />
            ) : (
              <ol className="divide-y">
                {lists.data.hotspots.map((spot) => (
                  <li key={spot.nodeId} className="flex items-center gap-2 px-5 py-2 text-sm">
                    <LabelBadge label={spot.label} />
                    <NodeLink id={spot.nodeId} className="flex-1" />
                    <span className="text-xs text-muted-foreground">{plural(spot.fanInModules, "módulo")}</span>
                  </li>
                ))}
              </ol>
            )}
          </Card>
          <Card>
            <CardHeader title="Telas fora do menu" description="Mais referenciadas primeiro" />
            {lists.isPending ? (
              <LoadingRows />
            ) : lists.data.screens.length === 0 ? (
              <p className="px-5 py-4 text-sm text-muted-foreground">Todas as telas estão no menu.</p>
            ) : (
              <ol className="divide-y">
                {lists.data.screens.map((screen) => (
                  <li key={screen.fileId} className="flex items-center gap-2 px-5 py-2 text-sm">
                    <NodeLink id={screen.fileId} className="flex-1" />
                    <span className="text-xs text-muted-foreground" title={keyOf(screen.module)}>
                      {screen.reachedBy === 1 ? "1 arquivo chega nela" : `${plural(screen.reachedBy, "arquivo")} chegam nela`}
                    </span>
                  </li>
                ))}
              </ol>
            )}
          </Card>
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        Confiança das ligações: <ConfidenceBadge confidence={1} /> certa ·{" "}
        <ConfidenceBadge confidence={0.9} /> alta · <ConfidenceBadge confidence={0.6} /> média ·{" "}
        <ConfidenceBadge confidence={0.35} /> baixa.
      </p>
    </div>
  );
}
