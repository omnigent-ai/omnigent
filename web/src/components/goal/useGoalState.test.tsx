import type * as GoalApiModule from "@/lib/goalApi";

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getGoal, type Goal } from "@/lib/goalApi";
import * as identity from "@/lib/identity";
import { useGoalState } from "./useGoalState";

vi.mock("@/lib/goalApi", async (importOriginal) => {
  const actual = await importOriginal<typeof GoalApiModule>();
  return { ...actual, getGoal: vi.fn() };
});

const mockGetGoal = vi.mocked(getGoal);

const GOAL: Goal = {
  objective: "Ship goal mode",
  status: "active",
  tokenBudget: 40000,
  tokensUsed: 1200,
  timeUsedSeconds: 125,
  createdAt: null,
  updatedAt: null,
};

function deferredGoalRead() {
  let resolve!: (response: { goal: Goal | null }) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<{ goal: Goal | null }>((resolveRead, rejectRead) => {
    resolve = resolveRead;
    reject = rejectRead;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  mockGetGoal.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useGoalState", () => {
  it("reflects goals created and paused outside the dialog", async () => {
    vi.useFakeTimers();
    mockGetGoal
      .mockResolvedValueOnce({ goal: null })
      .mockResolvedValueOnce({ goal: GOAL })
      .mockResolvedValueOnce({ goal: { ...GOAL, status: "paused" } });
    const { result } = renderHook(() => useGoalState("conv", true));
    await act(async () => {});
    expect(result.current.goal).toBeNull();

    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(result.current.goal).toEqual(GOAL);
    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(result.current.goal?.status).toBe("paused");
  });

  it.each(["initial read", "dialog refresh"])(
    "keeps an explicit update when an older %s finishes",
    async (source) => {
      const read = deferredGoalRead();
      if (source === "dialog refresh") mockGetGoal.mockResolvedValueOnce({ goal: GOAL });
      mockGetGoal.mockReturnValueOnce(read.promise);
      const { result } = renderHook(() => useGoalState("conv", true));
      let refresh: Promise<void> | undefined;
      if (source === "dialog refresh") {
        await act(async () => {});
        act(() => {
          refresh = result.current.refreshGoal();
        });
      }
      const pausedGoal = { ...GOAL, status: "paused" as const };
      act(() => result.current.setGoal(pausedGoal));

      await act(async () => {
        read.resolve({ goal: null });
        await refresh;
      });

      expect(result.current.goal).toEqual(pausedGoal);
    },
  );

  it.each([
    { newest: "dialog refresh", sameBatch: false },
    { newest: "dialog refresh", sameBatch: true },
    { newest: "poll", sameBatch: false },
    { newest: "poll", sameBatch: true },
  ])(
    "keeps the latest-started $newest when sameBatch=$sameBatch",
    async ({ newest, sameBatch }) => {
      vi.useFakeTimers();
      const older = deferredGoalRead();
      const newer = deferredGoalRead();
      mockGetGoal
        .mockResolvedValueOnce({ goal: GOAL })
        .mockReturnValueOnce(older.promise)
        .mockReturnValueOnce(newer.promise);
      const { result } = renderHook(() => useGoalState("conv", true));
      await act(async () => {});

      let dialogRefresh!: Promise<void>;
      if (newest === "poll") {
        act(() => {
          dialogRefresh = result.current.refreshGoal();
        });
      }
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      if (newest === "dialog refresh") {
        act(() => {
          dialogRefresh = result.current.refreshGoal();
        });
      }
      expect(mockGetGoal).toHaveBeenCalledTimes(3);
      const latestGoal = newest === "poll" ? null : { ...GOAL, status: "paused" as const };
      const olderGoal = newest === "poll" ? GOAL : null;

      if (sameBatch) {
        await act(async () => {
          newer.resolve({ goal: latestGoal });
          older.resolve({ goal: olderGoal });
          await dialogRefresh;
        });
      } else {
        await act(async () => newer.resolve({ goal: latestGoal }));
        expect(result.current.goal).toEqual(latestGoal);
        await act(async () => {
          older.resolve({ goal: olderGoal });
          await dialogRefresh;
        });
      }

      expect(result.current.goal).toEqual(latestGoal);
    },
  );

  it.each(["dialog refresh", "poll"])(
    "propagates an explicit read error after a newer %s fails",
    async (newest) => {
      const older = deferredGoalRead();
      const newer = deferredGoalRead();
      mockGetGoal
        .mockResolvedValueOnce({ goal: GOAL })
        .mockReturnValueOnce(older.promise)
        .mockReturnValueOnce(newer.promise);
      const { result } = renderHook(() => useGoalState("conv", true));
      await act(async () => {});
      const olderRefresh = result.current.refreshGoal();
      let newerRefresh: Promise<void> | undefined;
      if (newest === "poll") {
        act(() => {
          window.dispatchEvent(new Event("focus"));
        });
      } else {
        newerRefresh = result.current.refreshGoal();
      }
      expect(mockGetGoal).toHaveBeenCalledTimes(3);

      const currentError = new Error("current read failed");
      await act(async () => {
        newer.reject(currentError);
        if (newerRefresh) await expect(newerRefresh).rejects.toBe(currentError);
      });
      const openingError = new Error("opening read failed");
      await act(async () => {
        older.reject(openingError);
        await expect(olderRefresh).rejects.toBe(openingError);
      });

      expect(result.current.goal).toEqual(GOAL);
    },
  );

  it.each([
    { newest: "dialog refresh", ordering: "before the response" },
    { newest: "dialog refresh", ordering: "after the response" },
    { newest: "poll", ordering: "before the response" },
    { newest: "poll", ordering: "after the response" },
  ])(
    "publishes a successful read when a newer $newest starts $ordering",
    async ({ newest, ordering }) => {
      vi.useFakeTimers();
      const accepted = deferredGoalRead();
      const pending = deferredGoalRead();
      mockGetGoal
        .mockResolvedValueOnce({ goal: null })
        .mockReturnValueOnce(accepted.promise)
        .mockReturnValueOnce(pending.promise);
      const { result, unmount } = renderHook(() => useGoalState("conv", true));
      await act(async () => {});
      const paused = { ...GOAL, status: "paused" as const };
      let pendingRefresh: Promise<void> | undefined;
      const startNewer = () => {
        if (newest === "poll") window.dispatchEvent(new Event("focus"));
        else pendingRefresh = result.current.refreshGoal();
      };

      await act(async () => {
        result.current.setGoal(null);
        const acceptedRefresh = result.current.refreshGoal();
        if (ordering === "before the response") startNewer();
        accepted.resolve({ goal: paused });
        await acceptedRefresh;
        if (ordering === "after the response") startNewer();
      });

      expect(mockGetGoal).toHaveBeenCalledTimes(3);
      expect(result.current.goal).toEqual(paused);
      await act(async () => {
        pending.reject(new Error("newer read failed"));
        if (pendingRefresh) await expect(pendingRefresh).rejects.toThrow("newer read failed");
      });
      expect(result.current.goal).toEqual(paused);
      unmount();
    },
  );

  it.each(["dialog refresh", "poll", "dialog update"])(
    "ignores an old explicit read error after a newer %s succeeds",
    async (newest) => {
      const older = deferredGoalRead();
      mockGetGoal
        .mockResolvedValueOnce({ goal: GOAL })
        .mockReturnValueOnce(older.promise)
        .mockResolvedValueOnce({ goal: null });
      const { result } = renderHook(() => useGoalState("conv", true));
      await act(async () => {});
      const olderRefresh = result.current.refreshGoal();

      await act(async () => {
        if (newest === "poll") window.dispatchEvent(new Event("focus"));
        else if (newest === "dialog update") result.current.setGoal(null);
        else await result.current.refreshGoal();
      });
      expect(result.current.goal).toBeNull();

      await act(async () => {
        older.reject(new Error("obsolete read failed"));
        await expect(olderRefresh).resolves.toBeUndefined();
      });

      expect(result.current.goal).toBeNull();
    },
  );

  it("reports an unavailable session when explicitly refreshed while disconnected", async () => {
    const { result } = renderHook(() => useGoalState("conv", false));

    await expect(result.current.refreshGoal()).rejects.toThrow("Goal is unavailable");

    expect(mockGetGoal).not.toHaveBeenCalled();
    expect(result.current.goal).toBeNull();
  });

  it("does not overlap reads or poll after disabling", async () => {
    vi.useFakeTimers();
    let resolveRead!: (response: { goal: Goal | null }) => void;
    mockGetGoal.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveRead = resolve;
      }),
    );
    const { result, rerender } = renderHook(({ enabled }) => useGoalState("conv", enabled), {
      initialProps: { enabled: true },
    });
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(mockGetGoal).toHaveBeenCalledTimes(1);

    rerender({ enabled: false });
    await act(async () => resolveRead({ goal: GOAL }));
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(mockGetGoal).toHaveBeenCalledTimes(1);
    expect(result.current.goal).toBeNull();
  });

  it("clears a legacy deleted goal after retaining it through a real API read failure", async () => {
    vi.useFakeTimers();
    const actualGoalApi = await vi.importActual<typeof GoalApiModule>("@/lib/goalApi");
    mockGetGoal.mockImplementation(actualGoalApi.getGoal);
    const fetch = vi
      .spyOn(identity, "authenticatedFetch")
      .mockResolvedValueOnce(
        Response.json({
          goal: {
            thread_id: "thread-1",
            objective: GOAL.objective,
            status: GOAL.status,
            token_budget: GOAL.tokenBudget,
            tokens_used: GOAL.tokensUsed,
            time_used_seconds: GOAL.timeUsedSeconds,
          },
        }),
      )
      .mockResolvedValueOnce(
        Response.json(
          { error: "codex_native_goal_failed", detail: "runner unavailable" },
          { status: 503 },
        ),
      )
      .mockResolvedValueOnce(Response.json({ goal: null }));
    const { result, unmount } = renderHook(() => useGoalState("conv", true));
    try {
      await act(async () => {});
      const initialGoal = result.current.goal;
      expect(initialGoal).toEqual({ ...GOAL, threadId: "thread-1" });
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      expect(result.current.goal).toEqual(initialGoal);
      expect(fetch).toHaveBeenCalledTimes(2);
      await act(() => vi.advanceTimersByTimeAsync(10_000));
      expect(result.current.goal).toBeNull();
      expect(fetch).toHaveBeenCalledTimes(3);
      expect(fetch).toHaveBeenLastCalledWith("/v1/sessions/conv/codex_goal", {
        headers: { "X-Omnigent-Codex-Goal-Read": "strict" },
      });
    } finally {
      unmount();
      fetch.mockRestore();
    }
  });

  it("backs off repeated failures through a repeated sixty-second cap", async () => {
    vi.useFakeTimers();
    mockGetGoal.mockResolvedValueOnce({ goal: GOAL }).mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => useGoalState("conv", true));
    await act(async () => {});
    expect(mockGetGoal).toHaveBeenCalledTimes(1);

    // Each timer boundary must settle before the next clock advance.
    /* oxlint-disable no-await-in-loop */
    for (const [index, delay] of [5_000, 10_000, 20_000, 40_000, 60_000, 60_000].entries()) {
      await act(() => vi.advanceTimersByTimeAsync(delay - 1));
      expect(mockGetGoal).toHaveBeenCalledTimes(index + 1);
      expect(result.current.goal).toEqual(GOAL);
      await act(() => vi.advanceTimersByTimeAsync(1));
      expect(mockGetGoal).toHaveBeenCalledTimes(index + 2);
      expect(result.current.goal).toEqual(GOAL);
    }
    /* oxlint-enable no-await-in-loop */
  });

  it("resets polling to five seconds after recovering from the maximum backoff", async () => {
    vi.useFakeTimers();
    mockGetGoal.mockResolvedValueOnce({ goal: GOAL }).mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => useGoalState("conv", true));
    await act(async () => {});
    await act(() => vi.advanceTimersByTimeAsync(195_000));
    expect(mockGetGoal).toHaveBeenCalledTimes(7);
    const pausedGoal = { ...GOAL, status: "paused" as const };
    mockGetGoal.mockResolvedValue({ goal: pausedGoal });
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(result.current.goal).toEqual(pausedGoal);
    expect(mockGetGoal).toHaveBeenCalledTimes(8);
    await act(() => vi.advanceTimersByTimeAsync(4_999));
    expect(mockGetGoal).toHaveBeenCalledTimes(8);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(mockGetGoal).toHaveBeenCalledTimes(9);
  });

  it("retries on focus and visibility during backoff without overlapping a pending read", async () => {
    vi.useFakeTimers();
    const recovery = deferredGoalRead();
    mockGetGoal
      .mockRejectedValueOnce(new Error("offline"))
      .mockRejectedValueOnce(new Error("still offline"))
      .mockReturnValueOnce(recovery.promise)
      .mockResolvedValue({ goal: GOAL });
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    try {
      const { result } = renderHook(() => useGoalState("conv", true));
      await act(async () => {});
      await act(() => vi.advanceTimersByTimeAsync(1_000));
      await act(async () => window.dispatchEvent(new Event("focus")));
      expect(mockGetGoal).toHaveBeenCalledTimes(2);

      visibility.mockReturnValue("hidden");
      await act(async () => document.dispatchEvent(new Event("visibilitychange")));
      await act(() => vi.advanceTimersByTimeAsync(20_000));
      expect(mockGetGoal).toHaveBeenCalledTimes(2);
      visibility.mockReturnValue("visible");
      await act(async () => document.dispatchEvent(new Event("visibilitychange")));
      await act(async () => window.dispatchEvent(new Event("focus")));
      await act(() => vi.advanceTimersByTimeAsync(60_000));
      expect(mockGetGoal).toHaveBeenCalledTimes(3);

      await act(async () => recovery.resolve({ goal: GOAL }));
      expect(result.current.goal).toEqual(GOAL);
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      expect(mockGetGoal).toHaveBeenCalledTimes(4);
    } finally {
      visibility.mockRestore();
    }
  });

  it("ignores a previous session's reads and dialog updates", async () => {
    let resolveRead!: (response: { goal: Goal | null }) => void;
    mockGetGoal
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveRead = resolve;
        }),
      )
      .mockResolvedValueOnce({ goal: null })
      .mockResolvedValueOnce({ goal: null });
    const { result, rerender } = renderHook(({ id }) => useGoalState(id, true), {
      initialProps: { id: "first" },
    });
    const updatePreviousGoal = result.current.setGoal;
    rerender({ id: "second" });
    await act(async () => {});
    rerender({ id: "first" });
    await act(async () => {});
    await act(async () => resolveRead({ goal: GOAL }));
    act(() => updatePreviousGoal(GOAL));

    expect(result.current.goal).toBeNull();
  });

  it("refreshes on focus and skips hidden tabs", async () => {
    vi.useFakeTimers();
    mockGetGoal.mockResolvedValueOnce({ goal: null }).mockResolvedValueOnce({ goal: GOAL });
    const visibility = vi.spyOn(document, "visibilityState", "get");
    visibility.mockReturnValue("hidden");
    try {
      const { result } = renderHook(() => useGoalState("conv", true));
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      expect(mockGetGoal).not.toHaveBeenCalled();

      visibility.mockReturnValue("visible");
      await act(async () => document.dispatchEvent(new Event("visibilitychange")));
      expect(mockGetGoal).toHaveBeenCalledTimes(1);
      await act(async () => window.dispatchEvent(new Event("focus")));
      expect(result.current.goal).toEqual(GOAL);
    } finally {
      visibility.mockRestore();
    }
  });

  it("loads the goal when enabled for a conversation", async () => {
    mockGetGoal.mockResolvedValueOnce({ goal: GOAL });

    const { result } = renderHook(() => useGoalState("conv", true));

    await waitFor(() => expect(result.current.goal).toEqual(GOAL));
    expect(mockGetGoal).toHaveBeenCalledWith("conv");
  });

  it("starts empty on read errors and clears state when disabled", async () => {
    mockGetGoal.mockRejectedValueOnce(new Error("offline"));

    const { result, rerender } = renderHook(({ enabled }) => useGoalState("conv", enabled), {
      initialProps: { enabled: true },
    });
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalled());
    await waitFor(() => expect(result.current.goal).toBeNull());
    act(() => result.current.setGoal(GOAL));
    expect(result.current.goal).toEqual(GOAL);

    rerender({ enabled: false });
    expect(result.current.goal).toBeNull();
  });

  it("reloads the goal when a session becomes reachable again", async () => {
    mockGetGoal.mockResolvedValueOnce({ goal: GOAL });

    const { result, rerender } = renderHook(({ reachable }) => useGoalState("conv", reachable), {
      initialProps: { reachable: false },
    });
    expect(mockGetGoal).not.toHaveBeenCalled();

    rerender({ reachable: true });

    await waitFor(() => expect(result.current.goal).toEqual(GOAL));
    expect(mockGetGoal).toHaveBeenCalledTimes(1);
    expect(mockGetGoal).toHaveBeenCalledWith("conv");
  });

  it("does not resurrect an old goal when the reconnect read fails", async () => {
    mockGetGoal.mockResolvedValueOnce({ goal: GOAL }).mockRejectedValueOnce(new Error("offline"));
    const { result, rerender } = renderHook(({ reachable }) => useGoalState("conv", reachable), {
      initialProps: { reachable: true },
    });
    await waitFor(() => expect(result.current.goal).toEqual(GOAL));

    rerender({ reachable: false });
    rerender({ reachable: true });
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(2));

    expect(result.current.goal).toBeNull();
  });

  it.each(["success", "failure"])(
    "ignores a pending dialog read's %s after the same session reconnects",
    async (outcome) => {
      const oldRead = deferredGoalRead();
      mockGetGoal
        .mockResolvedValueOnce({ goal: GOAL })
        .mockReturnValueOnce(oldRead.promise)
        .mockResolvedValueOnce({ goal: null });
      const { result, rerender } = renderHook(({ enabled }) => useGoalState("conv", enabled), {
        initialProps: { enabled: true },
      });
      await act(async () => {});
      const oldSetter = result.current.setGoal;
      const oldRefresh = result.current.refreshGoal();

      rerender({ enabled: false });
      expect(result.current.goal).toBeNull();
      rerender({ enabled: true });
      await act(async () => {});
      expect(mockGetGoal).toHaveBeenCalledTimes(3);

      await act(async () => {
        if (outcome === "success") oldRead.resolve({ goal: GOAL });
        else oldRead.reject(new Error("disconnected read failed"));
        await expect(oldRefresh).resolves.toBeUndefined();
      });
      expect(result.current.goal).toBeNull();
      act(() => oldSetter(GOAL));
      expect(result.current.goal).toBeNull();
    },
  );
});
