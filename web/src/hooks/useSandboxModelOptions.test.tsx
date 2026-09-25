import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { useSandboxModelOptions } from "./useSandboxModelOptions";

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
