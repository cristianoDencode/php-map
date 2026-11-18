/** Usuário logado, «Trocar senha» e «Sair». Só aparece com login exigido. */
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, LogOut, User, X } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { useNavigate } from "react-router";
import { AUTH_KEY, postAuth, useAuth } from "../lib/auth";
import { Button, Notice } from "./ui";

const INPUT = "h-9 w-full rounded-md border border-input bg-card px-3 text-sm";

function ChangePassword({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const ids = { current: useId(), next: useId(), again: useId() };
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  function close(value: boolean) {
    onOpenChange(value);
    if (!value) {
      setCurrent("");
      setNext("");
      setAgain("");
      setError(null);
      setDone(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (next !== again) {
      setError("as duas senhas novas não conferem");
      return;
    }
    setBusy(true);
    const result = await postAuth("/password", { current, new: next });
    setBusy(false);
    if (result.ok) setDone(true);
    else setError(result.error);
  }

  return (
    <DialogPrimitive.Root open={open} onOpenChange={close}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <DialogPrimitive.Content className="fixed left-1/2 top-1/2 z-50 w-full max-w-sm -translate-x-1/2 -translate-y-1/2 space-y-4 rounded-xl border bg-card p-6 shadow-xl focus:outline-none">
          <div className="flex items-start justify-between gap-4">
            <DialogPrimitive.Title className="text-lg font-semibold">Trocar senha</DialogPrimitive.Title>
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="icon" aria-label="Fechar">
                <X className="h-4 w-4" aria-hidden="true" />
              </Button>
            </DialogPrimitive.Close>
          </div>
          <DialogPrimitive.Description className="text-sm text-muted-foreground">
            A nova senha precisa de pelo menos 10 caracteres.
          </DialogPrimitive.Description>
          {done ? (
            <div className="space-y-3">
              <Notice>Senha trocada.</Notice>
              <Button className="w-full" onClick={() => close(false)}>
                Fechar
              </Button>
            </div>
          ) : (
            <form onSubmit={submit} className="space-y-3">
              {(
                [
                  [ids.current, "Senha atual", current, setCurrent, "current-password"],
                  [ids.next, "Nova senha", next, setNext, "new-password"],
                  [ids.again, "Repita a nova senha", again, setAgain, "new-password"],
                ] as const
              ).map(([id, label, value, set, autoComplete]) => (
                <div key={id} className="space-y-1">
                  <label htmlFor={id} className="text-sm font-medium">
                    {label}
                  </label>
                  <input
                    id={id}
                    type="password"
                    className={INPUT}
                    value={value}
                    onChange={(e) => set(e.target.value)}
                    autoComplete={autoComplete}
                    required
                  />
                </div>
              ))}
              {error && <Notice tone="warning">{error}</Notice>}
              <Button type="submit" className="w-full" disabled={busy || !current || !next || !again}>
                {busy ? "Aguarde…" : "Trocar senha"}
              </Button>
            </form>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

export function AccountMenu() {
  const auth = useAuth();
  const client = useQueryClient();
  const navigate = useNavigate();
  const [changing, setChanging] = useState(false);
  const user = auth.data?.user;
  if (!auth.data?.loginRequired || !user) return null;

  async function logout() {
    await postAuth("/logout");
    client.clear();
    await client.invalidateQueries({ queryKey: AUTH_KEY });
    navigate("/login", { replace: true });
  }

  return (
    <div className="flex items-center gap-1">
      <span className="hidden items-center gap-1 text-xs text-muted-foreground lg:flex" title="Usuário logado">
        <User className="h-3.5 w-3.5" aria-hidden="true" />
        {user.username}
        {user.role === "leitor" ? <span className="rounded border px-1 text-[10px] uppercase">leitor</span> : null}
      </span>
      <Button variant="ghost" size="icon" title="Trocar senha" aria-label="Trocar senha" onClick={() => setChanging(true)}>
        <KeyRound className="h-4 w-4" aria-hidden="true" />
      </Button>
      <Button variant="ghost" size="icon" title="Sair" aria-label="Sair" onClick={() => void logout()}>
        <LogOut className="h-4 w-4" aria-hidden="true" />
      </Button>
      <ChangePassword open={changing} onOpenChange={setChanging} />
    </div>
  );
}
