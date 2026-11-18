import { afterEach, describe, expect, test, vi } from "vitest";
import { graphql } from "../graphql/generated";
import { request } from "./api";
import { authEvents, fetchMe, LOGIN_REQUIRED_EVENT, postAuth, safeReturn } from "./auth";

const Doc = graphql(`
  query Stats {
    stats
  }
`);

afterEach(() => vi.unstubAllGlobals());

describe("auth", () => {
  test("um 401 da API avisa o guarda de login e vira mensagem em português", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ error: "login" }), { status: 401, headers: { "content-type": "application/json" } })),
    );
    const seen = vi.fn();
    authEvents.addEventListener(LOGIN_REQUIRED_EVENT, seen);
    await expect(request(Doc)).rejects.toThrow("faça login para continuar");
    authEvents.removeEventListener(LOGIN_REQUIRED_EVENT, seen);
    expect(seen).toHaveBeenCalledTimes(1);
  });

  test("fetchMe cai para «sem login» quando o servidor não responde ou responde outra coisa", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>", { status: 200 })));
    expect((await fetchMe()).available).toBe(false);
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("rede"))));
    expect((await fetchMe()).loginRequired).toBe(false);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ loginRequired: true, setupRequired: true, user: null }), { status: 200 })),
    );
    expect(await fetchMe()).toMatchObject({ loginRequired: true, setupRequired: true, available: true });
  });

  test("postAuth devolve a mensagem do servidor e manda JSON com o cookie da mesma origem", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ error: "muitas tentativas; tente de novo em 3 min" }), { status: 429 }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await postAuth("/login", { username: "a", password: "b" })).toEqual({
      ok: false,
      status: 429,
      error: "muitas tentativas; tente de novo em 3 min",
    });
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(init.headers).toEqual({ "content-type": "application/json" });
  });

  test("safeReturn recusa endereço externo e a própria tela de login", () => {
    expect(safeReturn("/regras?x=1")).toBe("/regras?x=1");
    expect(safeReturn("//evil.example")).toBe("/");
    expect(safeReturn("https://evil.example")).toBe("/");
    expect(safeReturn("/login?volta=/")).toBe("/");
    expect(safeReturn("/configuracao")).toBe("/");
    expect(safeReturn(null)).toBe("/");
  });
});
