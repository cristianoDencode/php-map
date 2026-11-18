/** Seção "Origem" do nó `PgFunction`: por onde a função é usada, e o motivo se não for. */
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test } from "vitest";
import { distanceLabel, nearestLabel, shareLabel, STATUS_LABEL } from "../components/RoutineOrigin";
import { mockApi, renderApp } from "../test/utils";

const FN = "PgFunction:public.fc_calcula(int4)";
const PAGE = `/no/${encodeURIComponent(FN)}`;

const NODE = {
  node: {
    id: FN,
    label: "PgFunction",
    name: "fc_calcula",
    path: null,
    modules: [],
    attrs: { language: "plpgsql", returns: "int4", args: ["int4"] },
    memberships: [],
  },
};
const NO_RELATIONS = { node: { id: FN, neighbors: { total: 0, items: [], nodes: [] } } };

const ORIGIN = {
  status: "com_origem",
  reason: "chega a 1 menu, 1 endpoint (os mais próximos)",
  module: "Module:cadastro",
  moduleConfidence: 0.42,
  generated: false,
  callersPhp: 83,
  callersRoutines: 13,
  triggers: 0,
  entries: [
    {
      kind: "menu",
      target: "BusinessAction:menu:1",
      label: "Cadastro > Relatórios > Cálculo do IPTU",
      distance: 2,
      via: ["File:cad4_calculo.php", "Method:cl_calculo::executar", FN],
      confidence: 0.9,
    },
    {
      kind: "endpoint",
      target: "Endpoint:ecidade:rpc:cai3_emitecarne.RPC.php#acao",
      label: "cai3_emitecarne.RPC.php#acao",
      distance: 1,
      via: ["File:cai3_emitecarne.RPC.php", FN],
      confidence: 1,
    },
  ],
  mentions: [],
};

async function open(origin: unknown, has = true) {
  const user = userEvent.setup();
  mockApi({
    NodeDetail: () => NODE,
    Relations: () => NO_RELATIONS,
    RoutineOrigin: () => ({ hasRoutineOrigins: has, routineOrigin: origin }),
  });
  renderApp(PAGE);
  await user.click(await screen.findByRole("tab", { name: "Origem" }));
  return user;
}

describe("origem da função da base", () => {
  test("a aba só existe para PgFunction", async () => {
    mockApi({ NodeDetail: () => ({ node: { ...NODE.node, label: "Table" } }), Relations: () => NO_RELATIONS });
    renderApp(PAGE);
    await screen.findByRole("tab", { name: "Resumo" });
    expect(screen.queryByRole("tab", { name: "Origem" })).not.toBeInTheDocument();
  });

  test("mostra o status, o motivo, o módulo e as entradas por tipo, com a distância", async () => {
    await open(ORIGIN);
    expect(await screen.findByText("Com origem")).toBeInTheDocument();
    expect(screen.getByText("chega a 1 menu, 1 endpoint (os mais próximos)")).toBeInTheDocument();
    expect(screen.getByText(/42% dos votos/)).toBeInTheDocument();
    expect(screen.getByText(/83 arquivo\(s\) ou método\(s\) PHP, 13 outra/)).toBeInTheDocument();

    const menus = screen.getByRole("region", { name: "Menus" });
    expect(within(menus).getByRole("link", { name: "Cadastro > Relatórios > Cálculo do IPTU" })).toBeInTheDocument();
    expect(within(menus).getByText("2 saltos")).toBeInTheDocument();
    expect(within(menus).getByText("0,9")).toBeInTheDocument(); // a confiança do caminho
    expect(within(menus).getByRole("link", { name: /Ver menus por fonte/ })).toHaveAttribute(
      "href",
      "/menus?fonte=cad4_calculo.php",
    );
    const endpoints = screen.getByRole("region", { name: "Endpoints" });
    expect(within(endpoints).getByText("chamada direta")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Rotas" })).not.toBeInTheDocument(); // tipo sem entrada
  });

  test("o caminho até a função fica recolhido e lista os nós", async () => {
    const user = await open(ORIGIN);
    const menus = await screen.findByRole("region", { name: "Menus" });
    await user.click(within(menus).getByText("Caminho até a função (3 nós)"));
    expect(within(menus).getByRole("link", { name: "cad4_calculo.php" })).toBeInTheDocument();
  });

  test("sem chamador explica o motivo e que não é código morto; menção é hipótese", async () => {
    await open({
      ...ORIGIN,
      status: "sem_chamador",
      reason: "ninguém a chama no código mapeado; pode ser usada por SQL dinâmico",
      module: null,
      moduleConfidence: null,
      entries: [],
      mentions: [{ file: "File:z.php", line: 7, confidence: 0.5, hypothesis: true }],
    });
    expect(await screen.findByText("Sem chamador")).toBeInTheDocument();
    expect(screen.getByText(/ninguém a chama no código mapeado/)).toBeInTheDocument();
    expect(screen.getByText(/não quer dizer código morto/)).toBeInTheDocument();
    const mentions = screen.getByRole("region", { name: "Menções por texto" });
    expect(within(mentions).getByText("hipótese")).toBeInTheDocument();
    expect(within(mentions).getByText("linha 7")).toBeInTheDocument();
    expect(screen.queryByText(/Módulo provável/)).not.toBeInTheDocument();
  });

  test("grafo escaneado antes do cálculo de origem manda rodar o scan novo", async () => {
    await open(null, false);
    expect(await screen.findByText(/escaneado antes do cálculo de origem/)).toBeInTheDocument();
  });

  test("função sem origem calculada, num grafo que tem a tabela", async () => {
    await open(null, true);
    expect(await screen.findByText(/não tem origem calculada/)).toBeInTheDocument();
  });

  test("rótulos e formatos", () => {
    expect(Object.keys(STATUS_LABEL)).toHaveLength(5);
    expect(distanceLabel(1)).toBe("chamada direta");
    expect(distanceLabel(3)).toBe("3 saltos");
    expect(shareLabel(0.4153)).toBe("42%");
    expect(shareLabel(null)).toBe("");
    expect(nearestLabel(1)).toBe("o mais próximo");
    expect(nearestLabel(5)).toBe("os 5 mais próximos");
  });
});
