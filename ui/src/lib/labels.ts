/**
 * Dicionário PT-BR dos tipos do grafo (fonte única dos nomes dos tipos).
 */
import {
  Boxes,
  Braces,
  Cable,
  Database,
  FileCode2,
  Layers,
  type LucideIcon,
  MenuSquare,
  Route,
  ServerCog,
  SquareFunction,
  Variable,
  Workflow,
} from "lucide-react";

export type NodeKind = {
  singular: string;
  plural: string;
  icon: LucideIcon;
  /** Token CSS da cor (claro e escuro em `index.css`), reusado pelo Cytoscape. */
  color: string;
};

export const NODE_KINDS: Record<string, NodeKind> = {
  File: { singular: "Arquivo", plural: "Arquivos", icon: FileCode2, color: "var(--node-file)" },
  Table: { singular: "Tabela", plural: "Tabelas", icon: Database, color: "var(--node-table)" },
  Function: {
    singular: "Função PHP",
    plural: "Funções PHP",
    icon: Variable,
    color: "var(--node-function)",
  },
  Method: { singular: "Método", plural: "Métodos", icon: Braces, color: "var(--node-method)" },
  Class: { singular: "Classe", plural: "Classes", icon: Boxes, color: "var(--node-class)" },
  PgFunction: {
    singular: "Função do banco",
    plural: "Funções do banco",
    icon: SquareFunction,
    color: "var(--node-pgfunction)",
  },
  RpcCall: {
    singular: "Chamada ao banco",
    plural: "Chamadas ao banco",
    icon: ServerCog,
    color: "var(--node-pgfunction)",
  },
  Module: { singular: "Módulo", plural: "Módulos", icon: Layers, color: "var(--node-module)" },
  BusinessAction: {
    singular: "Item de menu",
    plural: "Itens de menu",
    icon: MenuSquare,
    color: "var(--node-action)",
  },
  Route: { singular: "Rota", plural: "Rotas", icon: Route, color: "var(--node-other)" },
  Endpoint: { singular: "Endpoint", plural: "Endpoints", icon: Cable, color: "var(--node-other)" },
};

const OTHER: NodeKind = { singular: "Nó", plural: "Nós", icon: Workflow, color: "var(--node-other)" };

export function nodeKind(label: string): NodeKind {
  return NODE_KINDS[label] ?? OTHER;
}

/** Verbo da aresta nos dois sentidos: "a tela **lê** a tabela" / "a tabela **é lida por** a tela". */
export const EDGE_VERBS: Record<string, { out: string; in: string }> = {
  QUERIES: { out: "lê", in: "é lida por" },
  INSERTS: { out: "insere em", in: "recebe inserção de" },
  UPDATES: { out: "altera", in: "é alterada por" },
  DELETES: { out: "exclui de", in: "tem exclusão feita por" },
  CALLS: { out: "chama", in: "é chamado por" },
  CALLS_RPC: { out: "chama a função do banco", in: "é chamada por" },
  CALLS_DYNAMIC: { out: "faz chamada dinâmica", in: "recebe chamada dinâmica de" },
  INCLUDES: { out: "inclui", in: "é incluído por" },
  REQUIRES: { out: "requer", in: "é requerido por" },
  INSTANTIATES: { out: "instancia", in: "é instanciada por" },
  REFERENCES: { out: "referencia", in: "é referenciado por" },
  DECLARES: { out: "declara", in: "é declarado em" },
  EXTENDS: { out: "estende", in: "é estendida por" },
  IMPLEMENTS: { out: "implementa", in: "é implementada por" },
  TRIGGERS: { out: "dispara", in: "é disparada por" },
  BELONGS_TO: { out: "pertence ao módulo", in: "contém" },
  IMPLEMENTS_ACTION: { out: "é aberto pelo item de menu", in: "abre" },
  ROUTES_TO: { out: "encaminha para", in: "recebe rota de" },
  CALLS_HTTP: { out: "chama a rota", in: "é chamada por" },
  SERVES: { out: "serve", in: "é servido por" },
  CALLS_REMOTE: { out: "chama o endpoint", in: "é chamado por" },
  MAPS_TO: { out: "mapeia a tabela", in: "é mapeada por" },
  RENDERS: { out: "renderiza", in: "é renderizada por" },
};

export function edgeVerb(type: string, incoming: boolean): string {
  const verbs = EDGE_VERBS[type];
  if (!verbs) return type;
  return incoming ? verbs.in : verbs.out;
}

/** De onde veio a aresta, em linguagem simples. */
export const SOURCE_LABELS: Record<string, string> = {
  PHP_AST: "código PHP",
  PHP_AST_AMBIGUOUS: "código PHP (ambíguo)",
  STRING_REF: "texto no código",
  SQL_AST: "SQL analisado",
  SQL_REGEX: "SQL por padrão de texto",
  LARAVEL_RESOLVER: "rotas do Laravel",
  ZEND1_RESOLVER: "estrutura do Zend 1",
  DOCTRINE_ANNOTATION: "anotações do Doctrine",
  WS_XML: "XML do webservice",
  ECIDADE_CATALOG: "catálogo do e-cidade",
  NAMING_RULE: "regra de nome",
  HEURISTIC_AI: "heurística de IA",
  AI_CLASSIFICATION: "classificação por IA",
};

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

export type ConfidenceLevel = "certa" | "alta" | "média" | "baixa";

/** Faixa de confiança para o badge: certa (1,0), alta (≥ 0,8), média (≥ 0,5), baixa. */
export function confidenceLevel(confidence: number): ConfidenceLevel {
  if (confidence >= 1) return "certa";
  if (confidence >= 0.8) return "alta";
  if (confidence >= 0.5) return "média";
  return "baixa";
}

export const LABEL_FILTERS = ["File", "Table", "BusinessAction"] as const;
