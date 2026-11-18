import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Copy, ExternalLink, ListTree } from "lucide-react";
import { Highlight, type Language, type PrismTheme, themes } from "prism-react-renderer";
import { useEffect, useRef } from "react";
import { useTheme } from "../app/theme";
import { graphql } from "../graphql/generated";
import { request } from "../lib/api";
import { languageOf, loadLanguage } from "../lib/prism";
import { cn } from "../lib/utils";
import { Button, LoadingRows, Notice } from "./ui";

export const WorkspaceQuery = graphql(`
  query Workspace {
    workspace {
      source
      origin
      exists
      isEcidade
      graphSource
      marker
    }
  }
`);

const SourceFileQuery = graphql(`
  query SourceFile($path: String!, $line: Int) {
    sourceFile(path: $path, line: $line, context: 200) {
      path
      totalLines
      startLine
      lines
      highlight
      truncated
      sizeBytes
    }
  }
`);

/** Acima disso a lista é virtualizada e só as linhas visíveis são realçadas. */
export const VIRTUALIZE_ABOVE = 2000;
const ROW_HEIGHT = 20;

export function useWorkspace() {
  return useQuery({ queryKey: ["workspace"], queryFn: () => request(WorkspaceQuery) });
}

/** `vscode://file/C:/pasta/arquivo:12` ou, no Linux, `vscode://file/pasta/arquivo:12`. */
export function vscodeHref(source: string, path: string, line?: number | null): string {
  const root = source.replace(/\\/g, "/").replace(/\/+$/, "").replace(/^\/+/, "");
  return `vscode://file/${root}/${path}${line ? `:${line}` : ""}`;
}

export function CodeViewer({
  path,
  line,
  onShowMenus,
}: {
  path: string;
  line?: number | null;
  /** "Ver menus" do arquivo aberto; sem ele, o botão não aparece. */
  onShowMenus?: (path: string) => void;
}) {
  const workspace = useWorkspace();
  const hasFolder = Boolean(workspace.data?.workspace.source && workspace.data.workspace.exists);
  const file = useQuery({
    queryKey: ["sourceFile", path, line ?? null],
    queryFn: () => request(SourceFileQuery, { path, line: line ?? null }),
    enabled: hasFolder,
    meta: { silent: true }, // o erro aparece no próprio painel
  });
  const language = languageOf(path);
  const grammar = useQuery({
    queryKey: ["prism", language],
    queryFn: () => loadLanguage(language).then(() => true),
    staleTime: Infinity,
  });

  if (workspace.isPending) return <LoadingRows label="Carregando a pasta do e-cidade" />;
  if (!hasFolder) {
    return (
      <div className="p-5">
        <Notice tone="warning">
          <p className="font-medium">Código-fonte indisponível: nenhuma pasta do e-cidade em uso.</p>
          <p className="mt-1">
            Informe a pasta com <code className="font-mono">php-map ui --source &lt;pasta&gt;</code>
            ; ou escolha a pasta em «Atualizar mapeamento».
          </p>
        </Notice>
      </div>
    );
  }
  if (file.isPending || grammar.isPending) return <LoadingRows rows={12} label="Carregando o arquivo" />;
  if (file.isError) {
    return (
      <div className="p-5">
        <Notice tone="warning">{file.error.message}</Notice>
      </div>
    );
  }
  const source = file.data.sourceFile;
  const folder = workspace.data?.workspace.source ?? "";
  const end = source.startLine + source.lines.length - 1;
  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b px-5 py-2 text-xs">
        <span className="text-muted-foreground">
          {source.totalLines.toLocaleString("pt-BR")} linhas
          {source.highlight ? ` · linha ${source.highlight}` : ""}
        </span>
        <span className="flex-1" />
        <Button
          variant="outline"
          size="sm"
          onClick={() => void navigator.clipboard?.writeText(source.path)}
          aria-label="Copiar o caminho do arquivo"
        >
          <Copy className="h-3.5 w-3.5" aria-hidden="true" /> Copiar caminho
        </Button>
        {onShowMenus ? (
          <Button variant="outline" size="sm" onClick={() => onShowMenus(source.path)}>
            <ListTree className="h-3.5 w-3.5" aria-hidden="true" /> Ver menus
          </Button>
        ) : null}
        <a
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-input px-3 hover:bg-muted"
          href={vscodeHref(folder, source.path, source.highlight)}
        >
          <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" /> Abrir no VS Code
        </a>
      </div>
      {source.truncated ? (
        <div className="px-5 pt-3">
          <Notice tone="warning">
            Arquivo grande: mostrando as linhas {source.startLine.toLocaleString("pt-BR")}–
            {end.toLocaleString("pt-BR")} de {source.totalLines.toLocaleString("pt-BR")}.
          </Notice>
        </div>
      ) : null}
      <CodeLines
        lines={source.lines}
        startLine={source.startLine}
        highlight={source.highlight ?? null}
        language={grammar.data ? language : "plain"}
      />
    </div>
  );
}

function CodeLines(props: {
  lines: string[];
  startLine: number;
  highlight: number | null;
  language: string;
}) {
  const { theme } = useTheme();
  const prismTheme = theme === "dark" ? themes.vsDark : themes.github;
  return props.lines.length > VIRTUALIZE_ABOVE ? (
    <VirtualLines {...props} prismTheme={prismTheme} />
  ) : (
    <WholeFile {...props} prismTheme={prismTheme} />
  );
}

type LinesProps = {
  lines: string[];
  startLine: number;
  highlight: number | null;
  language: string;
  prismTheme: PrismTheme;
};

function Row({
  number,
  current,
  children,
}: {
  number: number;
  current: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      data-line={number}
      aria-current={current ? "location" : undefined}
      className={cn("flex min-h-5 leading-5", current && "bg-code-highlight")}
    >
      <span className="w-14 shrink-0 select-none pr-3 text-right text-muted-foreground">
        {number}
      </span>
      <span className="whitespace-pre pr-4">{children}</span>
    </div>
  );
}

/** Arquivo inteiro realçado de uma vez (comentários de várias linhas saem certos). */
function WholeFile({ lines, startLine, highlight, language, prismTheme }: LinesProps) {
  const current = useRef<HTMLDivElement>(null);
  useEffect(() => {
    current.current?.scrollIntoView?.({ block: "center" });
  }, [highlight, lines]);
  return (
    <div className="min-h-0 flex-1 overflow-auto py-2 font-mono text-[13px]" tabIndex={0}>
      <Highlight theme={prismTheme} code={lines.join("\n")} language={language as Language}>
        {({ tokens, getTokenProps }) => (
          <div>
            {tokens.map((tokenLine, index) => {
              const number = startLine + index;
              return (
                <div key={number} ref={number === highlight ? current : undefined}>
                  <Row number={number} current={number === highlight}>
                    {tokenLine.map((token, key) => (
                      <span key={key} {...getTokenProps({ token })} />
                    ))}
                  </Row>
                </div>
              );
            })}
          </div>
        )}
      </Highlight>
    </div>
  );
}

/** Arquivo longo: virtualizado, cada linha realçada sozinha e só quando aparece. */
function VirtualLines({ lines, startLine, highlight, language, prismTheme }: LinesProps) {
  const parent = useRef<HTMLDivElement>(null);
  // O React Compiler não memoiza o useVirtualizer (funções da biblioteca): esperado.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: lines.length,
    getScrollElement: () => parent.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 20,
  });
  useEffect(() => {
    if (highlight) virtualizer.scrollToIndex(highlight - startLine, { align: "center" });
  }, [highlight, startLine, virtualizer]);
  return (
    <div ref={parent} className="min-h-0 flex-1 overflow-auto py-2 font-mono text-[13px]" tabIndex={0}>
      <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
        {virtualizer.getVirtualItems().map((item) => {
          const number = startLine + item.index;
          return (
            <div
              key={item.key}
              style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${item.start}px)` }}
            >
              <Highlight theme={prismTheme} code={lines[item.index] ?? ""} language={language as Language}>
                {({ tokens, getTokenProps }) => (
                  <Row number={number} current={number === highlight}>
                    {(tokens[0] ?? []).map((token, key) => (
                      <span key={key} {...getTokenProps({ token })} />
                    ))}
                  </Row>
                )}
              </Highlight>
            </div>
          );
        })}
      </div>
    </div>
  );
}
