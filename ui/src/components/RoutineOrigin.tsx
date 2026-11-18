import { useQuery } from "@tanstack/react-query";
import { ListTree } from "lucide-react";
import { Link } from "react-router";
import { graphql } from "../graphql/generated";
import type { RoutineOriginQuery } from "../graphql/generated/graphql";
import { request } from "../lib/api";
import { keyOf, labelOf } from "../lib/ids";
import { ConfidenceBadge, NodeLink } from "./NodeBits";
import { Badge, Card, LoadingRows, Notice } from "./ui";

const RoutineOriginDocument = graphql(`
  query RoutineOrigin($id: ID!) {
    hasRoutineOrigins
    routineOrigin(id: $id) {
      status
      reason
      module
      moduleConfidence
      generated
      callersPhp
      callersRoutines
      triggers
      entries {
        kind
        target
        label
        distance
        via
        confidence
      }
      mentions {
        file
        line
        confidence
        hypothesis
      }
    }
  }
`);

type Origin = NonNullable<RoutineOriginQuery["routineOrigin"]>;
type Entry = Origin["entries"][number];

/** O que cada status quer dizer, em palavras de quem não programa. */
export const STATUS_LABEL: Record<string, string> = {
  com_origem: "Com origem",
  so_arquivo: "Só arquivo",
  gerada: "Gerada pela auditoria",
  sem_chamador: "Sem chamador",
  cadeia_sem_origem: "Cadeia sem origem",
};

const KINDS: { kind: string; title: string }[] = [
  { kind: "menu", title: "Menus" },
  { kind: "route", title: "Rotas" },
  { kind: "endpoint", title: "Endpoints" },
  { kind: "file", title: "Arquivos" },
];

/** `1` → `o mais próximo`; `5` → `os 5 mais próximos`. */
export function nearestLabel(count: number): string {
  return count === 1 ? "o mais próximo" : `os ${count} mais próximos`;
}

export function distanceLabel(distance: number): string {
  return distance === 1 ? "chamada direta" : `${distance} saltos`;
}

/** Percentual do voto do módulo: `0,42` → `42%`. */
export function shareLabel(share: number | null | undefined): string {
  return share == null ? "" : `${Math.round(share * 100)}%`;
}

/** O arquivo de onde se abre a origem (o 1º do caminho), para "Menus por fonte". */
function sourceFile(entry: Entry): string | null {
  const first = entry.via[0];
  return first && labelOf(first) === "File" ? keyOf(first) : null;
}

/**
 * Origem de uma função da base: por onde ela é usada. 100% de origem não é alcançável
 * (SQL dinâmico, JavaScript e agendamentos não aparecem no grafo), por isso toda função tem um
 * status e um motivo. A menção por texto é hipótese.
 */
export function RoutineOriginSection({ id }: { id: string }) {
  const query = useQuery({
    queryKey: ["routineOrigin", id],
    queryFn: () => request(RoutineOriginDocument, { id }),
  });
  if (query.isPending) return <LoadingRows rows={5} label="Carregando a origem" />;
  if (query.isError) return <Notice tone="warning">{query.error.message}</Notice>;
  const origin = query.data.routineOrigin;
  if (!origin) {
    return (
      <Notice tone={query.data.hasRoutineOrigins ? "info" : "warning"}>
        {query.data.hasRoutineOrigins
          ? "Esta função não tem origem calculada: ela pode ter sido criada depois do scan."
          : "Este grafo foi escaneado antes do cálculo de origem das funções. Rode o scan novo."}
      </Notice>
    );
  }
  return (
    <div className="space-y-4">
      <Card className="space-y-2 px-5 py-4 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <Badge>{STATUS_LABEL[origin.status] ?? origin.status}</Badge>
          <span>{origin.reason}</span>
        </div>
        <p className="text-muted-foreground">
          Chamada por {origin.callersPhp.toLocaleString("pt-BR")} arquivo(s) ou método(s) PHP,{" "}
          {origin.callersRoutines.toLocaleString("pt-BR")} outra(s) função(ões) e{" "}
          {origin.triggers.toLocaleString("pt-BR")} trigger(s).
        </p>
        {origin.module ? (
          <p className="flex flex-wrap items-center gap-2">
            Módulo provável: <NodeLink id={origin.module} />
            <span className="text-muted-foreground">
              {shareLabel(origin.moduleConfidence)} dos votos dos arquivos de entrada
            </span>
          </p>
        ) : null}
        {origin.status === "sem_chamador" ? (
          <p className="text-muted-foreground">
            Sem chamador não quer dizer código morto: SQL dinâmico, JavaScript e agendamentos não aparecem no mapa.
          </p>
        ) : null}
      </Card>

      {KINDS.map(({ kind, title }) => {
        const entries = origin.entries.filter((e) => e.kind === kind);
        if (entries.length === 0) return null;
        return (
          <Card key={kind} aria-label={title}>
            <header className="border-b px-5 py-3 text-sm font-semibold">
              {title} <span className="font-normal text-muted-foreground">({nearestLabel(entries.length)})</span>
            </header>
            <ul className="divide-y">
              {entries.map((entry) => (
                <EntryRow key={`${entry.kind}:${entry.target}`} entry={entry} />
              ))}
            </ul>
          </Card>
        );
      })}

      {origin.mentions.length > 0 ? (
        <Card aria-label="Menções por texto">
          <header className="flex items-center gap-2 border-b px-5 py-3 text-sm font-semibold">
            Arquivos que citam o nome <Badge>hipótese</Badge>
          </header>
          <p className="px-5 pt-3 text-xs text-muted-foreground">
            O nome aparece em texto ou comentário do arquivo (confiança 0,5). Não é chamada confirmada e não entra no
            acoplamento.
          </p>
          <ul className="divide-y">
            {origin.mentions.map((mention) => (
              <li key={mention.file} className="flex items-center justify-between gap-3 px-5 py-2 text-sm">
                <NodeLink id={mention.file} />
                <span className="text-xs text-muted-foreground">linha {mention.line}</span>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}
    </div>
  );
}

function EntryRow({ entry }: { entry: Entry }) {
  const file = sourceFile(entry);
  return (
    <li className="space-y-1 px-5 py-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <NodeLink id={entry.target} name={entry.label} className="font-medium" />
        <span className="flex items-center gap-2 text-xs text-muted-foreground">
          {distanceLabel(entry.distance)}
          <ConfidenceBadge confidence={entry.confidence} />
        </span>
      </div>
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer">Caminho até a função ({entry.via.length} nós)</summary>
        <ol className="mt-1 space-y-0.5 pl-4">
          {entry.via.map((node) => (
            <li key={node} className="min-w-0">
              <NodeLink id={node} />
            </li>
          ))}
        </ol>
      </details>
      {file && entry.kind === "menu" ? (
        <Link
          className="inline-flex items-center gap-1.5 text-xs text-primary hover:underline"
          to={`/menus?fonte=${encodeURIComponent(file)}`}
        >
          <ListTree className="h-3.5 w-3.5" aria-hidden="true" /> Ver menus por fonte
        </Link>
      ) : null}
    </li>
  );
}
