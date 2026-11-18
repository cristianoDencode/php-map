/**
 * Configuração inicial (`/configuracao`): cria o administrador (e-mail e senha) com o código de uso único que o
 * `php-map ui` imprime no terminal. Só aparece quando o cofre não tem usuário; o servidor
 * confere o código de novo (esta tela não decide nada).
 */
import { useQueryClient } from "@tanstack/react-query";
import { Map as MapIcon, UserPlus } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { Navigate, useNavigate } from "react-router";
import { Button, LoadingRows, Notice } from "../components/ui";
import { AUTH_KEY, postAuth, useAuth } from "../lib/auth";

const INPUT = "h-9 w-full rounded-md border border-input bg-card px-3 text-sm";
const SETUP_CODE_ERROR = "setup-code";

export function SetupPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const client = useQueryClient();
  const ids = { email: useId(), pass: useId(), again: useId(), code: useId() };
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (auth.isPending) return <LoadingRows rows={3} label="Verificando o acesso" />;
  if (!auth.data?.setupRequired) return <Navigate to="/" replace />;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (password !== again) {
      setError("as duas senhas não conferem");
      return;
    }
    setBusy(true);
    const result = await postAuth("/first-user", { email, password, setupCode: code.trim() });
    setBusy(false);
    if (!result.ok) {
      setError(
        result.error === SETUP_CODE_ERROR
          ? "código de configuração incorreto: use o que o terminal mostrou ao subir o servidor"
          : result.status === 409
            ? "o administrador já foi criado: entre com o e-mail dele"
            : result.error,
      );
      setPassword("");
      setAgain("");
      if (result.status === 409) await client.invalidateQueries({ queryKey: AUTH_KEY });
      return;
    }
    await client.invalidateQueries({ queryKey: AUTH_KEY });
    void client.invalidateQueries({ predicate: (q) => q.queryKey[0] !== "auth" });
    navigate("/", { replace: true });
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-xl border bg-card p-6" aria-label="Configuração inicial">
        <div className="flex items-center gap-2">
          <MapIcon className="h-5 w-5 text-primary" aria-hidden="true" />
          <span className="font-semibold">php-map</span>
        </div>
        <h1 className="text-xl font-semibold">Configuração inicial</h1>
        <p className="text-sm text-muted-foreground">
          Crie o administrador com o seu e-mail e uma senha. O código de configuração está no terminal onde o servidor foi iniciado e vale uma única vez.
          A senha precisa de pelo menos 10 caracteres.
        </p>
        <div className="space-y-1">
          <label htmlFor={ids.code} className="text-sm font-medium">
            Código de configuração
          </label>
          <input
            id={ids.code}
            className={INPUT}
            value={code}
            onChange={(e) => setCode(e.target.value)}
            autoComplete="off"
            autoFocus
            spellCheck={false}
            required
          />
        </div>
        <div className="space-y-1">
          <label htmlFor={ids.email} className="text-sm font-medium">
            E-mail
          </label>
          <input
            id={ids.email}
            type="email"
            className={INPUT}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            placeholder="admin@exemplo.com"
            spellCheck={false}
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
            autoComplete="new-password"
            required
          />
        </div>
        <div className="space-y-1">
          <label htmlFor={ids.again} className="text-sm font-medium">
            Repita a senha
          </label>
          <input
            id={ids.again}
            type="password"
            className={INPUT}
            value={again}
            onChange={(e) => setAgain(e.target.value)}
            autoComplete="new-password"
            required
          />
        </div>
        {error && <Notice tone="warning">{error}</Notice>}
        <Button type="submit" className="w-full" disabled={busy || !code || !email || !password}>
          <UserPlus className="h-4 w-4" aria-hidden="true" />
          {busy ? "Aguarde…" : "Criar e entrar"}
        </Button>
      </form>
    </div>
  );
}
