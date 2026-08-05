import type * as SessionsApiModule from "@/lib/sessionsApi";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  getSessionSlim: vi.fn(),
}));

import { getSessionSlim } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { useSession } from "./useSession";

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
