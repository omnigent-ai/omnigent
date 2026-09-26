import type * as SessionsApiModule from "@/lib/sessionsApi";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  getSessionSlim: vi.fn(),
}));

import { getSessionHost } from "@/lib/sessionHost";
import { getSessionSlim } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { prefetchSessionHostChain, useSession } from "./useSession";

const getSessionSlimMock = vi.mocked(getSessionSlim);

function session(id: string): Session {
  return { id } as unknown as Session;
}

function harness() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, Wrapper };
}

/**
 * Let queued promises settle under fake timers. RTL's `waitFor` drives its own
 * timer loop, which deadlocks against vitest's fake clock here, so tests step
 * the clock explicitly instead.
 */
async function flush(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** `refresh_state` flags in call order. */
function refreshFlags(): boolean[] {
  return getSessionSlimMock.mock.calls.map((call) => call[1]?.refreshState === true);
}

beforeEach(() => {
  vi.useFakeTimers();
  getSessionSlimMock.mockReset();
  getSessionSlimMock.mockResolvedValue(session("conv_1"));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("useSession — refresh_state", () => {
  it("asks for a state refresh on the initial fetch", async () => {
    const { Wrapper } = harness();
    renderHook(() => useSession("conv_1"), { wrapper: Wrapper });
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(1);
    expect(refreshFlags()).toEqual([true]);
  });

  it("never fetches for a client-only temp id (no /v1/sessions/temp:*)", async () => {
    const { Wrapper } = harness();
    renderHook(() => useSession("temp:0a1b2c3d"), { wrapper: Wrapper });
    await flush();
    // The navigate-first invariant: a temp id has no server session, so the
    // query is disabled by construction — no request is issued.
    expect(getSessionSlimMock).not.toHaveBeenCalled();
  });

  // Switching the session's agent invalidates this query. The refetch has to
  // re-read runner-backed state too, or `model_options` comes back from the
  // runner's process cache and the picker keeps showing the PREVIOUS agent's
  // catalog until a hard reload.
  it("asks for a state refresh on the invalidation refetch too", async () => {
    const { client, Wrapper } = harness();
    renderHook(() => useSession("conv_1"), { wrapper: Wrapper });
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await client.invalidateQueries({ queryKey: ["session", "conv_1"] });
    });
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(2);
    expect(refreshFlags()).toEqual([true, true]);
  });

  it("does not poll — the snapshot is refreshed on bind and invalidation only", async () => {
    const { Wrapper } = harness();
    renderHook(() => useSession("conv_1"), { wrapper: Wrapper });
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(1);
    await flush(5 * 60_000);
    expect(getSessionSlimMock).toHaveBeenCalledTimes(1);
  });
});

describe("prefetchSessionHostChain", () => {
  /** Snapshot stub carrying only the routing fields the walk reads. */
  function routed(id: string, hostId: string | null, parentSessionId: string | null): Session {
    return { id, hostId, parentSessionId } as unknown as Session;
  }

  /** Serve snapshots by id, like the server would. */
  function serve(snapshots: Session[]): void {
    const byId = new Map(snapshots.map((s) => [s.id, s]));
    getSessionSlimMock.mockImplementation(async (id: string) => {
      const found = byId.get(id);
      if (!found) throw new Error(`no snapshot for ${id}`);
      return found;
    });
  }

  it("cold-opens a hostless child by loading its parent for the routing host", async () => {
    // Opening /c/<child> directly: nothing about the parent is cached, and the
    // child's own snapshot carries no host — only its parent id.
    const { client } = harness();
    serve([routed("cold_child", null, "cold_parent"), routed("cold_parent", "host_devbox", null)]);

    await prefetchSessionHostChain(client, "cold_child");

    expect(getSessionHost("cold_child")).toBe("host_devbox");
    expect(getSessionSlimMock.mock.calls.map((call) => call[0])).toEqual([
      "cold_child",
      "cold_parent",
    ]);
  });

  it("reuses cached snapshots instead of refetching", async () => {
    const { client } = harness();
    client.setQueryData(["session", "warm_parent"], routed("warm_parent", "host_warm", null));
    serve([routed("warm_child", null, "warm_parent")]);

    await prefetchSessionHostChain(client, "warm_child");

    expect(getSessionHost("warm_child")).toBe("host_warm");
    expect(getSessionSlimMock.mock.calls.map((call) => call[0])).toEqual(["warm_child"]);
  });

  it("stops at a hostless top-level session", async () => {
    const { client } = harness();
    serve([routed("local_top", null, null)]);

    await prefetchSessionHostChain(client, "local_top");

    expect(getSessionHost("local_top")).toBeNull();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(1);
  });

  it("terminates on a malformed parent cycle", async () => {
    const { client } = harness();
    serve([routed("cycle_a", null, "cycle_b"), routed("cycle_b", null, "cycle_a")]);

    await prefetchSessionHostChain(client, "cycle_a");

    expect(getSessionHost("cycle_a")).toBeNull();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(2);
  });
});
