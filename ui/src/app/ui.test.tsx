/**
 * Testes da interface base, com a API falsa (`mockApi`) e a aplicação inteira
 * (`renderApp`): ⌘K, busca, detalhe com relações, tema e o painel "Código".
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { relations, screenNode, SCREEN, sourceFile, TABLE } from "../test/fixtures";
import { mockApi, renderApp } from "../test/utils";
import { THEME_KEY, ThemeProvider, ThemeToggle } from "./theme";

const SUGGEST = {
  suggest: [
    { nodeId: SCREEN, label: "File", modules: ["Empenho"] },
    { nodeId: TABLE, label: "Table", modules: ["Empenho"] },
  ],
};

function tableNode() {
  return {
    node: {
      id: TABLE,
      label: "Table",
      name: "public.empempenho",
      path: null,
      modules: ["Module:empenho"],
      attrs: { catalog: { prefix: "e60", description: "Empenhos" }, columns_catalog: {} },
      memberships: [],
    },
  };
}

describe("⌘K (busca global)", () => {
  test("abre com Ctrl+K e navega pelo teclado até o detalhe", async () => {
    const user = userEvent.setup();
    mockApi({
      Suggest: () => SUGGEST,
      NodeDetail: (vars) => (vars.id === TABLE ? tableNode() : screenNode()),
      Relations: relations,
    });
    const { router } = renderApp("/");

    await user.keyboard("{Control>}k{/Control}");
    const input = await screen.findByPlaceholderText(/Digite um nome/);
    await user.type(input, "anular");
    await screen.findByText("emp4_anularempenho001.php", {}, { timeout: 3000 });
    await user.keyboard("{ArrowDown}{Enter}"); // a 1ª já vem selecionada: desce para a tabela

    await waitFor(() => expect(router.state.location.pathname).toBe(`/no/${encodeURIComponent(TABLE)}`));
    expect(await screen.findByRole("heading", { level: 1, name: "empempenho" })).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/Digite um nome/)).not.toBeInTheDocument();
  });

  test("item de menu aparece pelo caminho no menu, não pelo ID", async () => {
    const user = userEvent.setup();
    mockApi({
      Suggest: () => ({ suggest: [{ nodeId: "BusinessAction:menu:7650", label: "BusinessAction", modules: [] }] }),
      NodeNames: () => ({
        nodes: [
          {
            id: "BusinessAction:menu:7650",
            name: "Anulação",
            attrs: { menu_path: "Material > Procedimentos > Anulação" },
          },
        ],
      }),
    });
    renderApp("/");
    await user.keyboard("{Control>}k{/Control}");
    await user.type(await screen.findByPlaceholderText(/Digite um nome/), "anula");
    expect(await screen.findByText("Material > Procedimentos > Anulação", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.queryByText("menu:7650")).not.toBeInTheDocument();
  });

  test("o botão do cabeçalho também abre, e Esc fecha", async () => {
    const user = userEvent.setup();
    mockApi();
    renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Buscar (Ctrl+K)" }));
    expect(await screen.findByPlaceholderText(/Digite um nome/)).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByPlaceholderText(/Digite um nome/)).not.toBeInTheDocument());
  });
});

describe("busca", () => {
  test("destaca o trecho e mostra o próximo comando", async () => {
    const calls = mockApi({
      SearchPage: () => ({
        search: [
          {
            nodeId: "BusinessAction:menu:9001",
            label: "BusinessAction",
            modules: ["Empenho"],
            score: 12,
            field: "name",
            snippet: "[Anulação] de [Empenho]",
            nextCommand: "php-map trace --entry emp4_anularempenho001.php",
          },
        ],
      }),
    });
    renderApp("/busca?q=anular%20empenho&label=BusinessAction");

    const snippet = await screen.findByTestId("snippet");
    const marks = within(snippet).getAllByText(/Anulação|Empenho/, { selector: "mark" });
    expect(marks.map((m) => m.textContent)).toEqual(["Anulação", "Empenho"]);
    expect(screen.getByText("php-map trace --entry emp4_anularempenho001.php")).toBeInTheDocument();
    const search = calls.find((c) => c.operation === "SearchPage");
    expect(search?.variables).toMatchObject({ text: "anular empenho", label: "BusinessAction", anyTerm: false });
  });

  test("sem resultado mostra as dicas", async () => {
    mockApi({ SearchPage: () => ({ search: [] }) });
    renderApp("/busca?q=anula%C3%A7%C3%A3o%20de%20empenho");
    expect(await screen.findByText(/Tente sem preposições/)).toBeInTheDocument();
  });
});

describe("detalhe de nó", () => {
  test("agrupa as relações por tipo e pagina no servidor", async () => {
    const user = userEvent.setup();
    const calls = mockApi({ NodeDetail: screenNode, Relations: relations });
    renderApp(`/no/${encodeURIComponent(SCREEN)}`);

    await user.click(await screen.findByRole("tab", { name: "Relações" }));
    const outgoing = await screen.findByRole("region", { name: "Para onde este nó aponta" });
    expect(await within(outgoing).findByTestId("grupo-INCLUDES")).toHaveTextContent("inclui (2)");
    expect(within(outgoing).getByTestId("grupo-REFERENCES")).toHaveTextContent("referencia (48)");
    expect(within(outgoing).queryByTestId("grupo-QUERIES")).not.toBeInTheDocument();
    expect(within(outgoing).getByText("1–50 de 120")).toBeInTheDocument();

    await user.click(within(outgoing).getByRole("button", { name: "Próxima página" }));
    await user.click(await within(outgoing).findByRole("button", { name: "Próxima página" }));
    expect(await within(outgoing).findByTestId("grupo-QUERIES")).toHaveTextContent("lê (2)");
    expect(within(outgoing).getByText("101–120 de 120")).toBeInTheDocument();
    const offsets = calls.filter((c) => c.operation === "Relations" && c.variables.direction === "OUT");
    expect(offsets.map((c) => c.variables.offset)).toEqual([0, 50, 100]);
  });

  test("mostra os módulos com a fonte e a confiança", async () => {
    mockApi({ NodeDetail: screenNode, Relations: relations });
    renderApp(`/no/${encodeURIComponent(SCREEN)}`);
    const modules = await screen.findByLabelText("Módulos");
    expect(within(modules).getByText("Empenho")).toBeInTheDocument();
    expect(within(modules).getByText("catálogo do e-cidade")).toBeInTheDocument();
  });
});

describe("tema", () => {
  test("alterna e persiste no localStorage", async () => {
    const user = userEvent.setup();
    const first = render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>,
    );
    await user.click(screen.getByRole("button", { name: "Usar o tema escuro" }));
    expect(document.documentElement).toHaveClass("dark");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("dark");
    first.unmount();

    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>,
    );
    expect(screen.getByRole("button", { name: "Usar o tema claro" })).toBeInTheDocument();
    expect(document.documentElement).toHaveClass("dark");
  });

  test("funciona com o localStorage bloqueado", async () => {
    const user = userEvent.setup();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("bloqueado", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("bloqueado", "SecurityError");
    });
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>,
    );
    await user.click(screen.getByRole("button", { name: "Usar o tema escuro" }));
    expect(document.documentElement).toHaveClass("dark");
  });
});

describe("painel Código", () => {
  async function openEvidence(overrides: Record<string, unknown> = {}, workspace?: unknown) {
    const user = userEvent.setup();
    const calls = mockApi({
      NodeDetail: screenNode,
      Relations: relations,
      SourceFile: () => sourceFile(overrides),
      ...(workspace ? { Workspace: () => workspace } : {}),
    });
    renderApp(`/no/${encodeURIComponent(SCREEN)}`);
    await user.click(await screen.findByRole("tab", { name: "Relações" }));
    const outgoing = await screen.findByRole("region", { name: "Para onde este nó aponta" });
    await user.click(await within(outgoing).findByRole("button", { name: /emp4_anularempenho001\.php:3$/ }));
    return { calls, dialog: await screen.findByRole("dialog") };
  }

  test("a evidência abre o painel com a linha destacada", async () => {
    const { calls, dialog } = await openEvidence();
    const line = await within(dialog).findByText("12", { selector: "span" }, { timeout: 3000 });
    const row = line.closest("[data-line]");
    expect(row).toHaveAttribute("data-line", "12");
    expect(row).toHaveAttribute("aria-current", "location");
    expect(within(dialog).getByRole("link", { name: /Abrir no VS Code/ })).toHaveAttribute(
      "href",
      "vscode://file/D:/ecidade/emp4_anularempenho001.php:12",
    );
    const request = calls.find((c) => c.operation === "SourceFile");
    expect(request?.variables).toEqual({ path: "emp4_anularempenho001.php", line: 3 });
  });

  test("arquivo grande mostra o aviso da janela", async () => {
    const lines = Array.from({ length: 41 }, (_, i) => `linha ${4980 + i}`);
    const { dialog } = await openEvidence({
      startLine: 4980,
      lines,
      highlight: 5000,
      totalLines: 57123,
      truncated: true,
    });
    expect(await within(dialog).findByText(/Arquivo grande: mostrando as linhas 4\.980–5\.020 de 57\.123/)).toBeInTheDocument();
  });

  test("sem pasta do e-cidade, explica como informar", async () => {
    const { calls, dialog } = await openEvidence(
      {},
      { workspace: { source: null, origin: "nenhuma", exists: false, isEcidade: false, graphSource: null } },
    );
    expect(await within(dialog).findByText(/nenhuma pasta do e-cidade em uso/)).toBeInTheDocument();
    expect(within(dialog).getByText(/php-map ui --source/)).toBeInTheDocument();
    expect(calls.some((c) => c.operation === "SourceFile")).toBe(false);
  });
});
