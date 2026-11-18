import type { ReactNode } from "react";

/** O `snippet` da busca marca os termos com `[`…`]`: vira `<mark>`. */
export function renderSnippet(snippet: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const pattern = /\[([^\]]*)\]/g;
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(snippet)) !== null) {
    if (match.index > last) parts.push(snippet.slice(last, match.index));
    parts.push(
      <mark key={match.index} className="rounded bg-mark px-0.5 text-inherit">
        {match[1]}
      </mark>,
    );
    last = match.index + match[0].length;
  }
  if (last < snippet.length) parts.push(snippet.slice(last));
  return parts;
}
