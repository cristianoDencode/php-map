import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import {
  Cable,
  FolderGit2,
  FolderOpen,
  GitBranch,
  KeyRound,
  Home,
  Layers,
  ListTree,
  LoaderCircle,
  Map as MapIcon,
  Radar,
  RefreshCw,
  ScrollText,
  Search,
  Table2,
  TriangleAlert,
  UploadCloud,
} from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, Navigate, NavLink, Outlet, useLocation } from "react-router";
import { AccountMenu } from "../components/AccountMenu";
import { CommandPalette } from "../components/CommandPalette";
import { ProjectSwitcher } from "../components/ProjectSwitcher";
import { LoadingRows } from "../components/ui";
import { CodePanelNavigator } from "./code-panel";
import { useWorkspace } from "../components/CodeViewer";
import { graphql } from "../graphql/generated";
import { request } from "../lib/api";
import { AUTH_KEY, authEvents, FORBIDDEN_EVENT, LOGIN_REQUIRED_EVENT, useAuth, useIsAdmin } from "../lib/auth";
import { activeOf, useProjects } from "../lib/projects";
import { formatElapsed, isRunning, useScanJob, useScanWatcher } from "../lib/scan";
import { cn, formatDateTime } from "../lib/utils";
import { ThemeToggle } from "./theme";

export const StatsQuery = graphql(`
  query Stats {
    stats
  }
`);

export type Stats = {
  meta?: Record<string, string>;
  nodes?: Record<string, number>;
  edges?: { by_type?: Record<string, number>; by_source?: Record<string, number> };
  catalog?: {
    modules?: {
      files_with_catalog?: number;
      screens_classified?: number;
      daos_classified?: number;
      files_without_module?: number;
    } | null;
  } | null;
  analytics?: { screens?: number; screens_in_menu?: number; tables_shared?: number } | null;
  served?: { from_interface?: boolean };
};

export function useStats(enabled = true) {
  return useQuery({
    queryKey: ["stats"],
    queryFn: async () => (await request(StatsQuery)).stats as Stats,
    enabled,
  });
}

type NavItem = { to: string; label: string; icon: typeof Home; end?: boolean; adminOnly?: boolean };

const NAV: NavItem[] = [
  { to: "/", label: "Início", icon: Home, end: true },
  { to: "/busca", label: "Busca", icon: Search },
  { to: "/regras", label: "Regras de negócio", icon: ScrollText },
  { to: "/menus", label: "Menus por fonte", icon: ListTree },
  { to: "/comunicacao", label: "Comunicação", icon: Cable },
  { to: "/impacto", label: "Impacto", icon: Radar },
  { to: "/modulos", label: "Módulos", icon: Layers },
  { to: "/tabelas?compartilhadas=1", label: "Tabelas compartilhadas", icon: Table2 },
  { to: "/trace", label: "Trace", icon: GitBranch },
  { to: "/projetos", label: "Projetos", icon: FolderGit2 },
  { to: "/scan", label: "Atualizar mapeamento", icon: RefreshCw, adminOnly: true },
  { to: "/publicar", label: "MCP e túnel", icon: UploadCloud, adminOnly: true },
  { to: "/acesso", label: "Acesso", icon: KeyRound, adminOnly: true },
];

/** Telas que funcionam com o projeto ativo ainda sem grafo (cadastro e scan). */
const WITHOUT_GRAPH = ["/scan", "/projetos", "/publicar", "/acesso"];

/** Mesma pasta? Maiúsculas só são ignoradas em caminho do Windows (`D:/…`): no Linux, não. */
export function samePath(a: string, b: string): boolean {
  const norm = (p: string) => {
    const path = p.replace(/\\/g, "/").replace(/\/+$/, "");
    return /^[a-z]:/i.test(path) ? path.toLowerCase() : path;
  };
  return norm(a) === norm(b);
}

export function WorkspaceIndicator() {
  const { data } = useWorkspace();
  const ws = data?.workspace;
  if (!ws) return null;
  const folder = ws.source ?? "nenhuma pasta";
  const marker = ws.marker ?? "libs/db_stdlib.php";
  const short = ws.source ? (ws.source.replace(/\\/g, "/").split("/").pop() ?? ws.source) : "—";
  let warning: string | null = null;
  if (!ws.source) warning = "Nenhuma pasta do projeto: o código-fonte fica indisponível.";
  else if (!ws.exists) warning = "A pasta do projeto não existe mais neste computador.";
  else if (!ws.isEcidade) warning = `A pasta não parece um projeto do perfil (falta ${marker}).`;
  else if (ws.graphSource && !samePath(ws.source, ws.graphSource))
    warning = `O grafo foi gerado de outra pasta: ${ws.graphSource}`;
  return (
    <div
      className={cn(
        "hidden items-center gap-1.5 rounded-md border px-2 py-1 text-xs lg:flex",
        warning ? "border-warning bg-warning-bg text-warning" : "text-muted-foreground",
      )}
      title={warning ? `${folder}\n${warning}` : folder}
      aria-label={warning ? `Pasta do projeto: ${folder}. ${warning}` : `Pasta do projeto: ${folder}`}
    >
      {warning ? (
        <TriangleAlert className="h-3.5 w-3.5" aria-hidden="true" />
      ) : (
        <FolderOpen className="h-3.5 w-3.5" aria-hidden="true" />
      )}
      <span className="max-w-40 truncate">{short}</span>
    </div>
  );
}

/** "Scan em andamento: <etapa> (mm:ss)" em todas as páginas. */
export function ScanIndicator() {
  const job = useScanJob();
  useScanWatcher(job.data);
  if (!isRunning(job.data) || !job.data) return null;
  const text = `Scan em andamento: ${job.data.stageLabel ?? "preparando"} (${formatElapsed(job.data.elapsedSeconds)})`;
  return (
    <Link
      to="/scan"
      aria-live="polite"
      className="flex items-center gap-1.5 rounded-md border border-primary/40 px-2 py-1 text-xs text-primary hover:bg-muted"
    >
      <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
      <span className="max-w-72 truncate">{text}</span>
    </Link>
  );
}

/** O projeto ativo ainda não tem grafo: nenhuma consulta funciona, então só o caminho para o scan. */
function NoGraph({ name, admin }: { name: string; admin: boolean }) {
  if (!admin)
    return (
      <div className="mx-auto max-w-xl space-y-3 rounded-xl border bg-card px-6 py-8 text-center">
        <h1 className="text-xl font-semibold">O projeto {name} ainda não tem grafo</h1>
        <p className="text-sm text-muted-foreground">Peça a um administrador para rodar o scan deste projeto.</p>
      </div>
    );
  return (
    <div className="mx-auto max-w-xl space-y-3 rounded-xl border bg-card px-6 py-8 text-center">
      <h1 className="text-xl font-semibold">O projeto {name} ainda não tem grafo</h1>
      <p className="text-sm text-muted-foreground">
        Rode o scan para mapear a pasta dele. Enquanto isso, dá para escolher outro projeto no seletor do topo.
      </p>
      <Link
        to="/scan"
        className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground hover:opacity-90"
      >
        <RefreshCw className="h-4 w-4" aria-hidden="true" /> Rodar o scan
      </Link>
    </div>
  );
}

/**
 * Guarda de login: sem usuário no cofre vai para `/configuracao`; com login exigido e sem
 * sessão, para `/login?volta=…`. O servidor
 * é quem barra de verdade; aqui só se evita mostrar telas que falhariam com 401.
 */
export function Layout() {
  const auth = useAuth();
  const client = useQueryClient();
  const location = useLocation();
  useEffect(() => {
    const onLogin = () => void client.invalidateQueries({ queryKey: AUTH_KEY });
    authEvents.addEventListener(LOGIN_REQUIRED_EVENT, onLogin);
    authEvents.addEventListener(FORBIDDEN_EVENT, onLogin);
    return () => {
      authEvents.removeEventListener(LOGIN_REQUIRED_EVENT, onLogin);
      authEvents.removeEventListener(FORBIDDEN_EVENT, onLogin);
    };
  }, [client]);
  if (auth.isPending) return <LoadingRows rows={6} label="Verificando o acesso" />;
  if (auth.data?.setupRequired) return <Navigate to="/configuracao" replace />;
  if (auth.data?.loginRequired && !auth.data.user) {
    const back = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?volta=${back}`} replace />;
  }
  return <Shell />;
}

function Shell() {
  const admin = useIsAdmin();
  const projects = useProjects();
  const active = activeOf(projects.data);
  const path = useLocation().pathname;
  const noGraph = active !== null && !active.hasGraph;
  const stats = useStats(!noGraph && !projects.isPending);
  const scannedAt = stats.data?.meta?.scanned_at;
  const fromInterface = stats.data?.served?.from_interface ?? false;
  return (
    <div className="flex min-h-screen">
      <CodePanelNavigator />
      <aside className="sticky top-0 hidden h-screen w-56 shrink-0 flex-col border-r bg-card md:flex">
        <div className="flex items-center gap-2 px-5 py-4">
          <MapIcon className="h-5 w-5 text-primary" aria-hidden="true" />
          <span className="font-semibold">php-map</span>
        </div>
        <nav aria-label="Seções" className="flex flex-col gap-1 px-3">
          {NAV.filter((item) => admin || !item.adminOnly).map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 rounded-md px-3 py-2 text-sm",
                  isActive ? "bg-accent font-medium text-accent-foreground" : "hover:bg-muted",
                )
              }
            >
              <Icon className="h-4 w-4" aria-hidden="true" />
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex items-center gap-3 border-b bg-background/90 px-6 py-3 backdrop-blur">
          <CommandPalette />
          <span className="flex-1" />
          <ProjectSwitcher />
          <ScanIndicator />
          <WorkspaceIndicator />
          <span className="hidden text-xs text-muted-foreground lg:inline" title="Data do scan">
            {fromInterface
              ? `Grafo gerado pela interface em ${formatDateTime(scannedAt)}`
              : `Scan: ${formatDateTime(scannedAt)}`}
          </span>
          <ThemeToggle />
          <AccountMenu />
        </header>
        <main className="mx-auto w-full max-w-7xl flex-1 px-6 py-6">
          {projects.isPending ? (
            // Sem saber qual projeto está ativo, as telas consultariam um grafo que pode não existir.
            <LoadingRows rows={6} label="Carregando o projeto" />
          ) : noGraph && active && !WITHOUT_GRAPH.some((p) => path.startsWith(p)) ? (
            <NoGraph name={active.name} admin={admin} />
          ) : (
            <Outlet />
          )}
        </main>
      </div>
    </div>
  );
}
