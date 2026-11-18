import { render } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { vi } from "vitest";
import { AppProviders, createQueryClient, routes } from "../app/App";

export type Handler = (variables: Record<string, unknown>) => unknown;
export type Calls = { operation: string; variables: Record<string, unknown> }[];

/** Respostas padrão: servidor com pasta do e-cidade, stats e agregados vazios. */
export const DEFAULT_HANDLERS: Record<string, Handler> = {
  Stats: () => ({
    stats: {
      meta: { scanned_at: "2026-09-25T10:00:00-03:00", source: "D:/ecidade" },
      nodes: { File: 34, Table: 30, PgFunction: 2, BusinessAction: 3, Module: 3 },
      catalog: { modules: { files_with_catalog: 3, screens_classified: 0, daos_classified: 2, files_without_module: 25 } },
      analytics: { screens: 3, screens_in_menu: 2 },
    },
  }),
  Workspace: () => ({
    workspace: {
      source: "D:/ecidade",
      origin: "grafo",
      exists: true,
      isEcidade: true,
      graphSource: "D:/ecidade",
    },
  }),
  Projects: () => ({
    projects: [
      {
        slug: "ecidade",
        name: "e-cidade",
        profile: "ecidade",
        source: "D:/ecidade",
        sourceExists: true,
        hasGraph: true,
        lastScan: "2026-09-25T10:00:00-03:00",
        schemaFromDb: false,
        dsnEnv: null,
        dsnAvailable: false,
        active: true,
        scanning: false,
      },
    ],
    activeProject: { slug: "ecidade" },
  }),
  Home: () => ({ tables: [], hotspots: [], screens: [] }),
  ModuleNames: () => ({ modules: [{ module: "Module:empenho", name: "Empenho" }] }),
  ScanJob: () => ({ scanJob: null }),
  AstCache: () => ({
    astCache: {
      exists: true,
      reusable: true,
      files: 34,
      updatedAt: "2026-09-25T10:00:00-03:00",
      reason: "reusable",
      message: "Há extração desta pasta: 34 arquivos (25/09/2026 10:00). Só os arquivos novos ou alterados voltam ao extrator.",
    },
  }),
};

/**
 * Troca o `fetch`: cada operação GraphQL responde pelo handler de mesmo nome. Um handler
 * que lança vira erro GraphQL com a mensagem dele.
 */
export type AuthAnswer = { status?: number; body?: unknown };
export type AuthRoutes = Record<string, (body: Record<string, unknown>) => AuthAnswer>;

/** `/auth/me` padrão: sem login exigido (as telas existentes não passam pelo login). */
export const NO_LOGIN_ME = { loginRequired: false, setupRequired: false, user: null };

export function mockApi(
  handlers: Record<string, Handler> = {},
  authRoutes: AuthRoutes = {},
): Calls {
  const all = { ...DEFAULT_HANDLERS, ...handlers };
  const calls: Calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: unknown, init?: { body?: unknown }) => {
      const path = new URL(String(url), "http://localhost").pathname;
      if (path.startsWith("/auth/")) {
        const parsed = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        calls.push({ operation: `AUTH ${path}`, variables: parsed });
        const route: ((body: Record<string, unknown>) => AuthAnswer) | undefined =
          authRoutes[path] ?? (path === "/auth/me" ? () => ({ body: NO_LOGIN_ME }) : undefined);
        const answer: AuthAnswer = route ? route(parsed) : { status: 404, body: { error: "sem rota no teste" } };
        const status = answer.status ?? (answer.body === undefined ? 204 : 200);
        return new Response(status === 204 ? null : JSON.stringify(answer.body ?? {}), {
          status,
          headers: { "content-type": "application/json" },
        });
      }
      const body = JSON.parse(String(init?.body ?? "{}")) as {
        operationName?: string;
        variables?: Record<string, unknown>;
      };
      const operation = body.operationName ?? "";
      const variables = body.variables ?? {};
      calls.push({ operation, variables });
      const handler = all[operation];
      let payload: unknown;
      try {
        if (!handler) throw new Error(`operação sem handler no teste: ${operation}`);
        payload = { data: handler(variables) };
      } catch (error) {
        payload = { data: null, errors: [{ message: (error as Error).message }] };
      }
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return calls;
}

/** A aplicação inteira (mesmas rotas e providers do `main.tsx`) numa rota. */
export function renderApp(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const client = createQueryClient();
  const view = render(
    <AppProviders client={client}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { ...view, router, client };
}
