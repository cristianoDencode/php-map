import { Link } from "react-router";
import { labelOf, nodeHref, shortName } from "../lib/ids";
import { confidenceLevel, nodeKind, sourceLabel } from "../lib/labels";
import { cn } from "../lib/utils";
import { Badge } from "./ui";

/** Ícone + nome do tipo, na cor do tipo. */
export function LabelBadge({ label, className }: { label: string; className?: string }) {
  const kind = nodeKind(label);
  const Icon = kind.icon;
  return (
    <Badge className={cn("bg-card", className)} style={{ color: kind.color, borderColor: kind.color }}>
      <Icon className="h-3 w-3" aria-hidden="true" />
      {kind.singular}
    </Badge>
  );
}

/** Link para o detalhe do nó, com o ícone do tipo. */
export function NodeLink({
  id,
  name,
  label,
  className,
}: {
  id: string;
  name?: string | null;
  /** Texto exato a mostrar (sobrepõe o nome curto do tipo). */
  label?: string;
  className?: string;
}) {
  if (id.startsWith("Termo:")) {
    // a pergunta solta não é nó do grafo: não há para onde levar
    return (
      <span className={cn("inline-flex min-w-0 items-center gap-1.5", className)} title={id}>
        <span className="truncate">termo: {label ?? name ?? id.slice("Termo:".length)}</span>
      </span>
    );
  }
  const kind = nodeKind(labelOf(id));
  const Icon = kind.icon;
  return (
    <Link
      to={nodeHref(id)}
      className={cn("inline-flex min-w-0 items-center gap-1.5 hover:underline", className)}
      title={id}
    >
      <Icon className="h-4 w-4 shrink-0" style={{ color: kind.color }} aria-hidden="true" />
      <span className="truncate">{label ?? shortName(id, name)}</span>
    </Link>
  );
}

const CONFIDENCE_COLORS: Record<string, string> = {
  certa: "var(--confidence-certa)",
  alta: "var(--confidence-alta)",
  média: "var(--confidence-media)",
  baixa: "var(--confidence-baixa)",
};

/** Confiança com a faixa em palavras e a fonte no título (tooltip nativo, acessível). */
export function ConfidenceBadge({ confidence, source }: { confidence: number; source?: string }) {
  const level = confidenceLevel(confidence);
  const color = CONFIDENCE_COLORS[level];
  const text = confidence.toLocaleString("pt-BR", { maximumFractionDigits: 2 });
  const title = source ? `Confiança ${text} (${level}), fonte: ${sourceLabel(source)}` : undefined;
  return (
    <Badge style={{ color, borderColor: color }} title={title} aria-label={title}>
      {text}
    </Badge>
  );
}
