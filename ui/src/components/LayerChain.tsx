import { useQuery } from "@tanstack/react-query";
import { ArrowRight, ListTree } from "lucide-react";
import { Fragment, useState } from "react";
import { Link } from "react-router";
import { request } from "../lib/api";
import { edgeVerb } from "../lib/labels";
import {
  COLLAPSED_LAYER,
  DEFAULT_MIN_CONFIDENCE,
  daoSource,
  downGroups,
  EMPTY_UP,
  EMPTY_UP_HELP,
  type FileLayers,
  hiddenPaths,
  INITIAL_SHOWN,
  LAYER_NOTE,
  LayersDocument,
  type LayerDownNode,
  type LayerPath,
  layerTitle,
} from "../lib/layers";
import { keyOf } from "../lib/ids";
import { ConfidenceBadge, NodeLink } from "./NodeBits";
import { Badge, Card, LoadingRows, Notice } from "./ui";

/** Um nó do caminho: a faixa da camada, o nome com link e a aresta que chegou nele. */
function Station({
  layer,
  node,
  name,
  edgeType,
  confidence,
}: {
  layer: string;
  node: string;
  name: string;
  edgeType?: string | null;
  confidence?: number | null;
}) {
  return (
    <li className="flex min-w-0 flex-col gap-1 rounded-md border bg-card px-2.5 py-1.5 text-sm" data-layer={layer}>
      <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{layerTitle(layer)}</span>
      <NodeLink id={node} label={name} className="max-w-[18rem]" />
      {edgeType ? (
        <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
          {edgeVerb(edgeType, true)}
          {confidence != null ? <ConfidenceBadge confidence={confidence} /> : null}
        </span>
      ) : null}
    </li>
  );
}

function Arrow() {
  return (
    <li aria-hidden="true" className="flex items-center">
      <ArrowRight className="h-4 w-4 shrink-0 text-muted-foreground" />
    </li>
  );
}

function UpPath({ path, current }: { path: LayerPath; current: { node: string; name: string; layer: string } }) {
  const label = path.menuPath ?? keyOf(path.menu);
  return (
    <li className="space-y-2 px-5 py-3" aria-label={`Caminho até ${label}`}>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">{label}</span>
        <span className="text-xs text-muted-foreground">
          {path.distance === 1 ? "1 salto" : `${path.distance} saltos`}
        </span>
        <ConfidenceBadge confidence={path.confidence} />
      </div>
      <ol className="flex flex-wrap items-stretch gap-1.5" aria-label="Camadas, do menu ao fonte">
        {path.steps.map((step, index) => (
          <Fragment key={`${step.node}-${index}`}>
            {index > 0 ? <Arrow /> : null}
            <Station
              layer={step.layer}
              node={step.node}
              name={step.name}
              edgeType={index > 0 ? step.edgeType : null}
              confidence={index > 0 ? step.confidence : null}
            />
          </Fragment>
        ))}
        <Arrow />
        <li
          className="flex flex-col gap-1 rounded-md border-2 border-primary bg-card px-2.5 py-1.5 text-sm"
          aria-current="true"
          data-layer={current.layer}
        >
          <span className="text-[11px] font-medium uppercase tracking-wide text-primary">Este fonte · {layerTitle(current.layer)}</span>
          <span className="max-w-[18rem] truncate font-medium" title={current.node}>
            {current.name}
          </span>
        </li>
      </ol>
    </li>
  );
}

/** Um item do que o fonte alcança. No DAO: a classe, por onde chegou e os métodos que ele chama. */
function DownItem({ item }: { item: LayerDownNode }) {
  const source = item.layer === "dao" ? daoSource(item) : null;
  return (
    <li className="flex flex-wrap items-center gap-2 px-5 py-1.5 text-sm">
      <NodeLink id={item.node} label={item.name} className="min-w-0 flex-1" />
      {source ? <span className="text-xs text-muted-foreground">{source}</span> : null}
      {item.members.length > 0 ? (
        <span className="flex flex-wrap gap-x-2 text-xs text-muted-foreground" aria-label="Métodos chamados">
          {item.members.map((member) => (
            <code key={member.node} title={`${edgeVerb(member.edgeType, false)} (${member.confidence.toFixed(2)})`}>
              {member.name}()
            </code>
          ))}
        </span>
      ) : null}
      <span className="text-xs text-muted-foreground">{edgeVerb(item.edgeType, false)}</span>
      <ConfidenceBadge confidence={item.confidence} />
    </li>
  );
}

/** Lista de uma camada: os primeiros itens e “Mostrar mais” para o resto do que o servidor devolveu. */
function DownList({ layer, nodes }: { layer: string; nodes: LayerDownNode[] }) {
  const [all, setAll] = useState(false);
  const visible = all ? nodes : nodes.slice(0, INITIAL_SHOWN);
  const rest = nodes.length - visible.length;
  return (
    <>
      <ul aria-label={`Itens: ${layerTitle(layer)}`}>
        {visible.map((item) => (
          <DownItem key={item.node} item={item} />
        ))}
      </ul>
      {rest > 0 ? (
        <div className="px-5 py-1.5">
          <button
            type="button"
            className="text-xs text-primary hover:underline"
            onClick={() => setAll(true)}
          >
            Mostrar mais ({rest.toLocaleString("pt-BR")})
          </button>
        </div>
      ) : null}
    </>
  );
}

function DownGroupHeading({ layer, total, shown }: { layer: string; total: number; shown: number }) {
  return (
    <>
      {layerTitle(layer)} <Badge>{total.toLocaleString("pt-BR")}</Badge>
      {total > shown ? (
        <span className="font-normal text-muted-foreground">mostrando {shown.toLocaleString("pt-BR")}</span>
      ) : null}
    </>
  );
}

/** As camadas de um fonte: o que chega nele até o menu e o que ele alcança até a tabela. */
export function LayerChain({ data, menusHref }: { data: FileLayers; menusHref?: string }) {
  const hidden = hiddenPaths(data);
  const groups = downGroups(data);
  const current = { node: data.node ?? "", name: data.name ?? "", layer: data.layer ?? "outro" };
  return (
    <div className="space-y-4">
      <Notice>{LAYER_NOTE}</Notice>
      <Card aria-label="Para cima: até o menu">
        <header className="flex flex-wrap items-center justify-between gap-2 border-b px-5 py-3">
          <h2 className="text-sm font-semibold">
            Quem chega aqui, até o menu{" "}
            <span className="font-normal text-muted-foreground">({data.upTotal.toLocaleString("pt-BR")})</span>
          </h2>
          {menusHref ? (
            <Link
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-input px-3 text-sm hover:bg-muted"
              to={menusHref}
            >
              <ListTree className="h-4 w-4" aria-hidden="true" /> Ver menu desta fonte
            </Link>
          ) : null}
        </header>
        {data.up.length === 0 ? (
          <div className="space-y-1 px-5 py-4 text-sm" data-testid="camadas-vazio">
            <p>{EMPTY_UP}</p>
            <p className="text-muted-foreground">{EMPTY_UP_HELP}</p>
          </div>
        ) : (
          <ul className="divide-y" aria-label="Caminhos até o menu">
            {data.up.map((path) => (
              <UpPath key={`${path.menu}-${path.steps.map((s) => s.node).join(">")}`} path={path} current={current} />
            ))}
          </ul>
        )}
        {hidden > 0 ? <p className="border-t px-5 py-2 text-xs text-muted-foreground">… e mais {hidden.toLocaleString("pt-BR")} caminhos.</p> : null}
        {data.upTruncated ? (
          <p className="border-t px-5 py-2 text-xs text-muted-foreground">A busca bateu no limite de nós: pode haver mais caminhos.</p>
        ) : null}
      </Card>

      <Card aria-label="Para baixo: até a tabela">
        <header className="border-b px-5 py-3">
          <h2 className="text-sm font-semibold">
            O que este fonte alcança, até a tabela{" "}
            <span className="font-normal text-muted-foreground">({data.downTotal.toLocaleString("pt-BR")})</span>
          </h2>
        </header>
        {groups.length === 0 ? (
          <p className="px-5 py-4 text-sm text-muted-foreground">Este fonte não chama nada que o mapa conheça.</p>
        ) : (
          <div className="divide-y">
            {groups.map((group) => {
              const heading = (
                <DownGroupHeading layer={group.layer} total={group.total} shown={group.nodes.length} />
              );
              // `Outros` começa recolhido: é o que o mapa não soube classificar.
              if (group.layer === COLLAPSED_LAYER) {
                return (
                  <details key={group.layer} data-testid={`camada-${group.layer}`}>
                    <summary className="flex cursor-pointer items-center gap-2 bg-muted/60 px-5 py-1.5 text-xs font-medium">
                      {heading}
                    </summary>
                    <DownList layer={group.layer} nodes={group.nodes} />
                  </details>
                );
              }
              return (
                <section key={group.layer} aria-label={layerTitle(group.layer)} data-testid={`camada-${group.layer}`}>
                  <h3 className="flex items-center gap-2 bg-muted/60 px-5 py-1.5 text-xs font-medium">{heading}</h3>
                  <DownList layer={group.layer} nodes={group.nodes} />
                </section>
              );
            })}
          </div>
        )}
        {data.downTruncated ? (
          <p className="border-t px-5 py-2 text-xs text-muted-foreground">A busca bateu no limite de nós: os totais podem estar abaixo do real.</p>
        ) : null}
      </Card>
    </div>
  );
}

/** Seção carregada sob demanda (só consulta quando a aba abre). */
export function LayersSection({ id, label }: { id: string; label: string }) {
  const path = label === "File" ? keyOf(id) : id;
  const query = useQuery({
    queryKey: ["layers", id, DEFAULT_MIN_CONFIDENCE],
    queryFn: () => request(LayersDocument, { path, minConfidence: DEFAULT_MIN_CONFIDENCE }),
  });
  if (query.isPending) return <LoadingRows rows={6} label="Carregando as camadas" />;
  if (query.isError) return <Notice tone="warning">{query.error.message}</Notice>;
  const data = query.data.layers;
  if (!data.node) {
    return <Notice tone="warning">Fonte não encontrado no grafo: {path}.</Notice>;
  }
  return <LayerChain data={data} menusHref={label === "File" ? `/menus?fonte=${encodeURIComponent(keyOf(id))}` : undefined} />;
}
