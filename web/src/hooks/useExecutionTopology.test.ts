import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import {
  chooseBinding,
  fetchExecutionTopology,
  useExecutionTopology,
  type ExecutionBinding,
  type ExecutionTopology,
} from "./useExecutionTopology";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
afterEach(() => vi.clearAllMocks());
const bindings: ExecutionBinding[] = [
  {
    host_id: "first",
    machine_id: "a",
    account_id: "codex",
    harness: "codex-native",
    available: true,
    reason: null,
  },
  {
    host_id: "shared",
    machine_id: "a",
    account_id: "codex",
    harness: "codex-native",
    available: true,
    reason: null,
  },
  {
    host_id: "third",
    machine_id: "b",
    account_id: "codex",
    harness: "codex-native",
    available: true,
    reason: null,
  },
  {
    host_id: "shared",
    machine_id: "a",
    account_id: "claude",
    harness: "claude-native",
    available: true,
    reason: null,
  },
];

describe("topology binding selection", () => {
  it("keeps the exact host/harness binding before declared order", () => {
    expect(chooseBinding(bindings, "codex", "shared", "codex-native", "a")?.host_id).toBe("shared");
  });
  it("does not confuse another harness on a shared legacy host with the current binding", () => {
    expect(chooseBinding(bindings, "codex", "shared", "claude-native", "a")?.host_id).toBe("first");
  });
  it("prefers the current physical machine and then declared available order", () => {
    expect(chooseBinding(bindings, "codex", null, null, "b")?.host_id).toBe("third");
    expect(chooseBinding(bindings, "codex", null, null, null)?.host_id).toBe("first");
    expect(
      chooseBinding(
        bindings.map((b) => ({ ...b, available: b.host_id !== "first" })),
        "codex",
        null,
        null,
        "a",
      )?.host_id,
    ).toBe("shared");
  });
  it("never changes machines to find an available route for an explicit machine", () => {
    expect(chooseBinding(bindings, "codex", "shared", "codex-native", "a", "missing")).toBeNull();
    expect(chooseBinding(bindings, "missing", null, null, "a")).toBeNull();
  });
});

describe("topology loading", () => {
  it("reads only the additive topology endpoint", async () => {
    const view = { configured: false, machines: [], accounts: [], bindings: [] };
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response(JSON.stringify(view)));
    expect(await fetchExecutionTopology()).toEqual(view);
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/execution-topology");
  });
  it("treats a server without the endpoint as having no registry", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response("", { status: 404 }));
    expect(await fetchExecutionTopology()).toEqual({
      configured: false,
      machines: [],
      accounts: [],
      bindings: [],
    });
  });
  it("throws on other failures so they are retried rather than cached", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response("", { status: 503 }));
    await expect(fetchExecutionTopology()).rejects.toThrow("Couldn't load machines");
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  });
});

describe("useExecutionTopology freshness", () => {
  const view: ExecutionTopology = { configured: true, machines: [], accounts: [], bindings: [] };
  const answer = () =>
    vi
      .mocked(authenticatedFetch)
      .mockImplementation(async () => new Response(JSON.stringify(view)));

  function setup() {
    const client = new QueryClient();
    client.setQueryData(["execution-topology"], view);
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    return wrapper;
  }

  afterEach(() => vi.useRealTimers());

  it("reads the registry once by default and never polls", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    answer();
    const { result } = renderHook(() => useExecutionTopology(true), { wrapper: setup() });
    expect(result.current.data).toEqual(view);
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("when live, refetches on mount and polls while enabled", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    answer();
    renderHook(() => useExecutionTopology(true, { live: true }), { wrapper: setup() });
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    await act(() => vi.advanceTimersByTimeAsync(7_000));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(2));
    await act(() => vi.advanceTimersByTimeAsync(7_000));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(3));
  });

  it("when live, stops polling once disabled", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    answer();
    const { rerender } = renderHook(
      ({ enabled }) => useExecutionTopology(enabled, { live: true }),
      { wrapper: setup(), initialProps: { enabled: true } },
    );
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    rerender({ enabled: false });
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  });
});
