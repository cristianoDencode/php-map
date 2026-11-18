/**
 * "MCP e túnel" (`/publicar`): o `/mcp` da própria interface, consultável por assistentes de IA,
 * e o túnel opcional que o expõe por HTTPS.
 */
import { ThisComputerPanel } from "./PublishLocal";

export function PublishPage() {
  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <header>
        <h1 className="text-2xl font-semibold">MCP e túnel</h1>
        <p className="text-sm text-muted-foreground">
          Deixa o mapa consultável por assistentes de IA que falam MCP: a partir deste computador ou, com um túnel,
          de qualquer lugar.
        </p>
      </header>
      <ThisComputerPanel />
    </div>
  );
}
