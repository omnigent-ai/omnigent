import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

describe("hostFetch session recovery", () => {
  const reload = vi.fn();
  const fetcher = vi.fn();
  const expiredSession = new Error("Fetch request failed due to expired user session");
  let originalLocation: Location;

  async function loadHost() {
    const host = await import("./host");
    host.setOmnigentHostConfig({ fetcher });
    return host;
  }

  beforeEach(() => {
    vi.resetModules();
    reload.mockReset();
    fetcher.mockReset();
    window.sessionStorage.clear();
    originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { reload },
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: originalLocation,
    });
  });

  it.each([
    "Fetch request failed due to expired user session",
    "Fetch request failed due expired user session",
  ])("reloads for %s and preserves the original error", async (message) => {
    const error = new Error(message);
    fetcher.mockRejectedValue(error);
    const { hostFetch } = await loadHost();

    await expect(hostFetch("/v1/sessions")).rejects.toBe(error);

    expect(reload).toHaveBeenCalledExactlyOnceWith();
  });

  it("coalesces concurrent failures without replaying writes", async () => {
    fetcher.mockRejectedValue(expiredSession);
    const { hostFetch } = await loadHost();
    const init = { method: "POST", body: JSON.stringify({ title: "New session" }) };

    await Promise.all(
      Array.from({ length: 8 }, () =>
        expect(hostFetch("/v1/sessions", init)).rejects.toBe(expiredSession),
      ),
    );

    expect(reload).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledTimes(8);
    expect(fetcher).toHaveBeenCalledWith("/v1/sessions", init);
  });

  it("does not reload-loop across page loads while the session stays expired", async () => {
    fetcher.mockRejectedValue(expiredSession);
    const firstPage = await loadHost();
    await expect(firstPage.hostFetch("/v1/me")).rejects.toBe(expiredSession);

    vi.resetModules();
    const nextPage = await loadHost();
    await expect(nextPage.hostFetch("/v1/me")).rejects.toBe(expiredSession);
    await expect(nextPage.hostFetch("/v1/sessions")).rejects.toBe(expiredSession);

    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("allows recovery again after a successful identity probe on the next page", async () => {
    fetcher.mockRejectedValue(expiredSession);
    const firstPage = await loadHost();
    await expect(firstPage.hostFetch("/v1/sessions")).rejects.toBe(expiredSession);

    vi.resetModules();
    const nextPage = await loadHost();
    const response = new Response(JSON.stringify({ user_id: "user@example.com" }));
    fetcher.mockResolvedValueOnce(response);
    await expect(nextPage.hostFetch("/v1/me")).resolves.toBe(response);
    await expect(nextPage.hostFetch("/v1/sessions")).rejects.toBe(expiredSession);

    expect(reload).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["/health", 200],
    ["/v1/me", 401],
    ["/v1/me", 500],
  ] as const)("does not reset recovery after %s returns %s", async (path, status) => {
    fetcher.mockRejectedValue(expiredSession);
    const firstPage = await loadHost();
    await expect(firstPage.hostFetch("/v1/sessions")).rejects.toBe(expiredSession);

    vi.resetModules();
    const nextPage = await loadHost();
    fetcher.mockResolvedValueOnce(new Response(null, { status }));
    await nextPage.hostFetch(path);
    await expect(nextPage.hostFetch("/v1/sessions")).rejects.toBe(expiredSession);

    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("does not clear the guard when an in-flight identity probe finishes before navigation", async () => {
    let resolveIdentity!: (response: Response) => void;
    const identityResponse = new Promise<Response>((resolve) => {
      resolveIdentity = resolve;
    });
    fetcher.mockReturnValueOnce(identityResponse).mockRejectedValue(expiredSession);
    const firstPage = await loadHost();
    const identityRequest = firstPage.hostFetch("/v1/me");
    await expect(firstPage.hostFetch("/v1/sessions")).rejects.toBe(expiredSession);
    resolveIdentity(new Response("{}"));
    await identityRequest;

    vi.resetModules();
    const nextPage = await loadHost();
    await expect(nextPage.hostFetch("/v1/me")).rejects.toBe(expiredSession);

    expect(reload).toHaveBeenCalledTimes(1);
  });

  it.each([
    new TypeError("Failed to fetch"),
    new DOMException("The operation was aborted", "AbortError"),
    new Error("Permission denied"),
  ])("does not reload for unrelated failures: %s", async (error) => {
    fetcher.mockRejectedValue(error);
    const { hostFetch } = await loadHost();

    await expect(hostFetch("/v1/sessions")).rejects.toBe(error);

    expect(reload).not.toHaveBeenCalled();
  });

  it("leaves embedded 401 responses to the host", async () => {
    const response = new Response(null, { status: 401 });
    fetcher.mockResolvedValue(response);
    const { hostFetch } = await loadHost();

    await expect(hostFetch("/v1/sessions")).resolves.toBe(response);

    expect(reload).not.toHaveBeenCalled();
  });

  it("does not reload standalone requests", async () => {
    vi.stubGlobal("fetch", fetcher.mockRejectedValue(expiredSession));
    const { hostFetch } = await import("./host");

    await expect(hostFetch("/v1/sessions")).rejects.toBe(expiredSession);

    expect(reload).not.toHaveBeenCalled();
  });

  it.each(["getItem", "setItem"] as const)(
    "does not reload without a persistent loop guard when storage.%s fails",
    async (method) => {
      vi.spyOn(Storage.prototype, method).mockImplementation(() => {
        throw new DOMException("Storage is disabled", "SecurityError");
      });
      fetcher.mockRejectedValue(expiredSession);
      const { hostFetch } = await loadHost();

      await expect(hostFetch("/v1/sessions")).rejects.toBe(expiredSession);

      expect(reload).not.toHaveBeenCalled();
    },
  );

  it("preserves successful responses when clearing storage fails", async () => {
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("Storage is disabled", "SecurityError");
    });
    const response = new Response("{}");
    fetcher.mockResolvedValue(response);
    const { hostFetch } = await loadHost();

    await expect(hostFetch("/v1/me")).resolves.toBe(response);
  });
});
