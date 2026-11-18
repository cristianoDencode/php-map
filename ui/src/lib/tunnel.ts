/**
 * Túnel do `/mcp`: consultas, mutações e funções puras.
 *
 * A configuração só tem NOMES de variáveis de ambiente (ex.: NGROK_AUTHTOKEN); a página vê só se
 * cada uma está definida, nunca o valor. O log do túnel já chega mascarado do servidor.
 */
import { useQuery } from "@tanstack/react-query";
import { graphql } from "../graphql/generated";
import { request } from "./api";

export const TunnelConfigDocument = graphql(`
  query TunnelConfig {
    tunnelConfig {
      configured
      command
      publicUrl
      urlRegex
      env {
        name
        defined
      }
      examples {
        id
        label
        command
        publicUrl
        urlRegex
        envNames
      }
    }
  }
`);

export const TunnelStatusDocument = graphql(`
  query TunnelStatus {
    tunnelStatus {
      state
      stage
      stageLabel
      url
      publicHost
      error
      log
      findings {
        kind
        where
        masked
        hash
      }
      elapsedSeconds
      canStart
      blockedReason
    }
  }
`);

export const SaveTunnelConfigDocument = graphql(`
  mutation SaveTunnelConfig($command: [String!]!, $publicUrl: String, $urlRegex: String, $envNames: [String!]) {
    saveTunnelConfig(command: $command, publicUrl: $publicUrl, urlRegex: $urlRegex, envNames: $envNames) {
      configured
    }
  }
`);

export const StartTunnelDocument = graphql(`
  mutation StartTunnel {
    startTunnel {
      state
    }
  }
`);

export const StopTunnelDocument = graphql(`
  mutation StopTunnel {
    stopTunnel {
      state
    }
  }
`);

export const TUNNEL_STAGES: { id: string; label: string }[] = [
  { id: "prechecks", label: "Pré-checagens" },
  { id: "secrets", label: "Varredura de segredos" },
  { id: "tunnel", label: "Túnel" },
  { id: "url", label: "URL pública" },
  { id: "health", label: "Saúde" },
];

export const STATE_LABEL: Record<string, string> = {
  parado: "parado",
  iniciando: "subindo…",
  "no-ar": "no ar",
  caiu: "caiu",
  falhou: "falhou",
};

export function isTunnelActive(state: string | undefined): boolean {
  return state === "iniciando" || state === "no-ar";
}

export function useTunnelStatus() {
  return useQuery({
    queryKey: ["tunnelStatus"],
    queryFn: async () => (await request(TunnelStatusDocument)).tunnelStatus,
    refetchInterval: (query) => (query.state.data?.state === "iniciando" ? 1500 : false),
    meta: { silent: true },
  });
}

export function useTunnelConfig() {
  return useQuery({
    queryKey: ["tunnelConfig"],
    queryFn: async () => (await request(TunnelConfigDocument)).tunnelConfig,
    meta: { silent: true },
  });
}

/** `ngrok http {port} --url="https://a b"` → argumentos (aspas simples ou duplas agrupam). */
export function splitCommand(text: string): string[] {
  const out: string[] = [];
  let current = "";
  let quote: string | null = null;
  let started = false;
  for (const ch of text) {
    if (quote) {
      if (ch === quote) quote = null;
      else current += ch;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      started = true;
    } else if (/\s/.test(ch)) {
      if (started || current) out.push(current);
      current = "";
      started = false;
    } else {
      current += ch;
    }
  }
  if (started || current) out.push(current);
  return out;
}

export function joinCommand(args: string[]): string {
  return args.map((a) => (a === "" || /[\s"']/.test(a) ? `"${a.replace(/"/g, "'")}"` : a)).join(" ");
}

/** Nomes separados por vírgula ou espaço. */
export function splitNames(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((n) => n.trim())
    .filter(Boolean);
}
