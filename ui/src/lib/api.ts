import type { TypedDocumentNode } from "@graphql-typed-document-node/core";
import { ClientError, GraphQLClient } from "graphql-request";
import { authEvents, FORBIDDEN_EVENT, LOGIN_REQUIRED_EVENT } from "./auth";

/** Única porta para o servidor (`php-map ui`). */
function client(): GraphQLClient {
  return new GraphQLClient(new URL("/graphql", window.location.origin).toString());
}

export async function request<TResult, TVariables>(
  document: TypedDocumentNode<TResult, TVariables>,
  variables?: TVariables,
): Promise<TResult> {
  try {
    const gql = client();
    // O documento tipado do codegen é aceito pelo graphql-request; o cast só une os tipos.
    const run = gql.request.bind(gql) as (doc: unknown, vars?: unknown) => Promise<TResult>;
    return await run(document, variables ?? undefined);
  } catch (error) {
    // Sessão vencida ou sem login: o guarda do Layout recarrega `/auth/me` e redireciona.
    if (error instanceof ClientError && error.response.status === 401) {
      authEvents.dispatchEvent(new Event(LOGIN_REQUIRED_EVENT));
      throw new Error("faça login para continuar", { cause: error });
    }
    if (isForbidden(error)) authEvents.dispatchEvent(new Event(FORBIDDEN_EVENT));
    throw new Error(apiMessage(error), { cause: error });
  }
}

/** A API recusou por papel (`extensions.code = FORBIDDEN`): a mensagem já diz "ação restrita…". */
export function isForbidden(error: unknown): boolean {
  return error instanceof ClientError && error.response.errors?.some((e) => e.extensions?.code === "FORBIDDEN") === true;
}

/** Mensagem em português para o toast (a API já responde em português). */
export function apiMessage(error: unknown): string {
  if (error instanceof ClientError) {
    const first = error.response.errors?.[0]?.message;
    if (first) return first;
    return `o servidor respondeu ${error.response.status}`;
  }
  if (error instanceof TypeError) {
    return "sem conexão com o servidor (o php-map ui está no ar?)";
  }
  return error instanceof Error ? error.message : String(error);
}
