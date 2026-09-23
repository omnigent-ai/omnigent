import { afterEach, describe, expect, it, vi } from "vitest";

import { getCliServerUrl, hostFetch, resolveWebSocketUrl, setOmnigentHostConfig } from "./host";

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
    await freshHostFetch("/v1/sessions");
    expect(fetcher).toHaveBeenCalledWith("/v1/sessions", undefined);
    vi.resetModules();
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
