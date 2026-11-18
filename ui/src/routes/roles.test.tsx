import { screen, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { StatsQuery } from "../app/Layout";
import { request } from "../lib/api";
import { authEvents, FORBIDDEN_EVENT } from "../lib/auth";
import { mockApi, renderApp } from "../test/utils";

const ME = (role: string) => ({
  loginRequired: true,
  setupRequired: false,
  user: { username: "bia", role },
});
const as = (role: string) => ({ "/auth/me": () => ({ body: ME(role) }) });
const SECOND_PROJECT = {
  slug: "outro",
  name: "Outro",
  profile: "zend1",
  source: "D:/outro",
  sourceExists: true,
  hasGraph: true,
  lastScan: null,
  schemaFromDb: false,
  dsnEnv: null,
  dsnAvailable: false,
  active: false,
  scanning: false,
};

describe("papéis", () => {
  test("o leitor não vê as telas de administração no menu e consulta o resto", async () => {
    mockApi({}, as("leitor"));
    renderApp("/");
    const nav = await screen.findByRole("navigation", { name: "Seções" });
    expect(within(nav).getByRole("link", { name: "Busca" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Projetos" })).toBeInTheDocument();
    for (const hidden of ["Atualizar mapeamento", "MCP e túnel", "Acesso"]) {
      expect(within(nav).queryByRole("link", { name: hidden })).not.toBeInTheDocument();
    }
  });

  test("o administrador vê o menu inteiro", async () => {
    mockApi({}, as("admin"));
    renderApp("/");
    const nav = await screen.findByRole("navigation", { name: "Seções" });
    for (const shown of ["Atualizar mapeamento", "MCP e túnel", "Acesso"]) {
      expect(within(nav).getByRole("link", { name: shown })).toBeInTheDocument();
    }
  });

  test.each(["/acesso", "/scan", "/publicar"])("%s avisa o leitor em vez de abrir a página", async (path) => {
    const calls = mockApi({}, as("leitor"));
    renderApp(path);
    expect(await screen.findByRole("heading", { name: "Ação restrita ao administrador" })).toBeInTheDocument();
    expect(calls.some((c) => ["McpStatus", "AccessUsers", "AccessTokens"].includes(c.operation))).toBe(false);
  });

  test("Projetos para o leitor só lista: sem cadastro, edição, remoção nem troca do ativo", async () => {
    mockApi(
      {
        Projects: () => ({
          projects: [
            { ...SECOND_PROJECT, slug: "ecidade", name: "e-cidade", profile: "ecidade", active: true },
            SECOND_PROJECT,
          ],
          activeProject: { slug: "ecidade" },
        }),
      },
      as("leitor"),
    );
    renderApp("/projetos");
    const list = await screen.findByRole("list", { name: "Projetos" });
    expect(within(list).getByText("Outro")).toBeInTheDocument();
    expect(screen.queryByText("Novo projeto")).not.toBeInTheDocument();
    for (const name of [/Usar este projeto/, /Editar/, /Remover/, /Rodar o scan|Atualizar o mapeamento/]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
      expect(screen.queryByRole("link", { name })).not.toBeInTheDocument();
    }
    expect(screen.getByRole("combobox", { name: "Projeto ativo" })).toBeDisabled();
  });

  test("a recusa por papel (FORBIDDEN) avisa o guarda para recarregar /auth/me", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              data: null,
              errors: [{ message: "ação restrita ao administrador", extensions: { code: "FORBIDDEN" } }],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
      ),
    );
    const heard = vi.fn();
    authEvents.addEventListener(FORBIDDEN_EVENT, heard);
    await expect(request(StatsQuery)).rejects.toThrow("ação restrita ao administrador");
    authEvents.removeEventListener(FORBIDDEN_EVENT, heard);
    expect(heard).toHaveBeenCalledTimes(1);
  });
});
