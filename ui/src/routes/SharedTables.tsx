/** Tabelas compartilhadas: `/tabelas?compartilhadas=1&modulo=`. */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { Button, Card, LoadingRows, Notice } from "../components/ui";
import { graphql } from "../graphql/generated";
import { request } from "../lib/api";
import { withParams } from "../lib/graph-url";
import { keyOf, shortName } from "../lib/ids";
import { NO_MODULE } from "../lib/modules";
import { formatNumber } from "../lib/utils";
import { topModules } from "./ModuleSheet";

const SharedTablesDocument = graphql(`
  query SharedTables($shared: Boolean, $module: String, $limit: Int!) {
    tables(shared: $shared, module: $module, limit: $limit) {
      ...TableStatsFields
    }
    tablesTotal(shared: $shared, module: $module)
    modules {
      module
      name
    }
  }
`);

export const PAGE = 100;

export function SharedTablesPage() {
  const [params, setParams] = useSearchParams();
  const shared = params.get("compartilhadas") === "0" ? null : true;
  const module = params.get("modulo") || null;
  const [limit, setLimit] = useState(PAGE);
  const query = useQuery({
    queryKey: ["sharedTables", shared, module, limit],
    queryFn: () => request(SharedTablesDocument, { shared, module, limit }),
    placeholderData: keepPreviousData,
    meta: { silent: true },
  });
  const update = (changes: Record<string, string | null>) => {
    setLimit(PAGE);
    setParams((current) => withParams(current, changes));
  };
  const names = new Map((query.data?.modules ?? []).map((m) => [m.module, m.name]));
  const name = (id: string) => names.get(id) ?? (id === NO_MODULE ? NO_MODULE : keyOf(id));

  return (
    <div className="space-y-5">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">Tabelas compartilhadas</h1>
        <p className="text-sm text-muted-foreground">
          Tabelas que amarram os domínios: com dono, quando outro módulo lê ou grava; sem dono, quando dois ou mais módulos a usam.
          Clique numa tabela para ver o impacto.
        </p>
      </header>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={shared === true}
            onChange={(event) => update({ compartilhadas: event.target.checked ? "1" : "0" })}
          />
          Só compartilhadas
        </label>
        <label className="flex items-center gap-2">
          Dono
          <select
            value={module ?? ""}
            onChange={(event) => update({ modulo: event.target.value || null })}
            className="h-8 max-w-64 rounded-md border border-input bg-card px-2"
          >
            <option value="">Todos</option>
            {[...(query.data?.modules ?? [])]
              .filter((m) => m.module !== NO_MODULE)
              .sort((a, b) => a.name.localeCompare(b.name, "pt-BR"))
              .map((m) => (
                <option key={m.module} value={m.module}>
                  {m.name}
                </option>
              ))}
          </select>
        </label>
        {query.data ? (
          <span className="ml-auto text-xs text-muted-foreground">
            {formatNumber(Math.min(limit, query.data.tablesTotal))} de {formatNumber(query.data.tablesTotal)} tabela(s), das mais usadas para as menos
          </span>
        ) : null}
      </div>
      {query.isPending ? (
        <LoadingRows rows={8} label="Carregando as tabelas" />
      ) : query.isError ? (
        <Notice tone="warning">{query.error.message}</Notice>
      ) : query.data.tables.length === 0 ? (
        <Notice>Nenhuma tabela com estes filtros.</Notice>
      ) : (
        <Card className="overflow-auto">
          <table className="w-full text-sm" aria-label="Tabelas">
            <thead className="text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-4 py-2 font-medium">Tabela</th>
                <th className="px-2 py-2 font-medium">Dono</th>
                <th className="px-2 py-2 font-medium">Módulos que leem</th>
                <th className="px-2 py-2 font-medium">Módulos que gravam</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {query.data.tables.map((t) => (
                <tr key={t.tableId}>
                  <td className="px-4 py-1.5">
                    <Link className="text-primary hover:underline" to={`/impacto?tabela=${encodeURIComponent(keyOf(t.tableId))}`} title="Ver o impacto">
                      {shortName(t.tableId)}
                    </Link>
                  </td>
                  <td className="px-2 py-1.5 text-xs">{t.ownerModules.length ? t.ownerModules.map(name).join(", ") : "—"}</td>
                  <td className="px-2 py-1.5 text-xs text-muted-foreground">
                    <span className="font-medium text-foreground">{t.readers}</span> · {topModules(t.readerModules, name, 4)}
                  </td>
                  <td className="px-2 py-1.5 text-xs text-muted-foreground">
                    <span className="font-medium text-foreground">{t.writers}</span> · {topModules(t.writerModules, name, 4)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {query.data.tablesTotal > query.data.tables.length ? (
            <div className="border-t px-4 py-2">
              <Button variant="ghost" size="sm" onClick={() => setLimit(Math.min(limit + PAGE, 500))} disabled={limit >= 500}>
                {limit >= 500 ? "Limite de 500 linhas: filtre pelo dono" : `Mostrar mais (${formatNumber(query.data.tablesTotal - query.data.tables.length)} restantes)`}
              </Button>
            </div>
          ) : null}
        </Card>
      )}
    </div>
  );
}
