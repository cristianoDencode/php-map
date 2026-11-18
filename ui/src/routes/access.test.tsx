import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { mockApi, renderApp } from "../test/utils";

const STATUS = {
  mcpStatus: {
    available: true,
    reason: null,
    path: "/mcp",
    localUrl: "http://127.0.0.1:8765/mcp",
    publicUrls: [],
    exposeSource: false,
    activeTokens: 1,
    legacyToken: false,
    vaultAvailable: true,
  },
};
const TOKEN = (id: number, name: string, state = "ativo") => ({
  id,
  name,
  prefix: `ecm_abc${id}defgh`,
  createdAt: "2026-11-09T12:00:00Z",
  createdBy: "ana",
  expiresAt: "2027-02-07T12:00:00Z",
  lastUsedAt: id === 1 ? "2026-11-09T13:00:00Z" : null,
  lastUsedIp: id === 1 ? "10.0.0.5" : null,
  state,
});
const USER = (username: string, active = true, isCurrent = false, role = "admin") => ({
  username,
  role,
  active,
  createdAt: "2026-11-09T12:00:00Z",
  lastLoginAt: null,
  locked: false,
  isCurrent,
});

function base(extra: Record<string, (v: Record<string, unknown>) => unknown> = {}) {
  return {
    McpStatus: () => STATUS,
    AccessTokens: () => ({ accessTokens: [TOKEN(1, "meu-notebook")] }),
    AccessUsers: () => ({ accessUsers: [USER("ana", true, true), USER("bia")] }),
    ...extra,
  };
}

describe("acesso", () => {
  test("lista os tokens sem nunca mostrar o valor e abre o estado do MCP", async () => {
    mockApi(base());
    renderApp("/acesso");
    const tokens = await screen.findByLabelText("Tokens");
    expect(await within(tokens).findByText("meu-notebook")).toBeInTheDocument();
    expect(within(tokens).getByText("ecm_abc1defgh…")).toBeInTheDocument();
    expect(within(tokens).getByText(/10\.0\.0\.5/)).toBeInTheDocument();
    const mcp = await screen.findByLabelText("MCP");
    expect(within(mcp).getByText("http://127.0.0.1:8765/mcp")).toBeInTheDocument();
    expect(within(mcp).getByText("no ar")).toBeInTheDocument();
  });

  test("novo token: mostra o valor uma vez, com o comando do Claude Code, e some ao fechar", async () => {
    const user = userEvent.setup();
    const calls = mockApi(
      base({
        CreateAccessToken: () => ({
          createAccessToken: { token: "ecm_SEGREDO-do-token-123456", info: { id: 2, name: "outro", prefix: "ecm_SEGREDO-d", expiresAt: null } },
        }),
      }),
    );
    renderApp("/acesso");
    await user.click(await screen.findByRole("button", { name: /Novo token/ }));
    await user.type(screen.getByLabelText("Nome do token"), "outro");
    await user.selectOptions(screen.getByLabelText("Validade"), "never");
    expect(screen.getByText(/Token sem validade/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Criar token" }));
    expect(await screen.findByText("ecm_SEGREDO-do-token-123456")).toBeInTheDocument();
    expect(screen.getByText(/claude mcp add --transport http php-map http:\/\/127\.0\.0\.1:8765\/mcp/)).toBeInTheDocument();
    expect(screen.getByText(/Authorization: Bearer ecm_SEGREDO-do-token-123456/)).toBeInTheDocument();
    expect(screen.getByText(/\$\{PHP_MAP_TOKEN\}/)).toBeInTheDocument(); // .mcp.json sem o segredo
    const create = calls.find((c) => c.operation === "CreateAccessToken");
    expect(create?.variables).toEqual({ name: "outro", expiresDays: null });
    await user.click(screen.getByRole("button", { name: "Já guardei, fechar" }));
    await waitFor(() => expect(screen.queryByText("ecm_SEGREDO-do-token-123456")).not.toBeInTheDocument());
    expect(document.body.textContent).not.toContain("ecm_SEGREDO-do-token-123456");
  });

  test("copiar usa a área de transferência", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(async () => undefined);
    mockApi(base());
    renderApp("/acesso");
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await user.click(await screen.findByRole("button", { name: "Copiar o endereço local" }));
    expect(writeText).toHaveBeenCalledWith("http://127.0.0.1:8765/mcp");
  });

  test("revogar pede confirmação antes de chamar o servidor", async () => {
    const user = userEvent.setup();
    const calls = mockApi(base({ RevokeAccessToken: () => ({ revokeAccessToken: { id: 1, state: "revogado" } }) }));
    renderApp("/acesso");
    await user.click(await screen.findByRole("button", { name: "Revogar meu-notebook" }));
    expect(calls.some((c) => c.operation === "RevokeAccessToken")).toBe(false);
    await user.click(screen.getByRole("button", { name: "Cancelar" }));
    expect(calls.some((c) => c.operation === "RevokeAccessToken")).toBe(false);
    await user.click(screen.getByRole("button", { name: "Revogar meu-notebook" }));
    await user.click(screen.getByRole("button", { name: "Confirmar a revogação de meu-notebook" }));
    await waitFor(() => expect(calls.find((c) => c.operation === "RevokeAccessToken")?.variables).toEqual({ id: 1 }));
  });

  test("token revogado não oferece «Revogar»", async () => {
    mockApi(base({ AccessTokens: () => ({ accessTokens: [TOKEN(1, "velho", "revogado")] }) }));
    renderApp("/acesso");
    expect(await screen.findByText("velho")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revogar velho" })).not.toBeInTheDocument();
  });

  test("não deixa desativar o próprio usuário nem o último ativo", async () => {
    mockApi(base({ AccessUsers: () => ({ accessUsers: [USER("ana", true, true), USER("bia", false)] }) }));
    renderApp("/acesso");
    const own = await screen.findByRole("button", { name: "Desativar ana" });
    expect(own).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reativar bia" })).toBeEnabled();
  });

  test("desativa outro usuário, reativa e redefine a senha", async () => {
    const user = userEvent.setup();
    const calls = mockApi(
      base({
        DisableAccessUser: () => ({ disableAccessUser: { username: "bia", active: false } }),
        ResetAccessUserPassword: () => ({ resetAccessUserPassword: { username: "bia" } }),
      }),
    );
    renderApp("/acesso");
    await user.click(await screen.findByRole("button", { name: "Desativar bia" }));
    await waitFor(() => expect(calls.some((c) => c.operation === "DisableAccessUser")).toBe(true));
    await user.click(screen.getByRole("button", { name: "Redefinir a senha de bia" }));
    await user.type(screen.getByLabelText("Nova senha de bia"), "nova-senha-longa");
    await user.click(screen.getByRole("button", { name: "Salvar senha" }));
    await waitFor(() =>
      expect(calls.find((c) => c.operation === "ResetAccessUserPassword")?.variables).toEqual({
        username: "bia",
        password: "nova-senha-longa",
      }),
    );
  });

  test("adicionar usuário mostra o erro do servidor", async () => {
    const user = userEvent.setup();
    mockApi(
      base({
        AddAccessUser: () => {
          throw new Error("a senha precisa de pelo menos 10 caracteres");
        },
      }),
    );
    renderApp("/acesso");
    await user.type(await screen.findByLabelText("E-mail do novo usuário"), "cris@exemplo.com");
    await user.type(screen.getByLabelText("Senha (mín. 10)"), "curta");
    await user.click(screen.getByRole("button", { name: "Adicionar" }));
    expect((await screen.findAllByText(/pelo menos 10 caracteres/)).length).toBeGreaterThan(0);
  });

  test("novo usuário nasce leitor, e o papel pode ser trocado na lista", async () => {
    const user = userEvent.setup();
    const calls = mockApi(
      base({
        AccessUsers: () => ({ accessUsers: [USER("ana", true, true), USER("bia", true, false, "leitor")] }),
        AddAccessUser: () => ({ addAccessUser: { username: "cris" } }),
        SetAccessUserRole: () => ({ setAccessUserRole: { username: "bia", role: "admin" } }),
      }),
    );
    renderApp("/acesso");
    await user.type(await screen.findByLabelText("E-mail do novo usuário"), "cris@exemplo.com");
    await user.type(screen.getByLabelText("Senha (mín. 10)"), "uma-senha-longa");
    expect(screen.getByLabelText("Papel")).toHaveValue("leitor");
    await user.click(screen.getByRole("button", { name: "Adicionar" }));
    await waitFor(() =>
      expect(calls.find((c) => c.operation === "AddAccessUser")?.variables).toEqual({
        username: "cris@exemplo.com",
        password: "uma-senha-longa",
        role: "leitor",
      }),
    );
    const own = screen.getByLabelText("Papel de ana");
    expect(own).toBeDisabled(); // o único administrador não se rebaixa
    await user.selectOptions(screen.getByLabelText("Papel de bia"), "admin");
    await waitFor(() =>
      expect(calls.find((c) => c.operation === "SetAccessUserRole")?.variables).toEqual({ username: "bia", role: "admin" }),
    );
  });

  test("o interruptor do código-fonte vem desligado e liga pelo servidor", async () => {
    const user = userEvent.setup();
    const calls = mockApi(base({ SetMcpExposeSource: () => ({ setMcpExposeSource: { exposeSource: true } }) }));
    renderApp("/acesso");
    const toggle = await screen.findByRole("checkbox", { name: /Permitir ler o código-fonte pelo MCP/ });
    expect(toggle).not.toBeChecked();
    expect(screen.getByText(/libs\/db_conn\.php/)).toBeInTheDocument();
    await user.click(toggle);
    await waitFor(() => expect(calls.find((c) => c.operation === "SetMcpExposeSource")?.variables).toEqual({ expose: true }));
  });

  test("sem o extra do MCP, a página explica o que instalar", async () => {
    mockApi(
      base({ McpStatus: () => ({ mcpStatus: { ...STATUS.mcpStatus, available: false, reason: 'o MCP precisa do extra opcional: pip install -e ".[mcp]"' } }) }),
    );
    renderApp("/acesso");
    expect(await screen.findByText(/pip install -e/)).toBeInTheDocument();
    expect(screen.getByText("indisponível")).toBeInTheDocument();
  });

  test("com URL pública, o comando usa o endereço público", async () => {
    const user = userEvent.setup();
    mockApi(
      base({
        McpStatus: () => ({ mcpStatus: { ...STATUS.mcpStatus, publicUrls: ["https://x.ngrok-free.app/mcp"] } }),
        CreateAccessToken: () => ({ createAccessToken: { token: "ecm_t", info: { id: 3, name: "n", prefix: "ecm_t", expiresAt: null } } }),
      }),
    );
    renderApp("/acesso");
    await screen.findByText("https://x.ngrok-free.app/mcp");
    await user.click(screen.getByRole("button", { name: /Novo token/ }));
    await user.type(screen.getByLabelText("Nome do token"), "n");
    await user.click(screen.getByRole("button", { name: "Criar token" }));
    expect(await screen.findByText(/php-map https:\/\/x\.ngrok-free\.app\/mcp/)).toBeInTheDocument();
  });

  test("sem cofre, a página avisa", async () => {
    mockApi({ McpStatus: () => ({ mcpStatus: { ...STATUS.mcpStatus, vaultAvailable: false, available: false } }) });
    renderApp("/acesso");
    expect(await screen.findByText(/cofre de acesso não está ligado/)).toBeInTheDocument();
  });
});
