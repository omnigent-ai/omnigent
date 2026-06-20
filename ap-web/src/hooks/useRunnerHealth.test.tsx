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
  body: Record<string, { runner_online: boolean; host_online?: boolean | null }>,
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
        conv_a: { runner_online: true, host_online: true },
        conv_b: { runner_online: false, host_online: true },
      }),
    );
    const { result } = renderHook(() => useRunnerHealth([input("conv_a"), input("conv_b")]));

    await waitFor(() => expect(result.current.size).toBe(2));
    // Assert both fields, not just structure: this proves the host-aware
    // server payload made it through the poll into the exposed map.
    expect(result.current.get("conv_a")).toEqual({
      runner_online: true,
      host_online: true,
    });
    expect(result.current.get("conv_b")).toEqual({
      runner_online: false,
      host_online: true,
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
    });
  });

  it("polls nothing and stays empty when the session set is empty", async () => {
    const { result } = renderHook(() => useRunnerHealth([]));
    // Empty fallback set (nothing open) → no /health request, empty map.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.size).toBe(0);
  });
});
