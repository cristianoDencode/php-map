/**
 * Acesso: tokens do MCP, usuários e o estado do `/mcp`.
 *
 * O valor de um token só existe na resposta de `createAccessToken`; a página o mostra uma vez e o
 * descarta. Nunca há senha nem hash em nenhuma resposta.
 */
import { useQuery } from "@tanstack/react-query";
import { graphql } from "../graphql/generated";
import { request } from "./api";

export const McpStatusDocument = graphql(`
  query McpStatus {
    mcpStatus {
      available
      reason
      path
      localUrl
      publicUrls
      exposeSource
      activeTokens
      legacyToken
      vaultAvailable
    }
  }
`);

export const AccessTokensDocument = graphql(`
  query AccessTokens {
    accessTokens {
      id
      name
      prefix
      createdAt
      createdBy
      expiresAt
      lastUsedAt
      lastUsedIp
      state
    }
  }
`);

export const AccessUsersDocument = graphql(`
  query AccessUsers {
    accessUsers {
      username
      role
      active
      createdAt
      lastLoginAt
      locked
      isCurrent
    }
  }
`);

export const CreateAccessTokenDocument = graphql(`
  mutation CreateAccessToken($name: String!, $expiresDays: Int) {
    createAccessToken(name: $name, expiresDays: $expiresDays) {
      token
      info {
        id
        name
        prefix
        expiresAt
      }
    }
  }
`);

export const RevokeAccessTokenDocument = graphql(`
  mutation RevokeAccessToken($id: Int!) {
    revokeAccessToken(id: $id) {
      id
      state
    }
  }
`);

export const AddAccessUserDocument = graphql(`
  mutation AddAccessUser($username: String!, $password: String!, $role: String!) {
    addAccessUser(username: $username, password: $password, role: $role) {
      username
    }
  }
`);

export const SetAccessUserRoleDocument = graphql(`
  mutation SetAccessUserRole($username: String!, $role: String!) {
    setAccessUserRole(username: $username, role: $role) {
      username
      role
    }
  }
`);

export const ROLE_CHOICES: { value: string; label: string }[] = [
  { value: "leitor", label: "Leitor (só consulta)" },
  { value: "admin", label: "Administrador" },
];

export const ROLE_LABEL: Record<string, string> = { admin: "administrador", leitor: "leitor" };

export const DisableAccessUserDocument = graphql(`
  mutation DisableAccessUser($username: String!) {
    disableAccessUser(username: $username) {
      username
      active
    }
  }
`);

export const EnableAccessUserDocument = graphql(`
  mutation EnableAccessUser($username: String!) {
    enableAccessUser(username: $username) {
      username
      active
    }
  }
`);

export const ResetAccessUserPasswordDocument = graphql(`
  mutation ResetAccessUserPassword($username: String!, $password: String!) {
    resetAccessUserPassword(username: $username, password: $password) {
      username
    }
  }
`);

export const SetMcpExposeSourceDocument = graphql(`
  mutation SetMcpExposeSource($expose: Boolean!) {
    setMcpExposeSource(expose: $expose) {
      exposeSource
    }
  }
`);

export const EXPIRES_CHOICES: { value: string; label: string }[] = [
  { value: "30", label: "30 dias" },
  { value: "90", label: "90 dias (padrão)" },
  { value: "180", label: "180 dias" },
  { value: "365", label: "365 dias" },
  { value: "never", label: "Sem validade" },
];

export function useMcpStatus() {
  return useQuery({
    queryKey: ["mcpStatus"],
    queryFn: async () => (await request(McpStatusDocument)).mcpStatus,
    meta: { silent: true },
  });
}

/** O comando do Claude Code para este token e este endereço. */
export function claudeCommand(url: string, token: string): string {
  return `claude mcp add --transport http php-map ${url} --header "Authorization: Bearer ${token}"`;
}

/** O trecho do `.mcp.json` (o token vem de uma variável de ambiente, não do arquivo). */
export function mcpJson(url: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        "php-map": {
          type: "http",
          url,
          headers: { Authorization: "Bearer ${PHP_MAP_TOKEN}" },
        },
      },
    },
    null,
    2,
  );
}

export const STATE_LABEL: Record<string, string> = { ativo: "ativo", vencido: "vencido", revogado: "revogado" };
