import { FileCode2 } from "lucide-react";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { CodeViewer } from "../components/CodeViewer";
import { Sheet } from "../components/ui";
import { isSourcePath } from "../lib/ids";
import { cn } from "../lib/utils";

type Target = { path: string; line?: number | null };
type Navigate = (to: string) => void;
type CodePanelState = {
  open: (target: Target) => void;
  /** Fecha o painel e navega (o painel fica fora do router: quem navega é o `CodePanelNavigator`). */
  go: (to: string) => void;
  setNavigator: (navigate: Navigate | null) => void;
};

const CodePanelContext = createContext<CodePanelState | null>(null);

/** Painel lateral "Código": uma instância para a aplicação inteira. */
export function CodePanelProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<Target | null>(null);
  const navigator = useRef<Navigate | null>(null);
  const open = useCallback((next: Target) => setTarget(next), []);
  const go = useCallback((to: string) => {
    setTarget(null);
    navigator.current?.(to);
  }, []);
  const setNavigator = useCallback((navigate: Navigate | null) => {
    navigator.current = navigate;
  }, []);
  const value = useMemo(() => ({ open, go, setNavigator }), [open, go, setNavigator]);
  return (
    <CodePanelContext.Provider value={value}>
      {children}
      <Sheet
        open={target !== null}
        onOpenChange={(isOpen) => !isOpen && setTarget(null)}
        title={target ? `${target.path}${target.line ? `:${target.line}` : ""}` : ""}
      >
        {target ? (
          <CodeViewer
            path={target.path}
            line={target.line}
            onShowMenus={(path) => go(`/menus?fonte=${encodeURIComponent(path)}`)}
          />
        ) : null}
      </Sheet>
    </CodePanelContext.Provider>
  );
}

/** Dentro do router (no `Layout`): dá ao painel o `navigate` para o "Ver menus". */
export function CodePanelNavigator() {
  const navigate = useNavigate();
  const { setNavigator } = useCodePanel();
  useEffect(() => {
    setNavigator((to) => void navigate(to));
    return () => setNavigator(null);
  }, [navigate, setNavigator]);
  return null;
}

export function useCodePanel(): CodePanelState {
  const state = useContext(CodePanelContext);
  if (!state) throw new Error("useCodePanel fora do CodePanelProvider");
  return state;
}

/**
 * Evidência `arquivo:linha` que abre o painel. Evidência que não é arquivo do e-cidade
 * (`db:db_menu`, do catálogo) aparece como texto.
 */
export function EvidenceLink({
  path,
  line,
  className,
}: {
  path: unknown;
  line?: unknown;
  className?: string;
}) {
  const panel = useCodePanel();
  const lineNumber = typeof line === "number" ? line : null;
  const text = `${String(path ?? "")}${lineNumber ? `:${lineNumber}` : ""}`;
  if (!isSourcePath(path)) {
    return <span className={cn("font-mono text-xs text-muted-foreground", className)}>{text}</span>;
  }
  return (
    <button
      type="button"
      onClick={() => panel.open({ path, line: lineNumber })}
      className={cn(
        "inline-flex max-w-full items-center gap-1 truncate font-mono text-xs text-primary hover:underline",
        className,
      )}
      title={`Abrir ${text} no painel de código`}
    >
      <FileCode2 className="h-3 w-3 shrink-0" aria-hidden="true" />
      <span className="truncate">{text}</span>
    </button>
  );
}
