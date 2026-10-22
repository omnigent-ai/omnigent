import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { useSandboxModelOptions, useSandboxUnsupportedAgents } from "./useSandboxModelOptions";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
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
