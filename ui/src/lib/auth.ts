/**
 * Login da interface.
 *
 * `/auth/*` é JSON simples (não GraphQL). A sessão vai no cookie `HttpOnly`, então a página
 * nunca vê o token. Sem usuário no cofre o servidor está em modo configuração
 * (`setupRequired`): a única tela é «Configuração inicial», com o código impresso no terminal.
 * Se `/auth/me` falhar (servidor sem cofre, `--no-auth`), a interface age como antes: sem login.
 * Quem de fato barra é o servidor (401 `{"error":"login"}` ou `{"error":"setup"}` em `/graphql`).
 */
import { useQuery } from "@tanstack/react-query";

export type AuthState = {
  loginRequired: boolean;
  /** Nenhum usuário ativo: só a configuração inicial funciona. */
  setupRequired: boolean;
  user: { username: string; role?: string } | null;
  /** `false` quando `/auth/me` não respondeu (servidor sem cofre): sem aviso e sem login. */
  available: boolean;
};

export const NO_LOGIN: AuthState = { loginRequired: false, setupRequired: false, user: null, available: false };
export const AUTH_KEY = ["auth", "me"] as const;

/** O `request()` avisa por aqui que o servidor respondeu 401 de login; o guarda recarrega `/auth/me`. */
export const authEvents = new EventTarget();
export const LOGIN_REQUIRED_EVENT = "login-required";
/** O servidor recusou uma ação por papel (`FORBIDDEN`): o papel pode ter mudado, recarrega `/auth/me`. */
export const FORBIDDEN_EVENT = "forbidden";

export async function fetchMe(): Promise<AuthState> {
  try {
    const response = await fetch(new URL("/auth/me", window.location.origin).toString(), {
      credentials: "same-origin",
    });
    if (!response.ok) return NO_LOGIN;
    const data = (await response.json()) as Partial<AuthState>;
    if (typeof data.loginRequired !== "boolean") return NO_LOGIN;
    return {
      loginRequired: data.loginRequired,
      setupRequired: data.setupRequired === true,
      user: data.user ?? null,
      available: true,
    };
  } catch {
    return NO_LOGIN;
  }
}

/**
 * O usuário logado pode mudar o que o servidor guarda? Só esconde botões e telas: o servidor é quem
 * recusa a mutação de um `leitor`. Sem login (ou servidor antigo, sem papel) vale como administrador.
 */
export function isAdmin(auth: AuthState | undefined): boolean {
  return auth?.user?.role !== "leitor";
}

export function useIsAdmin(): boolean {
  return isAdmin(useAuth().data);
}

export function useAuth() {
  return useQuery({ queryKey: AUTH_KEY, queryFn: fetchMe, staleTime: 30_000, meta: { silent: true } });
}

export type AuthResult = { ok: true } | { ok: false; status: number; error: string };

export async function postAuth(path: string, body: Record<string, unknown> = {}): Promise<AuthResult> {
  try {
    const response = await fetch(new URL(`/auth${path}`, window.location.origin).toString(), {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (response.ok) return { ok: true };
    let error = `o servidor respondeu ${response.status}`;
    try {
      const data = (await response.json()) as { error?: string };
      if (data.error) error = data.error;
    } catch {
      /* corpo sem JSON: fica a mensagem genérica */
    }
    return { ok: false, status: response.status, error };
  } catch {
    return { ok: false, status: 0, error: "sem conexão com o servidor (o php-map ui está no ar?)" };
  }
}

/** Para onde voltar depois do login: só caminhos da própria interface (nada de `//host`). */
export function safeReturn(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/login") || value.startsWith("/configuracao")) return "/";
  return value;
}
