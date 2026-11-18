/**
 * Scan pela interface: consultas, etapas e o acompanhamento.
 *
 * O servidor roda o `scan` num subprocesso; a interface consulta `scanJob` a cada 2 s
 * enquanto ele roda e para no fim. Quando um scan termina com sucesso, todas as consultas
 * são invalidadas: o servidor já está servindo o grafo novo.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { graphql } from "../graphql/generated";
import type { ScanJobQuery as ScanJobData } from "../graphql/generated/graphql";
import { request } from "./api";

export const ScanJobDocument = graphql(`
  query ScanJob {
    scanJob {
      id
      state
      stage
      stageLabel
      stages {
        stage
        label
        at
      }
      startedAt
      finishedAt
      elapsedSeconds
      logTail
      result
      error
      project
    }
  }
`);

export const ScanDefaultsDocument = graphql(`
  query ScanDefaults {
    scanDefaults {
      source
      schema
      schemaSource
      schemaDbAvailable
      catalogFromEnv
      catalogAvailable
      project
      projectName
      profile
      marker
      dsnEnv
      reuseAst
      dockerAvailable
      dockerMessage
      recentSources
      readOnly
    }
  }
`);

/** A extração (cache de AST) desta pasta no `out` do servidor. */
export const AstCacheDocument = graphql(`
  query AstCache($source: String!) {
    astCache(source: $source) {
      exists
      reusable
      files
      updatedAt
      reason
      message
    }
  }
`);

export const SetSourceDocument = graphql(`
  mutation SetSource($path: String!) {
    setSource(path: $path) {
      source
      origin
      exists
      isEcidade
      graphSource
    }
  }
`);

export const StartScanDocument = graphql(`
  mutation StartScan($input: ScanInput!) {
    startScan(input: $input) {
      id
      state
    }
  }
`);

export const CancelScanDocument = graphql(`
  mutation CancelScan {
    cancelScan {
      id
      state
    }
  }
`);

export type ScanJob = NonNullable<ScanJobData["scanJob"]>;
export type ScanState = "running" | "succeeded" | "failed" | "canceled";

/** Mesma ordem e textos de `cli.STAGE_LABELS` (a ordem em que o `run_scan` executa). */
export const STAGES: readonly (readonly [string, string])[] = [
  ["discovery", "Descobrindo arquivos PHP"],
  ["hash", "Conferindo o cache de AST (sha1)"],
  ["extract", "Extraindo AST PHP"],
  ["schema", "Carregando o schema SQL"],
  ["resolve_php", "Resolvendo includes, chamadas e classes"],
  ["routes", "Ligando as rotas do Laravel aos controllers"], // só no perfil ecidade
  ["http_calls", "Ligando telas e JavaScript às rotas da API"], // só no perfil ecidade
  ["zend1_structure", "Ligando módulos, rotas e telas do Zend 1"], // só no perfil zend1
  ["doctrine", "Ligando entidades e DQL do Doctrine"], // só no perfil zend1
  ["eloquent", "Ligando models do Eloquent às tabelas"], // só no perfil ecidade
  ["sql", "Ligando o SQL das strings às tabelas"],
  ["endpoints", "Ligando os endpoints (SOAP, RPC e chamadas para fora)"],
  ["catalog", "Aplicando o dicionário de dados e os menus"],
  ["modules", "Classificando módulos por regra de nome"],
  ["save", "Gravando o grafo"],
  ["formula_index", "Indexando as fórmulas e regras por assunto"],
  ["search", "Construindo o índice de busca"],
  ["analytics", "Calculando os agregados (acoplamento entre módulos)"],
];

/** Etapas que só existem em um perfil: nos outros perfis nunca rodam, então nem aparecem. */
const STAGE_PROFILE: Record<string, string> = {
  routes: "ecidade",
  http_calls: "ecidade",
  zend1_structure: "zend1",
  doctrine: "zend1",
  eloquent: "ecidade",
};

/** Intervalo da consulta `scanJob` enquanto o scan roda (os testes encurtam). */
export const scanPolling = { intervalMs: 2000 };

export function isRunning(job: ScanJob | null | undefined): boolean {
  return job?.state === "running";
}

export function useScanJob() {
  return useQuery({
    queryKey: ["scanJob"],
    queryFn: async () => (await request(ScanJobDocument)).scanJob ?? null,
    refetchInterval: (query) => (isRunning(query.state.data) ? scanPolling.intervalMs : false),
    refetchIntervalInBackground: true,
    staleTime: 0,
    meta: { silent: true }, // servidor fora do ar: o cabeçalho só não mostra o indicador
  });
}

/**
 * Observa o scan (montado uma vez, no layout): quando um scan que estava rodando termina
 * com sucesso, invalida todas as consultas, porque o servidor já serve o grafo novo.
 */
export function useScanWatcher(job: ScanJob | null | undefined): void {
  const client = useQueryClient();
  const previous = useRef<{ id: string; state: string } | null>(null);
  useEffect(() => {
    const before = previous.current;
    previous.current = job ? { id: job.id, state: job.state } : null;
    if (job && before && before.id === job.id && before.state === "running" && job.state === "succeeded") {
      void client.invalidateQueries();
    }
  }, [client, job]);
}

export type StageStatus = "done" | "current" | "stopped" | "pending" | "skipped";

/**
 * Situação de cada etapa. Uma etapa que não apareceu, mas tem outra depois dela já vista,
 * foi pulada (ex.: `extract` quando tudo veio do cache, `catalog` sem catálogo). Numa falha
 * ou num cancelamento, a última etapa vista é onde o scan parou.
 */
export function stageStatuses(
  job: ScanJob,
  profile?: string | null,
): { stage: string; label: string; status: StageStatus; at: string | null }[] {
  const seen = new Map(job.stages.map((s) => [s.stage, s]));
  const stages = STAGES.filter(([stage]) => !profile || !STAGE_PROFILE[stage] || STAGE_PROFILE[stage] === profile);
  const lastIndex = stages.reduce((last, [stage], index) => (seen.has(stage) ? index : last), -1);
  return stages.map(([stage, label], index) => {
    const found = seen.get(stage);
    let status: StageStatus;
    if (found && stage === job.stage && job.state === "running") status = "current";
    else if (found && stage === job.stage && job.state !== "succeeded") status = "stopped";
    else if (found) status = "done";
    else if (index < lastIndex || job.state === "succeeded") status = "skipped";
    else status = "pending";
    return { stage, label: found?.label ?? label, status, at: found?.at ?? null };
  });
}

/** `mm:ss` (ou `h:mm:ss` acima de uma hora). */
export function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/** Duração legível para o resumo: `45 s`, `3 min 17 s`, `1 h 02 min`. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h} h ${String(m).padStart(2, "0")} min`;
  return m > 0 ? `${m} min ${s} s` : `${s} s`;
}

export type ScanResultSummary = {
  nodes?: number;
  edges?: number;
  modules?: number;
  screens_in_menu?: number | null;
  files?: number;
  warnings?: string[];
  /** Texto das funções da base (`routine_sources`). */
  routines?: { stored: number; bytes: number; source: string; warning?: string | null } | null;
};

/** `Funções com texto: 1.265 (7,7 MB)`; `null` quando o scan não leu o texto das funções. */
export function routinesLine(summary: ScanResultSummary): string | null {
  const routines = summary.routines;
  if (!routines || routines.source !== "db") return null;
  const mb = (routines.bytes / 1_000_000).toLocaleString("pt-BR", { maximumFractionDigits: 1 });
  return `${routines.stored.toLocaleString("pt-BR")} funções da base com o texto gravado (${mb} MB)`;
}

export function resultSummary(job: ScanJob): { summary: ScanResultSummary; seconds: number | null } {
  const result = (job.result ?? {}) as { summary?: ScanResultSummary; seconds?: number };
  return { summary: result.summary ?? {}, seconds: result.seconds ?? null };
}
