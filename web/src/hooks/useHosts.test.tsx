import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  useDetectedCredentials,
  useHostModelOptions,
  useHosts,
  useInstallHarness,
  useInstallingHarnesses,
  useStoreCredential,
} from "./useHosts";

const fetchMock = vi.fn();

function mockResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => body,
  } as unknown as Response;
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("useHosts", () => {
  it("does not fetch while disabled", async () => {
    renderHook(() => useHosts({ enabled: false }), { wrapper });
    await Promise.resolve();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches hosts from /v1/hosts", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        hosts: [
          {
            host_id: "host_1",
            name: "Laptop",
            owner: "alice",
            status: "online",
            sandbox_provider: null,
          },
        ],
      }),
    );

    const { result } = renderHook(() => useHosts(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(fetchMock.mock.calls[0][0]).toBe("/v1/hosts");
    expect(result.current.data).toEqual([
      {
        host_id: "host_1",
        name: "Laptop",
        owner: "alice",
        status: "online",
        sandbox_provider: null,
      },
    ]);
  });

  it("hides server-managed sandbox hosts from the host list", async () => {
    // Every host picker (NewChatDialog, ForkSessionDialog,
    // ResumeWithDirectoryDialog) consumes this hook, so filtering here
    // is what keeps sandbox-backed hosts out of all of them. A host
    // with a non-null sandbox_provider is server-managed (created from
    // a managed sandbox); one without the field at all comes from an
    // older server and must be kept.
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        hosts: [
          {
            host_id: "host_sandbox",
            name: "sandbox-abc123",
            owner: "alice",
            status: "online",
            sandbox_provider: "modal",
          },
          {
            host_id: "host_laptop",
            name: "Laptop",
            owner: "alice",
            status: "online",
            sandbox_provider: null,
          },
          {
            host_id: "host_legacy",
            name: "Old server host",
            owner: "alice",
            status: "offline",
          },
        ],
      }),
    );

    const { result } = renderHook(() => useHosts(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The modal-backed host is dropped; the explicit-null and
    // field-absent hosts both survive. If host_sandbox appears, the
    // sandbox filter regressed; if host_legacy disappears, the filter
    // broke old-server compatibility.
    expect(result.current.data?.map((h) => h.host_id)).toEqual(["host_laptop", "host_legacy"]);
  });

  it("retains sandbox hosts when includeSandbox is set", async () => {
    // The chat-header HostBadge needs sandbox-backed hosts so it can
    // label them ("Databricks Sandbox"); the default filtered call must
    // stay sandbox-free for the pickers.
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        hosts: [
          {
            host_id: "host_sandbox",
            name: "managed-abc123",
            owner: "alice",
            status: "online",
            sandbox_provider: "lakebox",
          },
          {
            host_id: "host_laptop",
            name: "Laptop",
            owner: "alice",
            status: "online",
            sandbox_provider: null,
          },
        ],
      }),
    );

    const { result } = renderHook(() => useHosts({ includeSandbox: true }), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.map((h) => h.host_id)).toEqual(["host_sandbox", "host_laptop"]);
  });

  it("keeps the filtered and unfiltered lists in separate cache entries", async () => {
    // The filtered (picker) and unfiltered (badge) calls share an
    // endpoint but use distinct query keys. If they ever collapsed back
    // to a bare ["hosts"] key, the second queryFn's result would clobber
    // the first in cache and both consumers would see the same list.
    // Mounting both in ONE QueryClient and asserting they diverge guards
    // that regression. mockResolvedValue (persistent) feeds both fetches.
    fetchMock.mockResolvedValue(
      mockResponse({
        hosts: [
          {
            host_id: "host_sandbox",
            name: "managed-abc123",
            owner: "alice",
            status: "online",
            sandbox_provider: "lakebox",
          },
          {
            host_id: "host_laptop",
            name: "Laptop",
            owner: "alice",
            status: "online",
            sandbox_provider: null,
          },
        ],
      }),
    );

    const { result } = renderHook(
      () => ({
        filtered: useHosts(),
        all: useHosts({ includeSandbox: true }),
      }),
      { wrapper },
    );
    await waitFor(() => {
      expect(result.current.filtered.isSuccess).toBe(true);
      expect(result.current.all.isSuccess).toBe(true);
    });

    expect(result.current.filtered.data?.map((h) => h.host_id)).toEqual(["host_laptop"]);
    expect(result.current.all.data?.map((h) => h.host_id)).toEqual(["host_sandbox", "host_laptop"]);
  });

  it("refetches on window focus so a missed readiness push recovers on tab return", async () => {
    // The common `omni setup` flow is: open the setup dialog, alt-tab to a
    // terminal to run the command, then tab back. If the hosts_changed WS push
    // is missed (reconnect gap, or the interval poll paused while the tab was
    // hidden), the badge would sit on a stale "needs setup" until a manual
    // reload. With `refetchOnFocus: true` (the setup flow's opt-in) useHosts
    // overrides the app-wide `refetchOnWindowFocus: false` so returning to the
    // tab refetches and the badge clears. The wrapper mirrors that global
    // default to prove the per-query override is what refires.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
    });
    function focusWrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    }
    const hostsBody = {
      hosts: [
        {
          host_id: "host_1",
          name: "Laptop",
          owner: "alice",
          status: "online",
          sandbox_provider: null,
        },
      ],
    };
    fetchMock.mockResolvedValue(mockResponse(hostsBody));

    const { result } = renderHook(() => useHosts({ refetchOnFocus: true }), {
      wrapper: focusWrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Drive React Query's focusManager directly — the same signal the browser
    // visibilitychange/focus listener feeds it on a real tab return (jsdom's
    // visibilityState is fixed, so we can't rely on dispatching the DOM event).
    // staleTime 0 means it refires rather than serving cache.
    focusManager.setFocused(false);
    focusManager.setFocused(true);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    focusManager.setFocused(undefined);
  });

  it("does NOT refetch on focus by default so non-setup consumers aren't taxed", async () => {
    // The aggressive refocus refetch is opt-in (refetchOnFocus). The ~8 other
    // useHosts consumers (Sidebar, HostBadge, …) keep the 30 s stale window, so
    // a tab refocus must not refire their /v1/hosts query.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
    });
    function focusWrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    }
    fetchMock.mockResolvedValue(mockResponse({ hosts: [] }));

    const { result } = renderHook(() => useHosts(), { wrapper: focusWrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    focusManager.setFocused(false);
    focusManager.setFocused(true);
    // Give any (unwanted) refetch a chance to fire, then assert it didn't.
    await new Promise((r) => setTimeout(r, 50));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    focusManager.setFocused(undefined);
  });

  it("surfaces an error when the request fails", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ detail: "nope" }, 503));

    const { result } = renderHook(() => useHosts(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(Error);
    expect((result.current.error as Error).message).toContain("503");
  });
});

describe("useInstallHarness + useInstallingHarnesses (concurrent installs)", () => {
  // Reproduces the "switch harness mid-install" bug: the setup dialog is one
  // persistent instance sharing a single install mutation observer. That
  // observer only remembers the LATEST mutate() call's per-call callbacks, so
  // when a user installs Codex, switches to Pi, and installs Pi before Codex
  // finishes, Codex's per-call onSettled/onSuccess are overwritten — leaving
  // Codex stuck on "Installing…" with an unpatched badge. The in-flight set and
  // the cache patch must therefore NOT ride on per-call callbacks.
  function deferred<T>() {
    let resolve!: (v: T) => void;
    const promise = new Promise<T>((r) => (resolve = r));
    return { promise, resolve };
  }

  const HOST = "host_1";
  const installResult = (harness: string, readiness: Record<string, boolean | string>) =>
    mockResponse({ object: "harness_install", harness, configured_harnesses: readiness });

  it("keeps the first install's in-flight state and cache patch when a second starts", async () => {
    // Both hooks share ONE QueryClient — exactly the dialog's single instance.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    function sharedWrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    }
    // Seed the hosts cache so the readiness patch has something to write into.
    client.setQueryData(
      ["hosts", { includeSandbox: false }],
      [{ host_id: HOST, name: "Laptop", owner: "alice", status: "online" }],
    );

    const { result } = renderHook(
      () => ({
        install: useInstallHarness(HOST),
        installing: useInstallingHarnesses(HOST),
      }),
      { wrapper: sharedWrapper },
    );

    // Fire Codex's install and hold it in flight.
    const codex = deferred<Response>();
    fetchMock.mockReturnValueOnce(codex.promise);
    result.current.install.mutate("codex-native");
    await waitFor(() => expect(result.current.installing.has("codex-native")).toBe(true));

    // Now fire Pi's install (same observer) BEFORE Codex resolves.
    const pi = deferred<Response>();
    fetchMock.mockReturnValueOnce(pi.promise);
    result.current.install.mutate("pi-native");
    await waitFor(() => expect(result.current.installing.has("pi-native")).toBe(true));
    // The bug: starting Pi must NOT drop Codex from the in-flight set.
    expect(result.current.installing.has("codex-native")).toBe(true);

    // Resolve Codex: its config-level onSuccess must still patch the badge, and
    // it must leave the in-flight set even though Pi's mutate() fired last.
    // The server returns the FULL refreshed readiness map on every install (see
    // the /install route), so Codex's response already reflects Pi still
    // installing (binary-missing).
    codex.resolve(
      installResult("codex-native", {
        "codex-native": "needs-auth",
        "pi-native": "binary-missing",
      }),
    );
    await waitFor(() => expect(result.current.installing.has("codex-native")).toBe(false));
    expect(result.current.installing.has("pi-native")).toBe(true); // Pi still going
    const hosts = client.getQueryData<Array<{ configured_harnesses?: Record<string, unknown> }>>([
      "hosts",
      { includeSandbox: false },
    ]);
    expect(hosts?.[0].configured_harnesses?.["codex-native"]).toBe("needs-auth");

    // Resolve Pi: it clears too and its response (again the full map) reflects
    // both harnesses' final readiness.
    pi.resolve(installResult("pi-native", { "codex-native": "needs-auth", "pi-native": true }));
    await waitFor(() => expect(result.current.installing.has("pi-native")).toBe(false));
    const hosts2 = client.getQueryData<Array<{ configured_harnesses?: Record<string, unknown> }>>([
      "hosts",
      { includeSandbox: false },
    ]);
    expect(hosts2?.[0].configured_harnesses?.["pi-native"]).toBe(true);
    // Codex's readiness is still present (the server's full map carries it).
    expect(hosts2?.[0].configured_harnesses?.["codex-native"]).toBe("needs-auth");
  });
});

describe("useHostModelOptions", () => {
  it("loads the selected host's pre-launch catalog", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        models: [
          {
            id: "sonnet",
            model: "system.ai.claude-sonnet-4-6[1m]",
            displayName: "Sonnet 4.6",
          },
        ],
      }),
    );

    const { result } = renderHook(() => useHostModelOptions("host_1", "claude-native"), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/hosts/host_1/harnesses/claude-native/model-options",
    );
    expect(result.current.data?.map((model) => model.displayName)).toEqual(["Sonnet 4.6"]);
  });

  it("does not fetch without a selected host", async () => {
    renderHook(() => useHostModelOptions(null, "claude-native"), { wrapper });
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("useStoreCredential", () => {
  const credResult = (harness: string, readiness: Record<string, boolean | string>) =>
    mockResponse({ object: "harness_credential", harness, configured_harnesses: readiness });

  it("POSTs the harness in the path and the rest of the input as the body (no secret in the URL)", async () => {
    fetchMock.mockResolvedValueOnce(credResult("codex-native", { "codex-native": true }));
    const { result } = renderHook(() => useStoreCredential("host_1"), { wrapper });

    result.current.mutate({ harness: "codex-native", kind: "key", secret: "sk-secret" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/hosts/host_1/harnesses/codex-native/credential");
    expect(init.method).toBe("POST");
    // The secret rides in the body, never the URL, and `harness` is stripped
    // from the body (it's already in the path).
    expect(url).not.toContain("sk-secret");
    expect(JSON.parse(init.body)).toEqual({ kind: "key", secret: "sk-secret" });
  });

  it("surfaces the server's JSON `detail` on a failed write", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ detail: "a gateway base_url must start with http:// or https://" }, 400),
    );
    const { result } = renderHook(() => useStoreCredential("host_1"), { wrapper });

    result.current.mutate({ harness: "codex-native", kind: "gateway", base_url: "ftp://nope" });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe(
      "a gateway base_url must start with http:// or https://",
    );
  });

  it("falls back to the status line when the error body isn't JSON", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
      json: async () => {
        throw new Error("not json");
      },
    } as unknown as Response);
    const { result } = renderHook(() => useStoreCredential("host_1"), { wrapper });

    result.current.mutate({ harness: "codex-native", kind: "key", secret: "sk" });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("502 Bad Gateway");
  });

  it("patches the hosts cache and invalidates the detect query on success", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    function sharedWrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    }
    client.setQueryData(
      ["hosts", { includeSandbox: false }],
      [{ host_id: "host_1", name: "Laptop", owner: "alice", status: "online" }],
    );
    const invalidate = vi.spyOn(client, "invalidateQueries");

    const { result } = renderHook(() => useStoreCredential("host_1"), { wrapper: sharedWrapper });
    fetchMock.mockResolvedValueOnce(credResult("codex-native", { "codex-native": true }));
    result.current.mutate({ harness: "codex-native", kind: "key", secret: "sk" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const hosts = client.getQueryData<Array<{ configured_harnesses?: Record<string, unknown> }>>([
      "hosts",
      { includeSandbox: false },
    ]);
    expect(hosts?.[0].configured_harnesses?.["codex-native"]).toBe(true);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["detected-credentials", "host_1"] });
  });
});

describe("useDetectedCredentials", () => {
  it("GETs the detect endpoint and returns the credentials array", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        credentials: [{ family: "openai", source: "$OPENAI_API_KEY", env_var: "OPENAI_API_KEY" }],
      }),
    );
    const { result } = renderHook(() => useDetectedCredentials("host_1", true), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(fetchMock.mock.calls[0][0]).toBe("/v1/hosts/host_1/credentials/detected");
    expect(result.current.data).toEqual([
      { family: "openai", source: "$OPENAI_API_KEY", env_var: "OPENAI_API_KEY" },
    ]);
  });

  it("degrades to an empty list when the body omits `credentials`", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}));
    const { result } = renderHook(() => useDetectedCredentials("host_1", true), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([]);
  });

  it("does not fetch when disabled or without a host id", async () => {
    const disabled = renderHook(() => useDetectedCredentials("host_1", false), { wrapper });
    const noHost = renderHook(() => useDetectedCredentials(null, true), { wrapper });
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();
    disabled.unmount();
    noHost.unmount();
  });
});
