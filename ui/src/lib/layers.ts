/**
 * Camadas de um fonte: a consulta e as funções puras da tela.
 * A camada de cada nó é deduzida do nome e do tipo pelo servidor; aqui só se ordena e se rotula.
 */
import { graphql } from "../graphql/generated";
import type { LayersQuery } from "../graphql/generated/graphql";

export const DEFAULT_MIN_CONFIDENCE = 0.5;

export const LayersDocument = graphql(`
  query Layers($path: String!, $minConfidence: Float!) {
    layers(path: $path, minConfidence: $minConfidence) {
      node
      candidates
      label
      layer
      name
      minConfidence
      upTotal
      upTruncated
      up {
        menu
        menuPath
        distance
        confidence
        steps {
          node
          label
          layer
          name
          edgeType
          confidence
          path
          line
        }
      }
      downTotal
      downTruncated
      downByLayer {
        layer
        count
      }
      down {
        node
        label
        layer
        name
        depth
        parent
        edgeType
        confidence
        path
        line
        factory
        members {
          name
          node
          edgeType
          confidence
        }
      }
    }
  }
`);

export type FileLayers = LayersQuery["layers"];
export type LayerPath = FileLayers["up"][number];
export type LayerDownNode = FileLayers["down"][number];

/** Ordem de apresentação, do menu à tabela; `outro` por último. */
export const LAYER_ORDER = [
  "menu",
  "tela",
  "js",
  "rota",
  "controller",
  "service",
  "repository",
  "model",
  "dao",
  "tabela",
  "outro",
] as const;

export const LAYER_TITLES: Record<string, string> = {
  menu: "Menu",
  tela: "Tela",
  js: "JavaScript",
  rota: "Rota",
  controller: "Controller",
  service: "Service",
  repository: "Repository",
  model: "Model",
  dao: "DAO",
  tabela: "Tabela",
  outro: "Outros",
};

export const LAYER_NOTE = "A camada é deduzida do nome e do tipo do arquivo, não é uma ligação do grafo.";
export const EMPTY_UP = "Nenhum menu chega neste fonte pelo mapa.";
export const EMPTY_UP_HELP =
  "Ele pode ser chamado por JavaScript sem URL literal, por SQL dinâmico, por agendamento ou ficar sem uso. " +
  "Aumente a distância em “Menus por fonte” ou baixe a confiança mínima para ver caminhos mais frágeis.";

/** Itens que cada camada mostra de início; “Mostrar mais” revela os demais que o servidor devolveu. */
export const INITIAL_SHOWN = 15;
/** Camada que começa recolhida (a lista é longa e o que importa está nas outras). */
export const COLLAPSED_LAYER = "outro";

export function layerTitle(layer: string): string {
  return LAYER_TITLES[layer] ?? layer;
}

/** Camadas do que o fonte alcança, na ordem de apresentação, com o total (inclusive o cortado). */
export function downGroups(data: FileLayers): { layer: string; total: number; nodes: LayerDownNode[] }[] {
  const totals = new Map(data.downByLayer.map((item) => [item.layer, item.count]));
  const rank = (layer: string) => {
    const index = (LAYER_ORDER as readonly string[]).indexOf(layer);
    return index < 0 ? LAYER_ORDER.length : index;
  };
  const byLayer = new Map<string, LayerDownNode[]>();
  for (const item of data.down) {
    const list = byLayer.get(item.layer) ?? [];
    list.push(item);
    byLayer.set(item.layer, list);
  }
  return [...new Set([...totals.keys(), ...byLayer.keys()])]
    .sort((a, b) => rank(a) - rank(b))
    .map((layer) => {
      const nodes = byLayer.get(layer) ?? [];
      return { layer, total: totals.get(layer) ?? nodes.length, nodes };
    });
}

/** Caminhos que ainda não aparecem na lista (o servidor corta em 50). */
export function hiddenPaths(data: FileLayers): number {
  return Math.max(0, data.upTotal - data.up.length);
}

/**
 * Por onde o fonte obteve o DAO: `via getDao('cgmtipoempresa')`. O argumento é o nome da classe
 * sem `cl_`; se a classe não segue o padrão, só `via getDao`.
 */
export function daoSource(item: Pick<LayerDownNode, "factory" | "name">): string | null {
  if (!item.factory) return null;
  const argument = item.name.startsWith("cl_") ? item.name.slice(3) : "";
  return argument ? `via ${item.factory}('${argument}')` : `via ${item.factory}`;
}
