/**
 * Entrar na interface (`/login`). Sem usuário no cofre a tela certa é a «Configuração inicial»
 * (`/configuracao`), para onde esta rota redireciona.
 */
import { useQueryClient } from "@tanstack/react-query";
import { LogIn, Map as MapIcon } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router";
import { Button, LoadingRows, Notice } from "../components/ui";
import { AUTH_KEY, postAuth, safeReturn, useAuth } from "../lib/auth";

const INPUT = "h-9 w-full rounded-md border border-input bg-card px-3 text-sm";

export function LoginPage() {
  const auth = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const ids = { user: useId(), pass: useId() };
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (auth.isPending) return <LoadingRows rows={3} label="Verificando o acesso" />;
  const state = auth.data;
  const back = safeReturn(params.get("volta"));
  if (state?.setupRequired) return <Navigate to="/configuracao" replace />;
  if (!state?.loginRequired || state.user) return <Navigate to={back} replace />;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    const result = await postAuth("/login", { email, password });
    setBusy(false);
    if (!result.ok) {
      setError(result.error);
      setPassword("");
      return;
    }
    await client.invalidateQueries({ queryKey: AUTH_KEY });
    void client.invalidateQueries({ predicate: (q) => q.queryKey[0] !== "auth" });
    navigate(back, { replace: true });
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-xl border bg-card p-6" aria-label="Entrar">
        <div className="flex items-center gap-2">
          <MapIcon className="h-5 w-5 text-primary" aria-hidden="true" />
          <span className="font-semibold">php-map</span>
        </div>
        <h1 className="text-xl font-semibold">Entrar</h1>
        <div className="space-y-1">
          <label htmlFor={ids.user} className="text-sm font-medium">
            E-mail
          </label>
          <input
            id={ids.user}
            className={INPUT}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </div>
        <div className="space-y-1">
          <label htmlFor={ids.pass} className="text-sm font-medium">
            Senha
          </label>
          <input
            id={ids.pass}
            type="password"
            className={INPUT}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </div>
        {error && <Notice tone="warning">{error}</Notice>}
        <Button type="submit" className="w-full" disabled={busy || !email || !password}>
          <LogIn className="h-4 w-4" aria-hidden="true" />
          {busy ? "Aguarde…" : "Entrar"}
        </Button>
      </form>
    </div>
  );
}
