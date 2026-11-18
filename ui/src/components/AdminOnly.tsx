/** Tela só do administrador: o leitor vê um aviso em vez da página (o servidor também recusa). */
import type { ReactNode } from "react";
import { Link } from "react-router";
import { useIsAdmin } from "../lib/auth";

export const ADMIN_ONLY_MESSAGE = "Ação restrita ao administrador";

export function AdminOnly({ children }: { children: ReactNode }) {
  if (useIsAdmin()) return <>{children}</>;
  return (
    <div className="mx-auto max-w-xl space-y-3 rounded-xl border bg-card px-6 py-8 text-center" role="alert">
      <h1 className="text-xl font-semibold">{ADMIN_ONLY_MESSAGE}</h1>
      <p className="text-sm text-muted-foreground">
        Seu usuário só consulta o mapa. Peça a um administrador para mudar esta configuração.
      </p>
      <Link to="/" className="text-sm text-primary hover:underline">
        Voltar ao início
      </Link>
    </div>
  );
}
