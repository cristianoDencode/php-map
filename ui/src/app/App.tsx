import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, type ReactNode, Suspense } from "react";
import type { RouteObject } from "react-router";
import { Toaster, toast } from "sonner";
import { AdminOnly } from "../components/AdminOnly";
import { LoadingRows } from "../components/ui";
import { AccessPage } from "../routes/Access";
import { NotFound } from "../routes/ComingSoon";
import { HomePage } from "../routes/Home";
import { LoginPage } from "../routes/Login";
import { SetupPage } from "../routes/Setup";
import { NodeDetailPage } from "../routes/NodeDetail";
import { RulesPage } from "../routes/Rules";
import { RulesIndexPage } from "../routes/RulesIndex";
import { ProjectsPage } from "../routes/Projects";
import { PublishPage } from "../routes/Publish";
import { ScanPage } from "../routes/Scan";
import { SearchPage } from "../routes/Search";
import { CodePanelProvider } from "./code-panel";
import { Layout } from "./Layout";
import { ThemeProvider, useTheme } from "./theme";

// Os diagramas trazem o Cytoscape (~700 KB): carregados só quando a página abre.
const ImpactPage = lazy(async () => ({ default: (await import("../routes/Impact")).ImpactPage }));
const TracePage = lazy(async () => ({ default: (await import("../routes/Trace")).TracePage }));
const ExplorePage = lazy(async () => ({ default: (await import("../routes/Explore")).ExplorePage }));
const ModulesPage = lazy(async () => ({ default: (await import("../routes/Modules")).ModulesPage }));
const ModuleSheetPage = lazy(async () => ({ default: (await import("../routes/ModuleSheet")).ModuleSheetPage }));
const SharedTablesPage = lazy(async () => ({ default: (await import("../routes/SharedTables")).SharedTablesPage }));
const FileMenusPage = lazy(async () => ({ default: (await import("../routes/FileMenus")).FileMenusPage }));
const CommunicationPage = lazy(async () => ({
  default: (await import("../routes/Communication")).CommunicationPage,
}));

function Deferred({ children }: { children: ReactNode }) {
  return <Suspense fallback={<LoadingRows rows={8} label="Carregando o diagrama" />}>{children}</Suspense>;
}

export const routes: RouteObject[] = [
  { path: "/login", element: <LoginPage /> },
  { path: "/configuracao", element: <SetupPage /> },
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <HomePage /> },
      { path: "busca", element: <SearchPage /> },
      { path: "no/:id", element: <NodeDetailPage /> },
      { path: "regras", element: <RulesIndexPage /> },
      { path: "regras/:id", element: <RulesPage /> },
      { path: "menus", element: <Deferred><FileMenusPage /></Deferred> },
      { path: "comunicacao", element: <Deferred><CommunicationPage /></Deferred> },
      { path: "projetos", element: <ProjectsPage /> },
      { path: "impacto", element: <Deferred><ImpactPage /></Deferred> },
      { path: "trace", element: <Deferred><TracePage /></Deferred> },
      { path: "explorar/:id", element: <Deferred><ExplorePage /></Deferred> },
      { path: "modulos", element: <Deferred><ModulesPage /></Deferred> },
      { path: "modulos/:id", element: <Deferred><ModuleSheetPage /></Deferred> },
      { path: "tabelas", element: <Deferred><SharedTablesPage /></Deferred> },
      { path: "scan", element: <AdminOnly><ScanPage /></AdminOnly> },
      { path: "publicar", element: <AdminOnly><PublishPage /></AdminOnly> },
      { path: "acesso", element: <AdminOnly><AccessPage /></AdminOnly> },
      { path: "*", element: <NotFound /> },
    ],
  },
];

/**
 * Erros da API viram toast em português com "tentar de novo". Consultas e mutações com
 * `meta: { silent: true }` mostram o erro na própria página e não geram toast.
 */
export function createQueryClient(): QueryClient {
  const client: QueryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false, staleTime: 60_000 } },
    queryCache: new QueryCache({
      onError: (error, query) => {
        if (query.meta?.silent) return;
        toast.error(error.message, {
          id: JSON.stringify(query.queryKey),
          action: {
            label: "Tentar de novo",
            onClick: () => void client.refetchQueries({ queryKey: query.queryKey }),
          },
        });
      },
    }),
    mutationCache: new MutationCache({
      onError: (error, _variables, _result, mutation) => {
        if (!mutation.meta?.silent) toast.error(error.message);
      },
    }),
  });
  return client;
}

function ThemedToaster() {
  const { theme } = useTheme();
  return <Toaster theme={theme} richColors closeButton position="bottom-right" />;
}

export function AppProviders({ client, children }: { client: QueryClient; children: ReactNode }) {
  return (
    <ThemeProvider>
      <QueryClientProvider client={client}>
        <CodePanelProvider>{children}</CodePanelProvider>
        <ThemedToaster />
      </QueryClientProvider>
    </ThemeProvider>
  );
}
