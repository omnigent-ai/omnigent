import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getMe, login, logout } from "./accountsApi";

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.__OMNIGENT_BASE_PATH__;
});

function jsonResponse(body: unknown, ok = true): Response {
  return { ok, status: ok ? 200 : 401, json: async () => body } as unknown as Response;
}

describe("accountsApi base path", () => {
  it("prefixes POST /auth/login", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ user: { id: "u1", is_admin: false }, token: "t", expires_in: 1 }),
    );
    await login({ username: "a", password: "b" });
    expect(fetchMock.mock.calls[0][0]).toBe("/proxy/6767/auth/login");
  });

  it("prefixes POST /auth/logout", async () => {
    fetchMock.mockResolvedValue(jsonResponse(null));
    await logout();
    expect(fetchMock.mock.calls[0][0]).toBe("/proxy/6767/auth/logout");
  });

  it("prefixes GET /auth/me", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: "u1", is_admin: false }));
    await getMe();
    expect(fetchMock.mock.calls[0][0]).toBe("/proxy/6767/auth/me");
  });
});
