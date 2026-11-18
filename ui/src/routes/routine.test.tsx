/** Seção "Definição" do nó `PgFunction`: texto da função em páginas de 200 linhas. */
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test } from "vitest";
import { readAtLabel } from "../components/RoutineDefinition";
import { mockApi, renderApp } from "../test/utils";

const FN = "PgFunction:public.fc_calcula(int4)";
const PAGE = `/no/${encodeURIComponent(FN)}`;
const TOTAL = 450;
const LINES = Array.from({ length: TOTAL }, (_, i) => `linha ${i + 1} da função`);

function fnNode(label = "PgFunction") {
  return {
    node: {
      id: FN,
      label,
      name: "fc_calcula",
      path: null,
      modules: [],
      attrs: { language: "plpgsql", returns: "int4", args: ["int4"], source_file: "db:public.fc_calcula" },
      memberships: [],
    },
  };
}

function source(vars: Record<string, unknown>, overrides: Record<string, unknown> = {}) {
  const offset = Number(vars.offset);
  const limit = Number(vars.limit);
  const lines = LINES.slice(offset - 1, offset - 1 + limit);
  return {
    hasRoutineTexts: true,
    routineSource: {
      id: FN,
      schema: "public",
      name: "fc_calcula",
      language: "plpgsql",
      generated: false,
      readAt: "2026-11-03T10:00:00-03:00",
      sha1: "a".repeat(40),
      totalLines: TOTAL,
      startLine: offset,
      endLine: offset + lines.length - 1,
      lines,
      truncated: offset > 1 || offset + lines.length - 1 < TOTAL,
      ...overrides,
    },
  };
}

const NO_RELATIONS = {
  node: { id: FN, neighbors: { total: 0, items: [], nodes: [] } },
};

async function openDefinition(handler: (vars: Record<string, unknown>) => unknown) {
  const user = userEvent.setup();
  const calls = mockApi({ NodeDetail: () => fnNode(), Relations: () => NO_RELATIONS, RoutineSource: handler });
  renderApp(PAGE);
  await user.click(await screen.findByRole("tab", { name: "Definição" }));
  return { user, calls };
}

describe("definição da função da base", () => {
  test("a aba só existe para PgFunction", async () => {
    mockApi({ NodeDetail: () => fnNode("Table"), Relations: () => NO_RELATIONS });
    renderApp(PAGE);
    await screen.findByRole("tab", { name: "Resumo" });
    expect(screen.queryByRole("tab", { name: "Definição" })).not.toBeInTheDocument();
  });

  test("mostra as 200 primeiras linhas, a data do scan e pede mais de 200 em 200", async () => {
    const { user, calls } = await openDefinition((vars) => source(vars));
    const code = await screen.findByLabelText("Definição da função");
    expect(within(code).getByText("linha 1 da função")).toBeInTheDocument();
    expect(within(code).getByText("linha 200 da função")).toBeInTheDocument();
    expect(within(code).queryByText("linha 201 da função")).not.toBeInTheDocument();
    expect(screen.getByText("Mostrando 200 de 450 linhas")).toBeInTheDocument();
    expect(screen.getByText(/03\/11\/2026/)).toBeInTheDocument();
    expect(screen.getByText(/plpgsql · 450 linhas/)).toBeInTheDocument();
    expect(calls.find((c) => c.operation === "RoutineSource")?.variables).toEqual({
      id: FN,
      offset: 1,
      limit: 200,
    });

    await user.click(screen.getByRole("button", { name: "Mostrar mais" }));
    expect(await within(code).findByText("linha 400 da função")).toBeInTheDocument();
    expect(calls.filter((c) => c.operation === "RoutineSource").at(-1)?.variables).toMatchObject({ offset: 201 });

    await user.click(screen.getByRole("button", { name: "Mostrar mais" }));
    expect(await within(code).findByText("linha 450 da função")).toBeInTheDocument();
    expect(screen.getByText("Mostrando 450 de 450 linhas")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mostrar mais" })).not.toBeInTheDocument();
  });

  test("copia o texto carregado", async () => {
    const { user } = await openDefinition((vars) => source(vars));
    await screen.findByLabelText("Definição da função");
    await user.click(screen.getByRole("button", { name: /Copiar o texto carregado/ }));
    const copied = await navigator.clipboard.readText(); // o user-event instala o próprio clipboard
    expect(copied.split("\n")).toHaveLength(200);
    expect(copied.startsWith("linha 1 da função")).toBe(true);
  });

  test("função gerada pela auditoria leva o aviso", async () => {
    await openDefinition((vars) => source(vars, { generated: true }));
    expect(await screen.findByText(/gerada pela auditoria do e-cidade/)).toBeInTheDocument();
  });

  test("grafo sem a tabela diz para rodar o scan novo", async () => {
    await openDefinition(() => ({ hasRoutineTexts: false, routineSource: null }));
    expect(await screen.findByText(/escaneado sem ler o banco/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Definição da função")).not.toBeInTheDocument();
  });

  test("função sem texto, num grafo que tem a tabela", async () => {
    await openDefinition(() => ({ hasRoutineTexts: true, routineSource: null }));
    expect(await screen.findByText(/não tem texto no grafo/)).toBeInTheDocument();
  });

  test("readAtLabel formata a data e devolve o que não é data", () => {
    expect(readAtLabel("2026-11-03T10:00:00-03:00")).toMatch(/^03\/11\/2026/);
    expect(readAtLabel("sem data")).toBe("sem data");
  });
});
