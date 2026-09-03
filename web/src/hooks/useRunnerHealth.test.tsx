// `useRunnerHealth` is the socket-down FALLBACK poll for open-session
// liveness. The server's /health is the single authority (host-aware):
// every session passed in is sent to /health and reflects whatever
// runner_online + host_online the server returns. The client no longer
// infers offline from a missing runner_id; that older heuristic disagreed
// with the server and blocked resuming auto-resumable host-bound sessions
// whose runner was reaped.

import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type RunnerHealthInput, useRunnerHealth } from "./useRunnerHealth";

const fetchMock = vi.fn();

function input(id: string, runner_id: string | null = null): RunnerHealthInput {
  return { id, runner_id };
}

function mockHealth(
  body: Record<
    string,
    { runner_online: boolean; host_online?: boolean | null; host_version?: string | null }
  >,
) {
  return {
    ok: true,
    status: 200,
    json: async () => ({ sessions: body }),
  } as unknown as Response;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("useRunnerHealth", () => {
  it("polls the given sessions and reflects host-aware server liveness", async () => {
    // runner_id null no longer means offline: a session is sent to /health,
    // and a host-bound session whose runner was reaped reads
    // runner_online=false / host_online=true because the host relaunches one
    // on the next message.
    fetchMock.mockResolvedValueOnce(
      mockHealth({
        conv_a: { runner_online: true, host_online: true, host_version: "1.2.3" },
        conv_b: { runner_online: false, host_online: true },
      }),
    );
    const { result } = renderHook(() => useRunnerHealth([input("conv_a"), input("conv_b")]));

    await waitFor(() => expect(result.current.size).toBe(2));
    // Assert all fields, not just structure: this proves the host-aware
    // server payload — including the bound host's version — made it through
    // the poll into the exposed map. conv_b omits host_version, which the
    // poll normalizes to null (same as host_online).
    expect(result.current.get("conv_a")).toEqual({
      runner_online: true,
      host_online: true,
      host_version: "1.2.3",
    });
    expect(result.current.get("conv_b")).toEqual({
      runner_online: false,
      host_online: true,
      host_version: null,
    });
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(`/health?session_ids=${encodeURIComponent("conv_a,conv_b")}`);
  });

  it("reports a bound + online session as runner_online true", async () => {
    fetchMock.mockResolvedValueOnce(
      mockHealth({ conv_a: { runner_online: true, host_online: true } }),
    );
    const { result } = renderHook(() => useRunnerHealth([input("conv_a", "runner_x")]));

    await waitFor(() => expect(result.current.get("conv_a")?.runner_online).toBe(true));
  });

  it("reports a dead-runner-on-live-host as runner_online false, host_online true", async () => {
    // The server's runner_online is strict now: a reaped runner reads false
    // even though the host that would relaunch it is still up.
    fetchMock.mockResolvedValueOnce(
      mockHealth({ conv_a: { runner_online: false, host_online: true } }),
    );
    const { result } = renderHook(() => useRunnerHealth([input("conv_a", "runner_x")]));

    await waitFor(() => expect(result.current.has("conv_a")).toBe(true));
    expect(result.current.get("conv_a")).toEqual({
      runner_online: false,
      host_online: true,
      host_version: null,
    });
  });

  it("defaults host_online to null when the server omits it (not host-bound)", async () => {
    // A non-host-bound session has no host to be online; the server reports
    // host_online null (or omits it), and the poll normalizes to null.
    fetchMock.mockResolvedValueOnce(mockHealth({ conv_a: { runner_online: true } }));
    const { result } = renderHook(() => useRunnerHealth([input("conv_a")]));

    await waitFor(() => expect(result.current.has("conv_a")).toBe(true));
    expect(result.current.get("conv_a")).toEqual({
      runner_online: true,
      host_online: null,
      host_version: null,
    });
  });

  it("polls nothing and stays empty when the session set is empty", async () => {
    const { result } = renderHook(() => useRunnerHealth([]));
    // Empty fallback set (nothing open) → no /health request, empty map.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.size).toBe(0);
  });
});

// A hidden tab renders no liveness badges, so the poll has nothing to keep
// fresh. Every other poller in the app is a react-query `refetchInterval`,
// which already stands down when the window loses focus; this hand-rolled
// loop is the one that didn't.
describe("useRunnerHealth — hidden tab", () => {
  // Hoisted so `renderHook` re-renders keep the same array identity, matching
  // how RunnerHealthProvider passes its memoized `fallbackSet`. An inline
  // literal would re-fire the effect on every state update the poll causes.
  const SESSIONS: RunnerHealthInput[] = [{ id: "conv_a", runner_id: null }];

  function setHidden(hidden: boolean) {
    Object.defineProperty(document, "hidden", { value: hidden, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
  }

  afterEach(() => {
    Object.defineProperty(document, "hidden", { value: false, configurable: true });
  });

  it("skips the request entirely while the tab is hidden", async () => {
    Object.defineProperty(document, "hidden", { value: true, configurable: true });
    renderHook(() => useRunnerHealth(SESSIONS));

    // Give the effect and any microtasks a chance to fire a request.
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("polls immediately on returning to the tab, without waiting out the tick", async () => {
    Object.defineProperty(document, "hidden", { value: true, configurable: true });
    fetchMock.mockResolvedValue(mockHealth({ conv_a: { runner_online: true } }));
    const { result } = renderHook(() => useRunnerHealth(SESSIONS));

    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(fetchMock).not.toHaveBeenCalled();

    // Becoming visible must not wait out the 10s tick the hidden branch
    // scheduled — otherwise liveness reads stale right when the user looks.
    setHidden(false);
    await waitFor(() => expect(result.current.get("conv_a")?.runner_online).toBe(true));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps a single poll chain across repeated visibility flips", async () => {
    fetchMock.mockResolvedValue(mockHealth({ conv_a: { runner_online: true } }));
    const { result } = renderHook(() => useRunnerHealth(SESSIONS));
    await waitFor(() => expect(result.current.size).toBe(1));

    const before = fetchMock.mock.calls.length;

    // Each return to the tab retires the previous chain rather than adding
    // one, so N flips must not leave N timers polling in parallel.
    for (let i = 0; i < 3; i++) {
      setHidden(true);
      setHidden(false);
    }

    // Exactly one poll per wake-up, and it stays there once things settle —
    // a duplicated chain would keep climbing past this.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(before + 3));
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    expect(fetchMock).toHaveBeenCalledTimes(before + 3);
  });

  it("stops listening for visibility changes after unmount", async () => {
    fetchMock.mockResolvedValue(mockHealth({ conv_a: { runner_online: true } }));
    const { result, unmount } = renderHook(() => useRunnerHealth(SESSIONS));
    await waitFor(() => expect(result.current.size).toBe(1));
    const callsAtUnmount = fetchMock.mock.calls.length;

    unmount();
    setHidden(true);
    setHidden(false);
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(fetchMock).toHaveBeenCalledTimes(callsAtUnmount);
  });
});
