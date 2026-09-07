import { afterEach, describe, expect, it, vi } from "vitest";

import { getCliServerUrl, setOmnigentHostConfig } from "./host";

afterEach(() => {
  setOmnigentHostConfig({});
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
