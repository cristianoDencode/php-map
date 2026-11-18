import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test } from "vitest";
import { mockApi, renderApp } from "../test/utils";

type Me = { loginRequired: boolean; setupRequired: boolean; user: { username: string } | null };
const LOGGED_OUT: Me = { loginRequired: true, setupRequired: false, user: null };
const LOGGED_IN: Me = { loginRequired: true, setupRequired: false, user: { username: "ana" } };
const SETUP: Me = { loginRequired: true, setupRequired: true, user: null };

describe("login", () => {
  test("com login exigido e sem sessão, a rota protegida vai para /login com a volta", async () => {
    mockApi({}, { "/auth/me": () => ({ body: LOGGED_OUT }) });
    const { router } = renderApp("/busca?q=empenho");
    expect(await screen.findByRole("heading", { name: "Entrar" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(router.state.location.search).toBe("?volta=%2Fbusca%3Fq%3Dempenho");
  });

  test("entrar manda usuário e senha, mostra o erro do servidor e depois volta à rota", async () => {
    const user = userEvent.setup();
    let me = LOGGED_OUT;
    const calls = mockApi(
      {},
      {
        "/auth/me": () => ({ body: me }),
        "/auth/login": (body) => {
          if (body.password !== "senha-certa-123") return { status: 401, body: { error: "usuário ou senha inválidos" } };
          me = LOGGED_IN;
          return { status: 204 };
        },
      },
    );
    const { router } = renderApp("/login?volta=%2Fregras");
    await user.type(await screen.findByLabelText("E-mail"), "ana@exemplo.com");
    await user.type(screen.getByLabelText("Senha"), "errada");
    await user.click(screen.getByRole("button", { name: "Entrar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("usuário ou senha inválidos");
    expect(screen.getByLabelText("Senha")).toHaveValue("");
    await user.type(screen.getByLabelText("Senha"), "senha-certa-123");
    await user.click(screen.getByRole("button", { name: "Entrar" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/regras"));
    expect(calls.filter((c) => c.operation === "AUTH /auth/login").map((c) => c.variables)).toEqual([
      { email: "ana@exemplo.com", password: "errada" },
      { email: "ana@exemplo.com", password: "senha-certa-123" },
    ]);
  });

  test("a volta só aceita caminho da própria interface", async () => {
    const user = userEvent.setup();
    let me = LOGGED_OUT;
    mockApi({}, { "/auth/me": () => ({ body: me }), "/auth/login": () => ((me = LOGGED_IN), { status: 204 }) });
    const { router } = renderApp("/login?volta=%2F%2Fevil.example%2Fx");
    await user.type(await screen.findByLabelText("E-mail"), "ana@exemplo.com");
    await user.type(screen.getByLabelText("Senha"), "senha-certa-123");
    await user.click(screen.getByRole("button", { name: "Entrar" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });

  test("logado, o cabeçalho mostra o usuário e «Sair» encerra a sessão e volta ao login", async () => {
    const user = userEvent.setup();
    let me = LOGGED_IN;
    const calls = mockApi({}, { "/auth/me": () => ({ body: me }), "/auth/logout": () => ((me = LOGGED_OUT), { status: 204 }) });
    const { router } = renderApp("/");
    expect(await screen.findByText("ana")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Sair/ }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(calls.some((c) => c.operation === "AUTH /auth/logout")).toBe(true);
  });

  test("trocar a senha: confere as duas novas e mostra o erro do servidor", async () => {
    const user = userEvent.setup();
    const calls = mockApi(
      {},
      {
        "/auth/me": () => ({ body: LOGGED_IN }),
        "/auth/password": (body) =>
          body.current === "atual-correta-1" ? { status: 204 } : { status: 400, body: { error: "a senha atual não confere" } },
      },
    );
    renderApp("/");
    await user.click(await screen.findByRole("button", { name: /Trocar senha/ }));
    await user.type(screen.getByLabelText("Senha atual"), "errada-errada");
    await user.type(screen.getByLabelText("Nova senha"), "nova-senha-longa");
    await user.type(screen.getByLabelText("Repita a nova senha"), "nova-senha-diferente");
    await user.click(screen.getByRole("button", { name: "Trocar senha" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("não conferem");
    await user.clear(screen.getByLabelText("Repita a nova senha"));
    await user.type(screen.getByLabelText("Repita a nova senha"), "nova-senha-longa");
    await user.click(screen.getByRole("button", { name: "Trocar senha" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("a senha atual não confere");
    await user.clear(screen.getByLabelText("Senha atual"));
    await user.type(screen.getByLabelText("Senha atual"), "atual-correta-1");
    await user.click(screen.getByRole("button", { name: "Trocar senha" }));
    expect(await screen.findByText("Senha trocada.")).toBeInTheDocument();
    expect(calls.filter((c) => c.operation === "AUTH /auth/password").at(-1)?.variables).toEqual({
      current: "atual-correta-1",
      new: "nova-senha-longa",
    });
  });

  test("sem usuário no cofre, qualquer rota vai para a configuração inicial (e não há aviso «Sem login»)", async () => {
    mockApi({}, { "/auth/me": () => ({ body: SETUP }) });
    const { router } = renderApp("/regras");
    expect(await screen.findByRole("heading", { name: "Configuração inicial" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/configuracao");
    expect(screen.queryByText(/Sem login/)).not.toBeInTheDocument();
  });

  test("/login em modo configuração também leva à configuração inicial", async () => {
    mockApi({}, { "/auth/me": () => ({ body: SETUP }) });
    const { router } = renderApp("/login");
    expect(await screen.findByRole("heading", { name: "Configuração inicial" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/configuracao");
  });

  test("configuração: confere as senhas, mostra código errado e, com o certo, cria o admin e entra", async () => {
    const user = userEvent.setup();
    let me: Me = SETUP;
    const calls = mockApi(
      {},
      {
        "/auth/me": () => ({ body: me }),
        "/auth/first-user": (body) => {
          if (body.setupCode !== "codigo-certo") return { status: 403, body: { error: "setup-code" } };
          me = LOGGED_IN;
          return { status: 201 };
        },
      },
    );
    const { router } = renderApp("/configuracao");
    await user.type(await screen.findByLabelText("Código de configuração"), "codigo-errado");
    await user.type(screen.getByLabelText("E-mail"), "ana@exemplo.com");
    await user.type(screen.getByLabelText("Senha"), "senha-certa-123");
    await user.type(screen.getByLabelText("Repita a senha"), "outra-coisa-123");
    await user.click(screen.getByRole("button", { name: "Criar e entrar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("não conferem");
    expect(calls.some((c) => c.operation === "AUTH /auth/first-user")).toBe(false);

    await user.clear(screen.getByLabelText("Repita a senha"));
    await user.type(screen.getByLabelText("Repita a senha"), "senha-certa-123");
    await user.click(screen.getByRole("button", { name: "Criar e entrar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("código de configuração incorreto");
    expect(screen.getByLabelText("Senha")).toHaveValue("");

    await user.clear(screen.getByLabelText("Código de configuração"));
    await user.type(screen.getByLabelText("Código de configuração"), " codigo-certo ");
    await user.type(screen.getByLabelText("Senha"), "senha-certa-123");
    await user.type(screen.getByLabelText("Repita a senha"), "senha-certa-123");
    await user.click(screen.getByRole("button", { name: "Criar e entrar" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    expect(calls.filter((c) => c.operation === "AUTH /auth/first-user").at(-1)?.variables).toEqual({
      email: "ana@exemplo.com",
      password: "senha-certa-123",
      setupCode: "codigo-certo",
    });
    expect(await screen.findByText("ana")).toBeInTheDocument();
  });

  test("configuração: mostra a recusa da senha curta vinda do servidor", async () => {
    const user = userEvent.setup();
    mockApi(
      {},
      {
        "/auth/me": () => ({ body: SETUP }),
        "/auth/first-user": () => ({ status: 400, body: { error: "a senha precisa de pelo menos 10 caracteres" } }),
      },
    );
    renderApp("/configuracao");
    await user.type(await screen.findByLabelText("Código de configuração"), "abc");
    await user.type(screen.getByLabelText("E-mail"), "ana@exemplo.com");
    await user.type(screen.getByLabelText("Senha"), "curta");
    await user.type(screen.getByLabelText("Repita a senha"), "curta");
    await user.click(screen.getByRole("button", { name: "Criar e entrar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("10 caracteres");
  });

  test("configuração: o e-mail inválido não chega ao servidor (a tela exige o formato)", async () => {
    const user = userEvent.setup();
    const calls = mockApi({}, { "/auth/me": () => ({ body: SETUP }) });
    renderApp("/configuracao");
    const field = await screen.findByLabelText("E-mail");
    expect(field).toHaveAttribute("type", "email");
    await user.type(await screen.findByLabelText("Código de configuração"), "abc");
    await user.type(field, "sem-arroba");
    await user.type(screen.getByLabelText("Senha"), "uma-senha-longa");
    await user.type(screen.getByLabelText("Repita a senha"), "uma-senha-longa");
    await user.click(screen.getByRole("button", { name: "Criar e entrar" }));
    expect(calls.some((c) => c.operation === "AUTH /auth/first-user")).toBe(false);
  });

  test("com o admin já criado, /configuracao volta para a interface", async () => {
    mockApi({}, { "/auth/me": () => ({ body: LOGGED_IN }) });
    const { router } = renderApp("/configuracao");
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });

  test("servidor sem /auth (404): a interface funciona como antes, sem aviso de login", async () => {
    mockApi({}, { "/auth/me": () => ({ status: 404, body: {} }) });
    renderApp("/");
    expect(await screen.findByRole("button", { name: "Buscar (Ctrl+K)" })).toBeInTheDocument();
    expect(screen.queryByText(/Sem login/)).not.toBeInTheDocument();
  });
});
