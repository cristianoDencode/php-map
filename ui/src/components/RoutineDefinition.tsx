import { useInfiniteQuery } from "@tanstack/react-query";
import { Copy } from "lucide-react";
import { toast } from "sonner";
import { graphql } from "../graphql/generated";
import { request } from "../lib/api";
import { copyText } from "../lib/utils";
import { Button, Card, LoadingRows, Notice } from "./ui";

const RoutineSourceQuery = graphql(`
  query RoutineSource($id: ID!, $offset: Int!, $limit: Int!) {
    hasRoutineTexts
    routineSource(id: $id, offset: $offset, limit: $limit) {
      id
      schema
      name
      language
      generated
      readAt
      sha1
      totalLines
      startLine
      endLine
      lines
      truncated
    }
  }
`);

/** Linhas pedidas a cada "Mostrar mais". */
export const ROUTINE_PAGE = 200;

/** `2026-11-03T10:00:00-03:00` → `03/11/2026 10:00`; texto que não é data volta como veio. */
export function readAtLabel(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

/**
 * Definição de uma função da base: o texto que o scan leu do banco, em páginas de
 * 200 linhas. É o texto da data do scan: a base pode ter mudado depois.
 */
export function RoutineDefinition({ id }: { id: string }) {
  const query = useInfiniteQuery({
    queryKey: ["routineSource", id],
    initialPageParam: 1,
    queryFn: ({ pageParam }) => request(RoutineSourceQuery, { id, offset: pageParam, limit: ROUTINE_PAGE }),
    getNextPageParam: (last) => {
      const page = last.routineSource;
      return page && page.endLine < page.totalLines ? page.endLine + 1 : undefined;
    },
  });
  if (query.isPending) return <LoadingRows rows={6} label="Carregando a definição" />;
  if (query.isError) return <Notice tone="warning">{query.error.message}</Notice>;

  const pages = query.data.pages;
  const first = pages[0]?.routineSource;
  if (!first) {
    return pages[0]?.hasRoutineTexts ? (
      <Notice>
        Esta função não tem texto no grafo: ela pode ter sido criada depois do scan. Rode o scan novo para trazê-la.
      </Notice>
    ) : (
      <Notice tone="warning">
        Este grafo foi escaneado sem ler o banco, então não guarda o texto das funções. Rode o scan novo com o
        schema do banco (Atualizar mapeamento, fonte do schema: banco).
      </Notice>
    );
  }
  const lines = pages.flatMap((p) => p.routineSource?.lines ?? []);
  const width = String(first.totalLines).length;
  const copy = async () => {
    const ok = await copyText(lines.join("\n"));
    if (ok) toast.success("Texto copiado.");
    else toast.error("O navegador não deixou copiar.");
  };
  return (
    <div className="space-y-3">
      <Card className="flex flex-wrap items-center justify-between gap-3 px-5 py-3 text-sm">
        <p className="text-muted-foreground">
          Texto lido da base em <strong className="text-foreground">{readAtLabel(first.readAt)}</strong> (data do
          scan) · {first.language} · {first.totalLines.toLocaleString("pt-BR")} linhas
        </p>
        <div className="flex items-center gap-3">
          <Button variant="outline" size="sm" onClick={() => void copy()}>
            <Copy className="h-4 w-4" aria-hidden="true" /> Copiar o texto carregado
          </Button>
        </div>
      </Card>
      {first.generated ? (
        <Notice>
          Função gerada pela auditoria do e-cidade (<code className="font-mono">fc_auditoria_tabela_*</code>): há
          centenas, quase iguais, uma por tabela.
        </Notice>
      ) : null}
      <Card className="overflow-x-auto">
        <pre aria-label="Definição da função" className="min-w-max p-4 font-mono text-xs leading-5">
          {lines.map((text, index) => (
            <div key={index} className="flex gap-4">
              <span className="select-none text-right text-muted-foreground" style={{ minWidth: `${width}ch` }}>
                {index + 1}
              </span>
              <code>{text}</code>
            </div>
          ))}
        </pre>
      </Card>
      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>
          Mostrando {lines.length.toLocaleString("pt-BR")} de {first.totalLines.toLocaleString("pt-BR")} linhas
        </span>
        {query.hasNextPage ? (
          <Button variant="outline" size="sm" disabled={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>
            {query.isFetchingNextPage ? "Carregando…" : "Mostrar mais"}
          </Button>
        ) : null}
      </div>
    </div>
  );
}
