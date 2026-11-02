import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import type { SandboxStatus } from "@/lib/types";
import {
  sandboxModelOptionsKey,
  useSandboxModelOptions,
  useSandboxLaunchModelOptions,
  useSandboxUnsupportedAgents,
} from "./useSandboxModelOptions";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn(), getCurrentUserId: () => "alice" }));
const fetchMock = vi.mocked(authenticatedFetch);

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

const catalog = {
  configured: true,
  status: "ready",
  models: [{ id: "gateway/model-a", isDefault: true }],
  configuration_revision: "revision-1",
  provider_label: "Bifrost",
  default_model: "gateway/model-a",
};

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
});

describe("useSandboxModelOptions", () => {
  it("previews the exact ACP identity without starting a host", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify(catalog)));
    const { result } = renderHook(
      () => useSandboxModelOptions("agent_sandbox", "acp:private", "ag_1", "alice", true),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.data).toEqual(catalog));
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      "/v1/sandbox-providers/agent_sandbox/harnesses/acp%3Aprivate/model-options?agent_id=ag_1",
    );
  });

  it("does not reuse another target or owner's cached catalog", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify(catalog)));
    const { result, rerender } = renderHook(
      ({ provider, user }) => useSandboxModelOptions(provider, "claude-native", "ag_1", user, true),
      { initialProps: { provider: "kubernetes", user: "alice" }, wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    fetchMock.mockImplementation(() => new Promise(() => {}));
    rerender({ provider: "agent_sandbox", user: "alice" });
    expect(result.current.data).toBeUndefined();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    rerender({ provider: "kubernetes", user: "bob" });
    expect(result.current.data).toBeUndefined();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("keeps successful empty discovery distinct from request failure", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ ...catalog, models: [], status: "empty" })),
    );
    const { result, rerender } = renderHook(
      ({ harness }) => useSandboxModelOptions("kubernetes", harness, "ag_1", "alice", true),
      { initialProps: { harness: "claude-native" }, wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.data?.status).toBe("empty"));
    expect(result.current.error).toBeNull();
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Catalog credentials unavailable" }), { status: 503 }),
    );
    rerender({ harness: "codex-native" });
    await waitFor(() =>
      expect(result.current.error?.message).toBe("Catalog credentials unavailable"),
    );
    expect(result.current.data).toBeUndefined();
  });

  it("does not query for connected-host targets", async () => {
    renderHook(
      () => useSandboxModelOptions("kubernetes", "claude-native", "ag_1", "alice", false),
      { wrapper: wrapper() },
    );
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("useSandboxUnsupportedAgents", () => {
  const entries = [
    { agentId: "ag_codex", harness: "codex-native" },
    { agentId: "ag_claude", harness: "claude-native" },
    { agentId: "ag_pi", harness: "pi-native" },
  ];

  function previewBy(statusFor: (harness: string) => number | string) {
    fetchMock.mockImplementation(async (url) => {
      const harness = decodeURIComponent(String(url).split("/harnesses/")[1].split("/")[0]);
      const status = statusFor(harness);
      if (typeof status === "number") {
        return new Response(JSON.stringify({ detail: "Gateway down" }), { status });
      }
      return new Response(
        JSON.stringify({
          ...catalog,
          configured: false,
          models: [],
          status,
          ...(status === "unsupported" ? { error: "No models for Codex." } : {}),
        }),
      );
    });
  }

  function render(enabled = true) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const hook = renderHook(
      () => useSandboxUnsupportedAgents("lakebox", entries, "alice", enabled),
      {
        wrapper: ({ children }: { children: ReactNode }) => (
          <QueryClientProvider client={client}>{children}</QueryClientProvider>
        ),
      },
    );
    return { client, ...hook };
  }

  it("fetches nothing until rows are prefetched", async () => {
    render();
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reports only agents whose preview is definitively unsupported", async () => {
    previewBy(
      (harness) =>
        ({ "codex-native": "unsupported", "claude-native": "unavailable", "pi-native": 503 })[
          harness
        ]!,
    );
    const { result } = render();
    act(() => result.current.prefetch(["ag_codex", "ag_claude", "ag_pi"]));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    await waitFor(() =>
      expect([...result.current.unsupported]).toEqual([["ag_codex", "No models for Codex."]]),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/sandbox-providers/lakebox/harnesses/codex-native/model-options?agent_id=ag_codex",
    );
  });

  it("reuses a fresh preview instead of refetching on the next open", async () => {
    previewBy(() => "ready");
    const { result } = render();
    act(() => result.current.prefetch(["ag_codex"]));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    act(() => result.current.prefetch(["ag_codex"]));
    await Promise.resolve();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("stops ruling an agent out once its preview fails", async () => {
    previewBy(() => "unsupported");
    const { client, result } = render();
    act(() => result.current.prefetch(["ag_codex"]));
    await waitFor(() => expect(result.current.unsupported.has("ag_codex")).toBe(true));
    previewBy(() => 503);
    // The next open refetches the now-stale entry through the real request.
    await act(() => client.invalidateQueries({ refetchType: "none" }));
    act(() => result.current.prefetch(["ag_codex"]));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.unsupported.size).toBe(0));
  });

  it("does nothing while disabled", async () => {
    const { result } = render(false);
    act(() => result.current.prefetch(["ag_codex"]));
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.unsupported.size).toBe(0);
  });
});

describe("useSandboxLaunchModelOptions", () => {
  const previewRows = [
    { id: "gateway/opus", displayName: "Opus", isDefault: false },
    { id: "gateway/gpt", displayName: "GPT", isDefault: false },
  ];
  const ready = {
    ...catalog,
    configured: false,
    models: previewRows,
    configuration_revision: null,
    default_model: null,
  };
  const managedInfo: ServerInfo = {
    ...FALLBACK_SERVER_INFO,
    managed_sandboxes_enabled: true,
    sandbox_provider: "agent_sandbox",
    sandbox_providers: ["agent_sandbox", "lakebox"],
    sandbox_provider_capabilities: {
      agent_sandbox: { default_models: true },
      lakebox: { default_models: true },
    },
  };
  const provisioning: SandboxStatus = { stage: "provisioning", provider: "agent_sandbox" };

  /** Serve every provider preview; other URLs fail loudly. */
  function serve(preview: object = ready) {
    fetchMock.mockImplementation(async (url) =>
      String(url).startsWith("/v1/sandbox-providers/")
        ? new Response(JSON.stringify(preview))
        : new Response(JSON.stringify({ detail: `unexpected ${String(url)}` }), { status: 500 }),
    );
  }

  function render({
    launch = provisioning,
    hostId = null,
    harness = "claude-native",
    enabled = true,
    info = managedInfo,
  }: {
    launch?: SandboxStatus | null;
    hostId?: string | null;
    harness?: string;
    enabled?: boolean;
    info?: ServerInfo | "loading";
  } = {}) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const hook = renderHook(
      ({ active }: { active: boolean }) =>
        useSandboxLaunchModelOptions(launch, hostId, harness, "ag_1", active),
      {
        initialProps: { active: enabled },
        wrapper: ({ children }: { children: ReactNode }) => (
          <QueryClientProvider client={client}>
            <CapabilitiesProvider info={info}>{children}</CapabilitiesProvider>
          </QueryClientProvider>
        ),
      },
    );
    return { client, ...hook };
  }

  // Gives a wrongly enabled query time to start before asserting it didn't.
  const settle = () =>
    act(
      () =>
        new Promise<void>((resolve) => {
          setTimeout(resolve, 0);
        }),
    );

  it("previews a launching sandbox's models before it has a host", async () => {
    serve();
    const { result } = render({ harness: "codex-native" });
    await waitFor(() => expect(result.current).toEqual(previewRows));
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      "/v1/sandbox-providers/agent_sandbox/harnesses/codex-native/model-options?agent_id=ag_1",
    );
  });

  it.each(["action_required", "cloning", "starting"] as const)(
    "keeps the launch's provider through the %s stage",
    async (stage) => {
      serve();
      const { result } = render({ launch: { stage, provider: "lakebox" } });
      await waitFor(() => expect(result.current).toEqual(previewRows));
      expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
        "/v1/sandbox-providers/lakebox/harnesses/claude-native/model-options?agent_id=ag_1",
      );
    },
  );

  it.each<{ name: string; launch: SandboxStatus | null; hostId: string | null }>([
    {
      name: "a failed launch",
      launch: { stage: "failed", provider: "agent_sandbox", error: "quota exceeded" },
      hostId: null,
    },
    { name: "a launch bound to its host", launch: provisioning, hostId: "host_1" },
    {
      name: "a launch starting its runner",
      launch: { stage: "connecting", provider: "agent_sandbox" },
      hostId: null,
    },
    { name: "a session without a launch", launch: null, hostId: "host_1" },
  ])("leaves $name to the session's own sources", async ({ launch, hostId }) => {
    serve();
    const { result } = render({ launch, hostId });
    await settle();
    expect(result.current).toBeUndefined();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each<{ name: string; info: ServerInfo | "loading" }>([
    {
      name: "a provider without a default catalog",
      info: {
        ...managedInfo,
        sandbox_provider_capabilities: { agent_sandbox: { multi_repo: true } },
      },
    },
    {
      name: "a bound inference profile",
      info: {
        ...managedInfo,
        sandbox_provider_capabilities: { agent_sandbox: { inference_models: true } },
      },
    },
    {
      name: "a provider the server does not list",
      info: { ...managedInfo, sandbox_provider_capabilities: {} },
    },
    { name: "a pending server info probe", info: "loading" },
  ])("does not preview $name", async ({ info }) => {
    serve();
    const { result } = render({ info });
    await settle();
    expect(result.current).toBeUndefined();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(["empty", "unavailable", "unconfigured", "unsupported"])(
    "offers no rows for a %s preview",
    async (status) => {
      const preview = { ...ready, models: [], status };
      serve(preview);
      const { client, result } = render();
      const key = sandboxModelOptionsKey("agent_sandbox", "claude-native", "ag_1", "alice");
      await waitFor(() => expect(client.getQueryData(key)).toEqual(preview));
      expect(result.current).toBeUndefined();
    },
  );

  it("recovers a launch's rows after an unavailable preview or a failed request", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      serve({ ...ready, models: [], status: "unavailable" });
      const { result } = render();
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      fetchMock.mockResolvedValue(new Response("{}", { status: 503 }));
      await act(() => vi.advanceTimersByTimeAsync(15_000));
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
      expect(result.current).toBeUndefined();
      serve();
      await act(() => vi.advanceTimersByTimeAsync(15_000));
      await waitFor(() => expect(result.current).toEqual(previewRows));
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the last good rows when a refresh fails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      serve();
      const { result } = render();
      await waitFor(() => expect(result.current).toEqual(previewRows));
      fetchMock.mockResolvedValue(new Response("{}", { status: 503 }));
      await act(() => vi.advanceTimersByTimeAsync(15_000));
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
      await settle();
      expect(result.current).toEqual(previewRows);
    } finally {
      vi.useRealTimers();
    }
  });

  it("drops the preview once the caller disables it", async () => {
    serve();
    const { result, rerender } = render();
    await waitFor(() => expect(result.current).toEqual(previewRows));
    rerender({ active: false });
    expect(result.current).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("requests nothing while disabled", async () => {
    serve();
    const { result } = render({ enabled: false });
    await settle();
    expect(result.current).toBeUndefined();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
