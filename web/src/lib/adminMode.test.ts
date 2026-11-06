import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  embedded: false,
  cookiePath: undefined as string | undefined,
  fetch: vi.fn(),
}));

vi.mock("./host", () => ({
  getOmnigentHostConfig: () => ({ adminModeCookiePath: mocks.cookiePath }),
  getOmnigentServerIdentity: () => "server-a",
  isDatabricksWorkspace: () => mocks.embedded,
  hostFetch: mocks.fetch,
}));

const reload = vi.fn();
let stopWatching: (() => void) | undefined;

beforeEach(() => {
  vi.resetModules();
  mocks.embedded = false;
  mocks.cookiePath = undefined;
  mocks.fetch.mockReset();
  reload.mockReset();
  stopWatching = undefined;
  localStorage.clear();
  document.cookie = "omnigent_admin_mode=; Path=/; Max-Age=0";
  document.cookie = "omnigent_admin_mode=; Path=/proxy/6767; Max-Age=0";
  document.cookie = "omnigent_admin_mode_server_a=; Path=/; Max-Age=0";
  document.cookie = "omnigent_admin_mode_server_b=; Path=/; Max-Age=0";
  delete window.__OMNIGENT_BASE_PATH__;
  vi.stubGlobal("location", { protocol: "http:", reload });
});

afterEach(() => {
  cleanup();
  stopWatching?.();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function eligibleAdmin(enabled = false) {
  const mode = await import("./adminMode");
  mode.initializeAdminMode();
  stopWatching = mode.subscribeAdminModeChanges();
  if (!document.cookie.includes("omnigent_admin_mode_server_a=")) {
    document.cookie = "omnigent_admin_mode_server_a=off; Path=/";
  }
  mode.resolveAdminMode({
    user_id: "admin@example.test",
    can_enable_admin_mode: true,
    admin_mode_enabled: enabled,
    admin_mode_cookie_name: "omnigent_admin_mode_server_a",
  });
  return mode;
}

describe("browser admin mode", () => {
  it("opts into bootstrap without writing a generic cookie or overwriting scoped state", async () => {
    document.cookie = "omnigent_admin_mode_server_a=accepted-user-hash; Path=/";
    const cookie = vi.spyOn(document, "cookie", "set");
    const mode = await import("./adminMode");
    mode.initializeAdminMode();
    mode.initializeAdminMode();
    expect(mode.usesBrowserAdminMode()).toBe(true);
    expect(cookie).not.toHaveBeenCalled();
    expect(document.cookie).not.toContain("omnigent_admin_mode=");
    expect(document.cookie).toContain("omnigent_admin_mode_server_a=accepted-user-hash");
  });

  it("opts standalone subpath deployments into the server handshake", async () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    const cookie = vi.spyOn(document, "cookie", "set");
    const mode = await import("./adminMode");
    mode.initializeAdminMode();
    expect(mode.usesBrowserAdminMode()).toBe(true);
    expect(cookie).not.toHaveBeenCalled();
  });

  it("requires embedded hosts to opt into the shared cookie path", async () => {
    mocks.embedded = true;
    const mode = await eligibleAdmin();
    expect(document.cookie).not.toContain("omnigent_admin_mode=");
    expect(mode.getAdminMode()).toBeNull();
    expect(mode.usesBrowserAdminMode()).toBe(false);

    mocks.cookiePath = "/";
    mode.initializeAdminMode();
    mode.resolveAdminMode({
      user_id: "admin@example.test",
      can_enable_admin_mode: true,
      admin_mode_enabled: false,
      admin_mode_cookie_name: "omnigent_admin_mode_server_a",
    });
    expect(mode.getAdminMode()).toBe(false);
    expect(mode.usesBrowserAdminMode()).toBe(true);
  });

  it("reacts to server identity and hides the switch for old servers and ordinary users", async () => {
    const mode = await eligibleAdmin();
    const { result } = renderHook(mode.useAdminMode);
    expect(result.current).toBe(false);

    act(() =>
      mode.resolveAdminMode({
        user_id: "admin@example.test",
        can_enable_admin_mode: true,
        admin_mode_enabled: true,
        admin_mode_cookie_name: "omnigent_admin_mode_server_a",
      }),
    );
    expect(result.current).toBe(true);
    act(() => mode.resolveAdminMode({ user_id: "admin@example.test" }));
    expect(result.current).toBeNull();
    act(() =>
      mode.resolveAdminMode({
        user_id: "member@example.test",
        can_enable_admin_mode: false,
        admin_mode_enabled: false,
        admin_mode_cookie_name: "omnigent_admin_mode_server_a",
      }),
    );
    expect(result.current).toBeNull();
  });

  it("signals the current server and account then reloads immediately after an accepted change", async () => {
    const mode = await eligibleAdmin();
    vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });
    mocks.fetch.mockResolvedValue(new Response("{}", { status: 200 }));
    await mode.setAdminMode(true);

    expect(mocks.fetch).toHaveBeenCalledExactlyOnceWith("/v1/me/admin-mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: true }),
    });
    expect(localStorage.key(0)).toBe(
      'omnigent:admin-mode-change:["server-a","/","omnigent_admin_mode_server_a","admin@example.test"]',
    );
    expect(localStorage.getItem(localStorage.key(0)!)).toBeTruthy();
    expect(reload).toHaveBeenCalledOnce();
  });

  it("shows a friendly message when changing mode fails before receiving a response", async () => {
    const mode = await eligibleAdmin();
    mocks.fetch.mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(mode.setAdminMode(true)).rejects.toThrow(
      "Could not change admin mode. Try again.",
    );
    expect(reload).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
  });

  it("keeps the current view after a rejected change", async () => {
    const mode = await eligibleAdmin();
    mocks.fetch.mockResolvedValue(new Response(null, { status: 403 }));
    await expect(mode.setAdminMode(true)).rejects.toThrow("Could not change admin mode");
    expect(reload).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
  });

  it("reloads other views for this account, but not another server or account", async () => {
    await eligibleAdmin(true);
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: 'omnigent:admin-mode-change:["server-b","/","omnigent_admin_mode_server_b","admin@example.test"]',
        newValue: "changed",
      }),
    );
    expect(reload).not.toHaveBeenCalled();
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: 'omnigent:admin-mode-change:["server-a","/","omnigent_admin_mode_server_a","member@example.test"]',
        newValue: "changed",
      }),
    );
    expect(reload).not.toHaveBeenCalled();
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: 'omnigent:admin-mode-change:["server-a","/","omnigent_admin_mode_server_a","admin@example.test"]',
        newValue: "changed",
      }),
    );
    expect(reload).toHaveBeenCalledOnce();
  });

  it("refreshes a restored view and catches cookie changes when returning to a tab", async () => {
    await eligibleAdmin(true);
    window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
    expect(reload).toHaveBeenCalledTimes(1);
    document.cookie = "omnigent_admin_mode_server_b=changed; Path=/";
    window.dispatchEvent(new Event("focus"));
    expect(reload).toHaveBeenCalledTimes(1);
    document.cookie = "omnigent_admin_mode_server_a=changed; Path=/";
    window.dispatchEvent(new Event("focus"));
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it("stops reloads on unmount and restores guards with cached identity on remount", async () => {
    document.cookie = "omnigent_admin_mode_server_a=accepted-user-hash; Path=/";
    const mode = await eligibleAdmin(true);
    const changed = new StorageEvent("storage", {
      key: 'omnigent:admin-mode-change:["server-a","/","omnigent_admin_mode_server_a","admin@example.test"]',
      newValue: "changed",
    });
    stopWatching!();
    window.dispatchEvent(changed);
    window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
    document.cookie = "omnigent_admin_mode_server_a=changed; Path=/";
    window.dispatchEvent(new Event("focus"));
    expect(reload).not.toHaveBeenCalled();
    expect(mode.getAdminMode()).toBe(true);

    mode.initializeAdminMode();
    expect(document.cookie).toContain("omnigent_admin_mode_server_a=changed");
    stopWatching = mode.subscribeAdminModeChanges();
    expect(mode.getAdminMode()).toBe(true);
    expect(reload).toHaveBeenCalledTimes(1);
    window.dispatchEvent(changed);
    expect(reload).toHaveBeenCalledTimes(2);
  });
});
