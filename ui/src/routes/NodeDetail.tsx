import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  ChevronLeft,
  ChevronRight,
  FileCode2,
  GitBranch,
  ListTree,
  Radar,
  ScrollText,
} from "lucide-react";
import { type ReactNode, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { EvidenceLink, useCodePanel } from "../app/code-panel";
import { CodeViewer } from "../components/CodeViewer";
import { LayersSection } from "../components/LayerChain";
import { ConfidenceBadge, LabelBadge, NodeLink } from "../components/NodeBits";
import { RoutineDefinition } from "../components/RoutineDefinition";
import { RoutineOriginSection } from "../components/RoutineOrigin";
import {
  Button,
  Card,
  LoadingRows,
  Notice,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "../components/ui";
import { graphql } from "../graphql/generated";
import { request } from "../lib/api";
import { keyOf, rulesHref, shortName } from "../lib/ids";
import { edgeVerb, sourceLabel } from "../lib/labels";

const NodeQuery = graphql(`
  query NodeDetail($id: ID!) {
    node(id: $id) {
      id
      label
      name
      path
      modules
      attrs
      memberships {
        module
        name
        source
        confidence
      }
    }
  }
`);

const RelationsQuery = graphql(`
  query Relations($id: ID!, $direction: Direction!, $limit: Int!, $offset: Int!) {
    node(id: $id) {
      id
      neighbors(direction: $direction, limit: $limit, offset: $offset) {
        total
        items {
          src
          dst
          type
          source
          confidence
          evidence
          occurrences
        }
        nodes {
          id
          name
        }
      }
    }
  }
`);

export const RELATIONS_PAGE = 50;

type Attrs = Record<string, unknown>;

function str(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function num(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

/** Onde está o código do nó: o próprio arquivo, ou o arquivo e a linha da declaração. */
function codeTarget(label: string, attrs: Attrs): { path: string; line: number | null } | null {
  const path = str(attrs.path);
  if (!path || !["File", "Function", "Method", "Class"].includes(label)) return null;
  return { path, line: label === "File" ? null : num(attrs.line_start) };
}

export function NodeDetailPage() {
  const id = useParams().id ?? "";
  const [search] = useSearchParams();
  const node = useQuery({
    queryKey: ["node", id],
    queryFn: () => request(NodeQuery, { id }),
    enabled: id.length > 0,
  });
  const panel = useCodePanel();

  if (node.isPending) return <LoadingRows rows={8} label="Carregando o nó" />;
  if (node.isError) return <Notice tone="warning">{node.error.message}</Notice>;
  const data = node.data.node;
  if (!data) {
    return (
      <Notice tone="warning">
        Nó não encontrado no grafo: <code className="font-mono">{id}</code>. Use a busca (Ctrl K).
      </Notice>
    );
  }
  const attrs = (data.attrs ?? {}) as Attrs;
  const code = codeTarget(data.label, attrs);
  const hasLayers = ["File", "Class", "Method"].includes(data.label);
  return (
    <div className="space-y-5">
      <header className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <LabelBadge label={data.label} />
          {attrs.is_infra ? <span className="text-xs text-muted-foreground">infraestrutura</span> : null}
          {attrs.resolved === false ? (
            <span className="text-xs text-muted-foreground" title="Citado no código, sem definição no schema ou no scan">
              só referenciado
            </span>
          ) : null}
        </div>
        <h1 className="break-all text-2xl font-semibold">{shortName(data.id, data.name)}</h1>
        <p className="break-all font-mono text-xs text-muted-foreground">{keyOf(data.id)}</p>
        {data.memberships.length > 0 ? (
          <div className="flex flex-wrap items-center gap-2 text-sm" aria-label="Módulos">
            {data.memberships.map((m) => (
              <span key={m.module} className="inline-flex items-center gap-1.5 rounded-md border px-2 py-1">
                <NodeLink id={m.module} name={m.name} />
                <span className="text-xs text-muted-foreground">{sourceLabel(m.source)}</span>
                <ConfidenceBadge confidence={m.confidence} source={m.source} />
              </span>
            ))}
          </div>
        ) : data.label === "File" ? (
          <p className="text-xs text-muted-foreground">Sem módulo (nem no catálogo, nem por regra de nome).</p>
        ) : null}
        <div className="flex flex-wrap gap-2 pt-1">
          {code ? (
            <Button variant="outline" size="sm" onClick={() => panel.open(code)}>
              <FileCode2 className="h-4 w-4" aria-hidden="true" /> Ver código
            </Button>
          ) : null}
          {data.label === "File" ? (
            <Link
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-input px-3 text-sm hover:bg-muted"
              to={rulesHref(data.id)}
            >
              <ScrollText className="h-4 w-4" aria-hidden="true" /> Ver regras
            </Link>
          ) : null}
          {data.label === "File" ? (
            <Link
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-input px-3 text-sm hover:bg-muted"
              to={`/trace?entrada=${encodeURIComponent(keyOf(data.id))}`}
            >
              <GitBranch className="h-4 w-4" aria-hidden="true" /> Ver trace
            </Link>
          ) : null}
          {data.label === "File" ? (
            <Link
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-input px-3 text-sm hover:bg-muted"
              to={`/menus?fonte=${encodeURIComponent(keyOf(data.id))}`}
            >
              <ListTree className="h-4 w-4" aria-hidden="true" /> Ver menus
            </Link>
          ) : null}
          {data.label === "Table" ? (
            <Link
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-input px-3 text-sm hover:bg-muted"
              to={`/impacto?tabela=${encodeURIComponent(keyOf(data.id))}`}
            >
              <Radar className="h-4 w-4" aria-hidden="true" /> Ver impacto
            </Link>
          ) : null}
        </div>
      </header>

      <Tabs defaultValue={search.get("aba") === "camadas" && hasLayers ? "camadas" : "resumo"}>
        <TabsList aria-label="Seções do nó">
          <TabsTrigger value="resumo">Resumo</TabsTrigger>
          <TabsTrigger value="relacoes">Relações</TabsTrigger>
          {hasLayers ? <TabsTrigger value="camadas">Camadas</TabsTrigger> : null}
          {data.label === "File" ? <TabsTrigger value="codigo">Código</TabsTrigger> : null}
          {data.label === "PgFunction" ? <TabsTrigger value="origem">Origem</TabsTrigger> : null}
          {data.label === "PgFunction" ? <TabsTrigger value="definicao">Definição</TabsTrigger> : null}
          <TabsTrigger value="atributos">Atributos</TabsTrigger>
        </TabsList>
        <TabsContent value="resumo" className="pt-4">
          <Summary label={data.label} attrs={attrs} />
        </TabsContent>
        <TabsContent value="relacoes" className="grid gap-4 pt-4 xl:grid-cols-2">
          <Relations id={data.id} incoming={false} />
          <Relations id={data.id} incoming />
        </TabsContent>
        {hasLayers ? (
          <TabsContent value="camadas" className="pt-4">
            <LayersSection id={data.id} label={data.label} />
          </TabsContent>
        ) : null}
        {data.label === "File" && code ? (
          <TabsContent value="codigo" className="pt-4">
            <Card className="h-[70vh] overflow-hidden">
              <CodeViewer path={code.path} />
            </Card>
          </TabsContent>
        ) : null}
        {data.label === "PgFunction" ? (
          <TabsContent value="origem" className="pt-4">
            <RoutineOriginSection id={data.id} />
          </TabsContent>
        ) : null}
        {data.label === "PgFunction" ? (
          <TabsContent value="definicao" className="pt-4">
            <RoutineDefinition id={data.id} />
          </TabsContent>
        ) : null}
        <TabsContent value="atributos" className="pt-4">
          <Card className="p-4">
            <JsonTree value={data.attrs} />
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}

// --------------------------------------------------------------------------- resumo

function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[12rem_1fr] gap-4 px-5 py-2 text-sm">
      <dt className="text-muted-foreground">{term}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

const PARSE_STATUS: Record<string, string> = {
  ok: "lido sem erros",
  partial: "lido em parte (há trechos com erro de sintaxe)",
  parse_error: "não foi possível ler (erro de sintaxe)",
};

/** Os atributos principais em linguagem natural, por tipo de nó. */
function Summary({ label, attrs }: { label: string; attrs: Attrs }) {
  const facts: ReactNode[] = [];
  const add = (term: string, value: ReactNode) => {
    if (value !== null && value !== undefined && value !== "") facts.push(<Fact key={term} term={term}>{value}</Fact>);
  };
  if (label === "File") {
    add("Caminho", str(attrs.path) ? <EvidenceLink path={attrs.path} /> : null);
    add("Linhas", num(attrs.lines)?.toLocaleString("pt-BR"));
    add("Leitura do código", PARSE_STATUS[str(attrs.parse_status) ?? ""] ?? str(attrs.parse_status));
    add("Prefixo de módulo", str(attrs.module_hint));
    add("Infraestrutura", attrs.is_infra ? "sim: fica fora do trace e do acoplamento" : null);
    const actions = Array.isArray(attrs.dispatch_actions) ? (attrs.dispatch_actions as unknown[]) : [];
    add("Ações despachadas", actions.length ? actions.map(String).join(", ") : null);
  } else if (label === "Table") {
    const catalog = (attrs.catalog ?? {}) as Attrs;
    add("Descrição (dicionário)", str(catalog.description));
    add("Rótulo", str(catalog.label));
    add("Prefixo das colunas", str(catalog.prefix));
    add("No schema", attrs.resolved === false ? "não: só citada no código" : "sim");
    const columns = Object.entries((attrs.columns_catalog ?? {}) as Record<string, Attrs>);
    add(
      "Colunas com rótulo",
      columns.length ? (
        <ul className="grid gap-1 sm:grid-cols-2">
          {columns.slice(0, 60).map(([name, column]) => (
            <li key={name}>
              <code className="font-mono text-xs">{name}</code>
              {str(column.label) ? <span className="text-muted-foreground"> — {str(column.label)}</span> : null}
            </li>
          ))}
          {columns.length > 60 ? <li className="text-muted-foreground">… e mais {columns.length - 60}</li> : null}
        </ul>
      ) : null,
    );
  } else if (label === "BusinessAction") {
    add("Caminho no menu", str(attrs.menu_path));
    const paths = Array.isArray(attrs.menu_paths) ? (attrs.menu_paths as unknown[]).map(String) : [];
    add("Também em", paths.length > 1 ? paths.slice(1).join(" · ") : null);
    add("Ajuda", str(attrs.help));
    add("Descrição técnica", str(attrs.desctec));
    add("Ativo", attrs.active === false ? "não (item desativado no menu)" : "sim");
    add("Parâmetros", str(attrs.params));
  } else if (label === "Module") {
    add("Descrição", str(attrs.description));
    const names = Array.isArray(attrs.names) ? (attrs.names as unknown[]).map(String) : [];
    add("Nomes no e-cidade", names.join(" · "));
    add("Código no dicionário", num(attrs.codmod));
    add("Código no menu", num(attrs.menu_id));
  } else if (label === "Function" || label === "Method" || label === "Class") {
    add("Declarado em", str(attrs.path) ? <EvidenceLink path={attrs.path} line={attrs.line_start} /> : null);
    add("Linhas", num(attrs.line_start) ? `${num(attrs.line_start)}–${num(attrs.line_end) ?? "?"}` : null);
    add("Classe", str(attrs.class));
    const declared = Array.isArray(attrs.declared_in) ? (attrs.declared_in as unknown[]).map(String) : [];
    add("Outras declarações", declared.length > 1 ? `${declared.length} arquivos declaram o mesmo nome` : null);
  } else if (label === "PgFunction") {
    add("Linguagem", str(attrs.language));
    add("Retorna", str(attrs.returns));
    const args = Array.isArray(attrs.args) ? (attrs.args as unknown[]).map(String) : [];
    add("Argumentos", args.length ? args.join(", ") : "nenhum");
    add("Definida em", str(attrs.source_file) ? `${str(attrs.source_file)}:${num(attrs.line) ?? "?"}` : null);
  }
  if (facts.length === 0) {
    return <Notice>Sem resumo para este tipo de nó. Veja a aba Atributos.</Notice>;
  }
  return (
    <Card>
      <dl className="divide-y">{facts}</dl>
    </Card>
  );
}

// --------------------------------------------------------------------------- relações

function Relations({ id, incoming }: { id: string; incoming: boolean }) {
  const [offset, setOffset] = useState(0);
  const query = useQuery({
    queryKey: ["relations", id, incoming, offset],
    queryFn: () =>
      request(RelationsQuery, {
        id,
        direction: incoming ? "IN" : "OUT",
        limit: RELATIONS_PAGE,
        offset,
      }),
    placeholderData: keepPreviousData,
  });
  const title = incoming ? "Quem chega neste nó" : "Para onde este nó aponta";
  const page = query.data?.node?.neighbors;
  const names = new Map((page?.nodes ?? []).map((n) => [n.id, n.name]));
  const groups = new Map<string, NonNullable<typeof page>["items"]>();
  for (const edge of page?.items ?? []) {
    const list = groups.get(edge.type) ?? [];
    list.push(edge);
    groups.set(edge.type, list);
  }
  const total = page?.total ?? 0;
  const last = Math.min(offset + RELATIONS_PAGE, total);
  return (
    <Card aria-label={title}>
      <header className="flex items-center justify-between gap-2 border-b px-5 py-3">
        <h2 className="text-sm font-semibold">
          {title} <span className="font-normal text-muted-foreground">({total.toLocaleString("pt-BR")})</span>
        </h2>
        {total > RELATIONS_PAGE ? (
          <div className="flex items-center gap-1 text-xs">
            <span className="text-muted-foreground">
              {offset + 1}–{last} de {total.toLocaleString("pt-BR")}
            </span>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Página anterior"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - RELATIONS_PAGE))}
            >
              <ChevronLeft className="h-4 w-4" aria-hidden="true" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Próxima página"
              disabled={last >= total}
              onClick={() => setOffset(offset + RELATIONS_PAGE)}
            >
              <ChevronRight className="h-4 w-4" aria-hidden="true" />
            </Button>
          </div>
        ) : null}
      </header>
      {query.isPending ? (
        <LoadingRows />
      ) : query.isError ? (
        <div className="p-4">
          <Notice tone="warning">{query.error.message}</Notice>
        </div>
      ) : total === 0 ? (
        <p className="px-5 py-4 text-sm text-muted-foreground">Nenhuma ligação.</p>
      ) : (
        <div className="divide-y">
          {[...groups.entries()].map(([type, edges]) => (
            <section key={type} aria-label={edgeVerb(type, incoming)} data-testid={`grupo-${type}`}>
              <h3 className="bg-muted/60 px-5 py-1.5 text-xs font-medium">
                {edgeVerb(type, incoming)} <span className="text-muted-foreground">({edges.length})</span>
              </h3>
              <ul>
                {edges.map((edge, index) => {
                  const other = incoming ? edge.src : edge.dst;
                  const evidence = (edge.evidence ?? {}) as Attrs;
                  return (
                    <li key={`${other}-${edge.source}-${index}`} className="flex flex-wrap items-center gap-2 px-5 py-1.5 text-sm">
                      <NodeLink id={other} name={names.get(other)} className="min-w-0 flex-1" />
                      <ConfidenceBadge confidence={edge.confidence} source={edge.source} />
                      <span className="text-xs text-muted-foreground">{sourceLabel(edge.source)}</span>
                      {edge.occurrences > 1 ? (
                        <span className="text-xs text-muted-foreground" title="Ocorrências no código">
                          ×{edge.occurrences}
                        </span>
                      ) : null}
                      <EvidenceLink path={evidence.path} line={evidence.line} className="w-full" />
                    </li>
                  );
                })}
              </ul>
            </section>
          ))}
        </div>
      )}
    </Card>
  );
}

// --------------------------------------------------------------------------- atributos

/** JSON livre (`attrs`), colapsável, sem assumir campos. */
export function JsonTree({ value, name }: { value: unknown; name?: string }) {
  if (value !== null && typeof value === "object") {
    const entries = Array.isArray(value)
      ? value.map((item, index) => [String(index), item] as const)
      : Object.entries(value as Attrs);
    const summary = Array.isArray(value) ? `[${entries.length}]` : `{${entries.length}}`;
    return (
      <details open={!name} className="font-mono text-xs">
        <summary className="cursor-pointer select-none">
          {name ? <span className="text-primary">{name}</span> : null} <span className="text-muted-foreground">{summary}</span>
        </summary>
        <div className="ml-4 border-l pl-3">
          {entries.map(([key, item]) => (
            <JsonTree key={key} name={key} value={item} />
          ))}
        </div>
      </details>
    );
  }
  return (
    <div className="font-mono text-xs">
      {name ? <span className="text-primary">{name}: </span> : null}
      <span className="break-all">{JSON.stringify(value)}</span>
    </div>
  );
}

