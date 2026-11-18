/**
 * Tela Comunicação: consultas, rótulos e CSV.
 *
 * A lista é paginada na API (`limit`/`offset`): o navegador nunca recebe os milhares de ações
 * de RPC do e-cidade de uma vez. O CSV busca as páginas, uma de cada vez, só quando pedido.
 */
import { graphql } from "../graphql/generated";
import type { CommunicationQuery, EndpointDetailQuery } from "../graphql/generated/graphql";
import { request } from "./api";
import { BOM, csvCell } from "./graph";
import { keyOf } from "./ids";

export const PAGE_SIZE = 50;
const EXPORT_PAGE = 200; // o limite da API

export const CommunicationDocument = graphql(`
  query Communication(
    $system: String
    $protocol: String
    $direction: String
    $module: String
    $search: String
    $limit: Int
    $offset: Int
  ) {
    communication(
      system: $system
      protocol: $protocol
      direction: $direction
      module: $module
      search: $search
      limit: $limit
      offset: $offset
    ) {
      total
      offset
      limit
      crossNote
      counters {
        serves
        calls
        withParams
        orphans
        crossAvailable
      }
      systems {
        value
        count
      }
      protocols {
        value
        count
      }
      modules {
        value
        count
      }
      items {
        endpointId
        nodeLabel
        system
        protocol
        category
        kind
        name
        direction
        resolved
        params {
          name
          required
          type
          source
        }
        implementedBy {
          node
          path
          line
        }
        callers
        callerSample {
          project
          node
          path
          line
          module
          occurrences
        }
        module
        status
        reaches {
          tables
          modules
          nodes
        }
      }
    }
  }
`);

export const EndpointDetailDocument = graphql(`
  query EndpointDetail($id: ID!) {
    endpointDetail(id: $id) {
      row {
        endpointId
        nodeLabel
        system
        protocol
        category
        kind
        name
        direction
        resolved
        params {
          name
          required
          type
          source
        }
        implementedBy {
          node
          path
          line
        }
        callers
        module
        status
        reaches {
          tables
          modules
          nodes
        }
      }
      callers {
        project
        node
        path
        line
        module
        args
        occurrences
      }
      attrs
      counterpart {
        available
        message
        status
        servedBy
        system
        servers {
          node
          path
          line
        }
        params {
          name
          required
          type
          source
        }
        reaches {
          tables
          modules
          nodes
        }
        callers {
          project
          node
          path
          line
          module
          occurrences
        }
      }
    }
  }
`);

export type CommunicationPage = CommunicationQuery["communication"];
export type CommunicationRow = CommunicationPage["items"][number];
export type EndpointDetail = EndpointDetailQuery["endpointDetail"];

export type Filters = {
  system: string | null;
  protocol: string | null;
  direction: string | null;
  module: string | null;
  search: string | null;
};

export const PROTOCOL_LABELS: Record<string, string> = {
  soap: "SOAP",
  rpc: "RPC (AJAX)",
  http: "HTTP (rotas)",
  externo: "Externo (sai do sistema)",
};
export const DIRECTION_LABELS: Record<string, string> = { entra: "Entra", sai: "Sai" };
export const DIRECTION_HELP: Record<string, string> = {
  entra: "O projeto serve (SOAP, RPC ou rota).",
  sai: "O projeto chama outro sistema.",
};

export const STATUS_LABELS: Record<string, string> = {
  casado: "casado",
  chamado_sem_servidor: "não servido",
  servido_sem_cliente: "sem cliente",
};
export const STATUS_HELP: Record<string, string> = {
  casado: "Servido por um projeto e chamado por outro.",
  chamado_sem_servidor: "Alguém chama, e nenhum projeto mapeado serve.",
  servido_sem_cliente:
    "Nenhum projeto mapeado chama. Não quer dizer código morto: portais, apps e outros municípios podem chamar.",
};

export function protocolLabel(category: string): string {
  return PROTOCOL_LABELS[category] ?? category;
}

/** Página (1-based) da URL: inteiro positivo; ausente ou inválido → 1. */
export function parsePage(value: string | null): number {
  if (value === null || !/^\d+$/.test(value.trim())) return 1;
  return Math.max(1, Number(value.trim()));
}

/** "51–100 de 2.360" para a barra de paginação. */
export function rangeText(page: Pick<CommunicationPage, "total" | "offset" | "items">): string {
  if (page.total === 0) return "0";
  const from = page.offset + 1;
  const to = page.offset + page.items.length;
  return `${from.toLocaleString("pt-BR")}–${to.toLocaleString("pt-BR")} de ${page.total.toLocaleString("pt-BR")}`;
}

type Param = { name: string; required?: boolean | null };

/** `cpf*, nome`: o asterisco marca o parâmetro obrigatório. */
export function paramsText(params: Param[], max = 4): string {
  if (!params.length) return "—";
  const shown = params.slice(0, max).map((p) => (p.required ? `${p.name}*` : p.name));
  return params.length > max ? `${shown.join(", ")}, +${params.length - max}` : shown.join(", ");
}

/** Nome do módulo: o do mapa de nomes, ou a chave do ID. */
export function moduleName(id: string, names: Map<string, string> | undefined): string {
  return names?.get(id) ?? keyOf(id);
}

function variables(filters: Filters, limit: number, offset: number) {
  return {
    system: filters.system,
    protocol: filters.protocol,
    direction: filters.direction,
    module: filters.module,
    search: filters.search,
    limit,
    offset,
  };
}

export function listVariables(filters: Filters, page: number) {
  return variables(filters, PAGE_SIZE, (page - 1) * PAGE_SIZE);
}

/** Todas as linhas do filtro, página a página (só para o CSV). */
export async function fetchAll(filters: Filters): Promise<CommunicationRow[]> {
  const rows: CommunicationRow[] = [];
  for (let offset = 0; ; offset += EXPORT_PAGE) {
    const { communication } = await request(CommunicationDocument, variables(filters, EXPORT_PAGE, offset));
    rows.push(...communication.items);
    if (communication.items.length < EXPORT_PAGE || rows.length >= communication.total) break;
  }
  return rows;
}

export const COMMUNICATION_CSV_COLUMNS = [
  "id",
  "nome",
  "sistema",
  "protocolo",
  "tipo",
  "direcao",
  "situacao",
  "modulo",
  "parametros",
  "implementado_em",
  "chamadas",
  "tabelas_alcancadas",
] as const;

/** CSV com `;` e BOM (o Excel em português abre direto, com os acentos). */
export function communicationCsv(rows: CommunicationRow[]): string {
  const lines = [COMMUNICATION_CSV_COLUMNS.join(";")];
  for (const row of rows) {
    const first = row.implementedBy[0];
    lines.push(
      [
        row.endpointId,
        row.name,
        row.system,
        row.category,
        row.kind ?? "",
        row.direction,
        row.status ?? "",
        row.module ? keyOf(row.module) : "",
        row.params.map((p) => (p.required ? `${p.name}*` : p.name)).join(" | "),
        first?.path ? `${first.path}${first.line ? `:${first.line}` : ""}` : "",
        row.callers,
        row.reaches ? row.reaches.tables.length : "",
      ]
        .map(csvCell)
        .join(";"),
    );
  }
  return `${BOM}${lines.join("\r\n")}\r\n`;
}

export function csvName(project: string | null): string {
  return `comunicacao-${(project ?? "projeto").replace(/[^\w.-]+/g, "_")}.csv`;
}
