import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getCliServerUrl,
  hostFetch,
  resolveMediaUrl,
  resolveWebSocketUrl,
  setOmnigentHostConfig,
  withRoutingKey,
} from "./host";

afterEach(() => {
  setOmnigentHostConfig({});
  delete window.__OMNIGENT_BASE_PATH__;
  vi.restoreAllMocks();
});

describe("getCliServerUrl", () => {
  it("returns window.location.origin when no suffix is configured", () => {
    setOmnigentHostConfig({});
    const url = getCliServerUrl();
    expect(url).toBe(window.location.origin);
  });

  it("appends the configured cliServerUrlSuffix", () => {
    setOmnigentHostConfig({ cliServerUrlSuffix: "/api/2.0/omnigent" });
    const url = getCliServerUrl();
    expect(url).toBe(`${window.location.origin}/api/2.0/omnigent`);
  });

  it("handles an empty string suffix the same as no suffix", () => {
    setOmnigentHostConfig({ cliServerUrlSuffix: "" });
    expect(getCliServerUrl()).toBe(window.location.origin);
  });

  it("includes the base path before the suffix when one is configured", () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    setOmnigentHostConfig({ cliServerUrlSuffix: "/api" });
    expect(getCliServerUrl()).toBe(`${window.location.origin}/proxy/6767/api`);
  });
});

describe("hostFetch base path", () => {
  it("prepends the base path to standalone fetch calls", async () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(null, { status: 200 }));
    await hostFetch("/v1/sessions");
    expect(fetchSpy).toHaveBeenCalledWith("/proxy/6767/v1/sessions", undefined);
  });

  it("does not rebase when embedded (a host fetcher is installed)", async () => {
    // A real fetcher can't be cleared by `setOmnigentHostConfig({})` (the
    // guard that stops a Suspense/concurrent re-render from wiping an
    // installed host transport — see `host.ts`), so this uses its own fresh
    // module instance rather than the shared top-of-file import, to avoid
    // leaking an installed fetcher into later tests.
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    vi.resetModules();
    const { hostFetch: freshHostFetch, setOmnigentHostConfig: setConfig } = await import("./host");
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    setConfig({ fetcher });
    vi.resetModules();
    await freshHostFetch("/v1/sessions");
    expect(fetcher.mock.calls[0][0]).toBe("/v1/sessions");
    // The installed fetcher marks a workspace, so the request names a replica.
    const init = fetcher.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get("X-Databricks-Omnigent-Slice-Key")).toMatch(
      /^[0-9a-f]{32}$/,
    );
  });
});

describe("routing key", () => {
  // An installed fetcher marks a Databricks workspace, which turns host routing on.
  async function routedHost() {
    vi.resetModules();
    const host = await import("./host");
    host.setOmnigentHostConfig({
      fetcher: vi.fn().mockResolvedValue(new Response(null, { status: 200 })),
    });
    vi.resetModules();
    return host;
  }

  it("adds the client key to a request that carries none", async () => {
    const routed = await routedHost();
    const key = new Headers(routed.withRoutingKey({ method: "POST" })?.headers).get(
      "X-Databricks-Omnigent-Slice-Key",
    );
    expect(key).toMatch(/^[0-9a-f]{32}$/);
    const own = routed.withRoutingKey({ headers: { "X-Databricks-Omnigent-Slice-Key": "host_1" } });
    expect(new Headers(own?.headers).get("X-Databricks-Omnigent-Slice-Key")).toBe("host_1");
    expect(routed.withRoutingKey({ method: "GET" }, { unkeyed: true })).toEqual({ method: "GET" });
  });

  it("keys a hostless request by the modal host once it resolves", async () => {
    vi.resetModules();
    const sessionHost = await import("./sessionHost");
    const host = await import("./host");
    host.setOmnigentHostConfig({
      fetcher: vi.fn().mockResolvedValue(new Response(null, { status: 200 })),
    });
    sessionHost.setSessionHost("session-a", "host_modal");
    const keyOf = (init?: RequestInit) =>
      new Headers(host.withRoutingKey(init, { hostless: true })?.headers).get(
        "X-Databricks-Omnigent-Slice-Key",
      );
    expect(keyOf()).toMatch(/^[0-9a-f]{32}$/);
    sessionHost.resolveModalHost(["host_modal"]);
    expect(keyOf()).toBe("host_modal");
    expect(host.resolveWebSocketUrl("/v1/sessions/updates", { hostless: true })).toContain(
      "omnigent_slice_key=host_modal",
    );
  });

  it("adds the client key to a WebSocket path that carries none", async () => {
    const { withRoutingKeyParam, resolveWebSocketUrl: routedWebSocketUrl } = await routedHost();
    expect(routedWebSocketUrl("/v1/dictation/stream")).toMatch(
      /\/v1\/dictation\/stream\?omnigent_slice_key=[0-9a-f]{32}$/,
    );
    expect(routedWebSocketUrl("/v1/x", { unkeyed: true })).not.toContain("omnigent_slice_key");
    expect(withRoutingKeyParam("/v1/x?a=1")).toMatch(
      /^\/v1\/x\?a=1&omnigent_slice_key=[0-9a-f]{32}$/,
    );
    expect(withRoutingKeyParam("/v1/x?omnigent_slice_key=host_1")).toBe(
      "/v1/x?omnigent_slice_key=host_1",
    );
    expect(withRoutingKeyParam("/v1/x", { unkeyed: true })).toBe("/v1/x");
  });

  it("leaves requests unkeyed when host routing is off", () => {
    expect(withRoutingKey(undefined)).toBeUndefined();
    expect(resolveWebSocketUrl("/v1/x")).not.toContain("omnigent_slice_key");
  });
});

describe("resolveWebSocketUrl base path", () => {
  it("prepends the base path to the standalone WebSocket URL", () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    const url = resolveWebSocketUrl("/v1/sessions/abc/stream");
    expect(url).toBe(
      `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/proxy/6767/v1/sessions/abc/stream`,
    );
  });
});

describe("resolveMediaUrl", () => {
  it("is null without a host resolver, so standalone media keeps the fetch path", () => {
    expect(resolveMediaUrl("/v1/sessions/abc/demo.mp4?download=true")).toBeNull();
  });

  it("delegates to the host resolver", () => {
    setOmnigentHostConfig({ resolveMediaUrl: (path) => `/host${path}&csrf=t` });
    expect(resolveMediaUrl("/v1/sessions/abc/demo.mp4?download=true")).toBe(
      "/host/v1/sessions/abc/demo.mp4?download=true&csrf=t",
    );
  });
});

describe("isDatabricksWorkspace", () => {
  // `hostConfig` and the inlined `import.meta.env` are module state, so each case
  // resets modules and re-imports for a clean slate (the `setOmnigentHostConfig`
  // guard won't let an empty config clear an installed fetcher otherwise).
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("is false for a bare local / self-hosted server (no fetcher, no flag)", async () => {
    const { isDatabricksWorkspace } = await import("./host");
    expect(isDatabricksWorkspace()).toBe(false);
  });

  it("is true when embedded (a host fetcher is installed)", async () => {
    const { isDatabricksWorkspace, setOmnigentHostConfig: setConfig } = await import("./host");
    setConfig({ fetcher: (path, init) => fetch(path, init) });
    expect(isDatabricksWorkspace()).toBe(true);
  });

  it("is true in standalone dev against a workspace (VITE_DATABRICKS_WORKSPACE)", async () => {
    // `npm run dev` at a workspace URL installs no fetcher; the build-time flag
    // is the only signal that the server is a Databricks workspace.
    vi.stubEnv("VITE_DATABRICKS_WORKSPACE", "true");
    const { isDatabricksWorkspace } = await import("./host");
    expect(isDatabricksWorkspace()).toBe(true);
  });
});

describe("isHostRoutingEnabled", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("defaults to disabled for an OSS server", async () => {
    const { isHostRoutingEnabled } = await import("./host");
    expect(isHostRoutingEnabled()).toBe(false);
  });

  it("enables OSS host routing without enabling Databricks behavior", async () => {
    vi.stubEnv("VITE_OMNIGENT_HOST_ROUTING", "true");
    const { isHostRoutingEnabled, isDatabricksWorkspace } = await import("./host");
    expect(isHostRoutingEnabled()).toBe(true);
    expect(isDatabricksWorkspace()).toBe(false);
  });

  it("keeps host routing enabled for Databricks", async () => {
    vi.stubEnv("VITE_DATABRICKS_WORKSPACE", "true");
    const { isHostRoutingEnabled } = await import("./host");
    expect(isHostRoutingEnabled()).toBe(true);
  });
});
