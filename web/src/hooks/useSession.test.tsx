import type * as SessionsApiModule from "@/lib/sessionsApi";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  // ApiError stays real — the 404 branch narrows on `instanceof`.
  ...(await importOriginal<typeof SessionsApiModule>()),
  getSessionSlim: vi.fn(),
}));

import { ApiError, getSessionSlim } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { useSession } from "./useSession";

const getSessionSlimMock = vi.mocked(getSessionSlim);

const POLL_MS = 30_000;

function session(id: string): Session {
  return { id, warnings: undefined } as unknown as Session;
}

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
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
  // `refresh_state=true` drops the runner's skills / model-options caches, so a
  // poll that kept asking for it would pop them every tick (and hand back empty
  // lists while they refill). Only the cache-cold first read needs it.
  it("asks for a state refresh on the initial fetch only", async () => {
    renderHook(() => useSession("conv_1", { refetchIntervalMs: POLL_MS }), {
      wrapper: wrapper(),
    });
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(1);
    expect(refreshFlags()).toEqual([true]);

    await flush(POLL_MS + 1);
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(2);
    await flush(POLL_MS + 1);
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(3);
    expect(refreshFlags()).toEqual([true, false, false]);
  });

  it("refreshes on every fetch when the caller opts in", async () => {
    renderHook(
      () => useSession("conv_1", { refetchIntervalMs: POLL_MS, refreshStateOnEveryFetch: true }),
      { wrapper: wrapper() },
    );
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(1);
    await flush(POLL_MS + 1);
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(2);
    expect(refreshFlags()).toEqual([true, true]);
  });
});

describe("useSession — polling lifecycle", () => {
  it("stops polling after consecutive 404s (the session was deleted)", async () => {
    getSessionSlimMock.mockRejectedValue(new ApiError("not found", 404, "not_found"));
    renderHook(() => useSession("conv_gone", { refetchIntervalMs: POLL_MS }), {
      wrapper: wrapper(),
    });
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(1);

    // Second 404 confirms it isn't a read that raced the delete.
    await flush(POLL_MS + 1);
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(2);

    // No third ask, however long the tab stays open.
    await flush(POLL_MS * 10);
    expect(getSessionSlimMock).toHaveBeenCalledTimes(2);
  });

  it("keeps polling through a transient non-404 failure", async () => {
    getSessionSlimMock.mockRejectedValueOnce(new ApiError("boom", 503, "runner_unavailable"));
    getSessionSlimMock.mockRejectedValueOnce(new ApiError("boom", 503, "runner_unavailable"));
    renderHook(() => useSession("conv_1", { refetchIntervalMs: POLL_MS }), {
      wrapper: wrapper(),
    });
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(1);
    await flush(POLL_MS + 1);
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(2);
    await flush(POLL_MS + 1);
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(3);
  });

  it("does not poll at all without an interval", async () => {
    renderHook(() => useSession("conv_1"), { wrapper: wrapper() });
    await flush();
    expect(getSessionSlimMock).toHaveBeenCalledTimes(1);
    await flush(POLL_MS * 5);
    expect(getSessionSlimMock).toHaveBeenCalledTimes(1);
  });
});
