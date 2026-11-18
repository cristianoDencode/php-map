import { Link, useSearchParams } from "react-router";
import { Card } from "../components/ui";

/** Seções que chegam nas próximas tarefas (21–23). */
export function ComingSoon({ title, task, what }: { title: string; task: string; what: string }) {
  const [params] = useSearchParams();
  const target = params.get("entrada") ?? params.get("tabela");
  return (
    <Card className="mx-auto max-w-2xl space-y-3 p-8 text-center">
      <h1 className="text-xl font-semibold">{title}</h1>
      <p className="text-sm text-muted-foreground">{what}</p>
      {target ? (
        <p className="text-sm">
          Alvo pedido: <code className="font-mono">{target}</code>
        </p>
      ) : null}
      <p className="text-xs text-muted-foreground">
        Em breve (tarefa {task}). Enquanto isso, use a CLI (<code className="font-mono">php-map trace</code> e{" "}
        <code className="font-mono">php-map impact</code>) ou a <Link to="/busca" className="text-primary underline">busca</Link>.
      </p>
    </Card>
  );
}

export function NotFound() {
  return (
    <Card className="mx-auto max-w-xl p-8 text-center">
      <h1 className="text-xl font-semibold">Página não encontrada</h1>
      <p className="mt-2 text-sm">
        <Link to="/" className="text-primary underline">
          Voltar ao início
        </Link>
      </p>
    </Card>
  );
}
