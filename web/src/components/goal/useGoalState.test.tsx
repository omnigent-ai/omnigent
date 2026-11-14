import type * as GoalApiModule from "@/lib/goalApi";

import { useEffect } from "react";
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
  it("uses idle-safe background reads and strict explicit refreshes", async () => {
    vi.useFakeTimers();
    mockGetGoal.mockResolvedValue({ goal: GOAL });
    const { result } = renderHook(() => useGoalState("conv", true));
    await act(async () => {});
    expect(mockGetGoal).toHaveBeenNthCalledWith(1, "conv", { strict: false });
    expect(result.current.goal).toEqual(GOAL);

    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(mockGetGoal).toHaveBeenNthCalledWith(2, "conv", { strict: false });
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(mockGetGoal).toHaveBeenNthCalledWith(3, "conv", { strict: false });
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    expect(mockGetGoal).toHaveBeenNthCalledWith(4, "conv", { strict: false });

    await act(() => result.current.refreshGoal());

    expect(mockGetGoal).toHaveBeenNthCalledWith(5, "conv", { strict: true });
    expect(result.current.goal).toEqual(GOAL);
  });

  it.each([GOAL, null])(
    "deduplicates equal background snapshots without losing confirmation or ordering (%j)",
    async (goal) => {
      vi.useFakeTimers();
      const olderRead = deferredGoalRead();
      const committed = vi.fn();
      mockGetGoal.mockImplementation(async () => ({ goal: goal ? { ...goal } : null }));
      const { result } = renderHook(() => {
        const state = useGoalState("conv", true);
        useEffect(committed);
        return state;
      });
      await act(async () => {});
      const initialGoal = result.current.goal;
      const initialRevision = result.current.goalRevision;
      const initialCommits = committed.mock.calls.length;

      await act(() => vi.advanceTimersByTimeAsync(5_000));
      await act(async () => window.dispatchEvent(new Event("focus")));

      expect(result.current.goal).toBe(initialGoal);
      expect(result.current.goalRevision).toBe(initialRevision);
      expect(committed).toHaveBeenCalledTimes(initialCommits);
      mockGetGoal.mockReturnValueOnce(olderRead.promise);
      const refresh = result.current.refreshGoal();

      await act(async () => window.dispatchEvent(new Event("focus")));
      const confirmedRevision = result.current.goalRevision;
      const confirmedCommits = committed.mock.calls.length;
      expect(confirmedRevision).toBeGreaterThan(initialRevision);
      await act(async () => window.dispatchEvent(new Event("focus")));
      await act(async () => {
        olderRead.resolve({ goal: goal ? null : GOAL });
        await refresh;
      });

      expect(result.current.goal).toEqual(goal);
      expect(result.current.goalRevision).toBe(confirmedRevision);
      expect(committed).toHaveBeenCalledTimes(confirmedCommits);
      const changedGoal = goal ? { ...goal, tokensUsed: goal.tokensUsed + 1 } : GOAL;
      mockGetGoal.mockResolvedValue({ goal: changedGoal });
      await act(async () => window.dispatchEvent(new Event("focus")));
      expect(result.current.goal).toEqual(changedGoal);
      expect(result.current.goalRevision).toBeGreaterThan(confirmedRevision);
      const changedRevision = result.current.goalRevision;

      await act(() => result.current.refreshGoal());

      expect(result.current.goalRevision).toBeGreaterThan(changedRevision);
    },
  );

  it("accepts an idle background null but reports an unavailable explicit read through the real API", async () => {
    vi.useFakeTimers();
    const actualGoalApi = await vi.importActual<typeof GoalApiModule>("@/lib/goalApi");
    mockGetGoal.mockImplementation(actualGoalApi.getGoal);
    const fetch = vi
      .spyOn(identity, "authenticatedFetch")
      .mockImplementation(async (_url, init) =>
        new Headers(init?.headers).get("X-Omnigent-Codex-Goal-Read") === "strict"
          ? Response.json(
              { error: "codex_native_goal_failed", detail: "runner unavailable" },
              { status: 503 },
            )
          : Response.json({ goal: null, available: true }),
      );
    const { result, unmount } = renderHook(() => useGoalState("conv", true));
    try {
      await act(async () => {});
      expect(result.current.goal).toBeNull();
      expect(result.current.goalRevision).toBeGreaterThan(0);

      await act(async () => {
        await expect(result.current.refreshGoal()).rejects.toMatchObject({
          status: 503,
          message: "runner unavailable",
        });
      });
      await act(() => vi.advanceTimersByTimeAsync(5_000));

      expect(fetch).toHaveBeenCalledTimes(3);
      expect(result.current.goal).toBeNull();
      expect(
        fetch.mock.calls.map(([, init]) =>
          new Headers(init?.headers).get("X-Omnigent-Codex-Goal-Read"),
        ),
      ).toEqual([null, "strict", null]);
    } finally {
      unmount();
      fetch.mockRestore();
    }
  });

  it("does not publish an unavailable initial background read", async () => {
    mockGetGoal.mockResolvedValue({ available: false });
    const { result } = renderHook(() => useGoalState("conv", true));

    await act(async () => {});

    expect(result.current.goal).toBeNull();
    expect(result.current.goalRevision).toBe(0);
  });

  it.each([GOAL, null])(
    "retains the snapshot and revision after unavailable polling (%j)",
    async (goal) => {
      vi.useFakeTimers();
      mockGetGoal.mockResolvedValueOnce({ goal });
      const { result } = renderHook(() => useGoalState("conv", true));
      await act(async () => {});
      const revision = result.current.goalRevision;
      mockGetGoal.mockRejectedValueOnce(new Error("runner unavailable"));
      await act(async () => {
        await expect(result.current.refreshGoal()).rejects.toThrow("runner unavailable");
      });
      mockGetGoal.mockResolvedValue({ available: false });

      await act(() => vi.advanceTimersByTimeAsync(5_000));
      await act(async () => window.dispatchEvent(new Event("focus")));

      expect(result.current.goal).toBe(goal);
      expect(result.current.goalRevision).toBe(revision);
      expect(mockGetGoal).toHaveBeenLastCalledWith("conv", { strict: false });
    },
  );

  it.each([GOAL, null])(
    "accepts an older owned read after a newer unavailable poll (%j)",
    async (goal) => {
      vi.useFakeTimers();
      const olderRead = deferredGoalRead();
      mockGetGoal
        .mockResolvedValueOnce({ goal: GOAL })
        .mockReturnValueOnce(olderRead.promise)
        .mockResolvedValue({ available: false });
      const { result } = renderHook(() => useGoalState("conv", true));
      await act(async () => {});
      const revision = result.current.goalRevision;
      let refresh: Promise<void> | undefined;
      act(() => {
        refresh = result.current.refreshGoal();
      });

      await act(() => vi.advanceTimersByTimeAsync(5_000));
      expect(result.current.goalRevision).toBe(revision);
      await act(async () => {
        olderRead.resolve({ goal });
        await refresh;
      });

      expect(result.current.goal).toBe(goal);
      expect(result.current.goalRevision).toBeGreaterThan(revision);
    },
  );

  it("reflects goals created and paused outside the dialog", async () => {
    vi.useFakeTimers();
    mockGetGoal
      .mockResolvedValueOnce({ goal: null })
      .mockResolvedValueOnce({ goal: GOAL })
      .mockResolvedValueOnce({ goal: { ...GOAL, status: "paused" } })
      .mockResolvedValue({ goal: GOAL });
    const { result, rerender } = renderHook(
      ({ pollingEnabled }) => useGoalState("conv", true, pollingEnabled),
      { initialProps: { pollingEnabled: true } },
    );
    await act(async () => {});
    expect(result.current.goal).toBeNull();

    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(result.current.goal).toEqual(GOAL);
    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(result.current.goal?.status).toBe("paused");

    rerender({ pollingEnabled: false });
    expect(result.current.goal).toEqual({ ...GOAL, status: "paused" });
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(mockGetGoal).toHaveBeenCalledTimes(3);

    await act(() => result.current.refreshGoal());
    expect(mockGetGoal).toHaveBeenCalledTimes(4);
    expect(result.current.goal).toEqual(GOAL);
    act(() => result.current.setGoal({ ...GOAL, status: "paused" }));
    expect(result.current.goal?.status).toBe("paused");
    act(() => result.current.setGoal(null));
    expect(result.current.goal).toBeNull();

    rerender({ pollingEnabled: true });
    await act(async () => {});
    expect(result.current.goal).toEqual(GOAL);
    expect(mockGetGoal).toHaveBeenCalledTimes(5);
    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(mockGetGoal).toHaveBeenCalledTimes(6);
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
      .mockResolvedValueOnce(Response.json({ available: true, goal: null }));
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
      expect(fetch).toHaveBeenLastCalledWith(
        "/v1/sessions/conv/codex_goal",
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      );
      expect(new Headers(fetch.mock.calls[2][1]?.headers).has("X-Omnigent-Codex-Goal-Read")).toBe(
        false,
      );
    } finally {
      unmount();
      fetch.mockRestore();
    }
  });

  it("backs off through a repeated sixty-second cap and resets polling after recovery", async () => {
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
    const pausedGoal = { ...GOAL, status: "paused" as const };
    mockGetGoal.mockResolvedValue({ goal: pausedGoal });
    await act(() => vi.advanceTimersByTimeAsync(59_999));
    expect(mockGetGoal).toHaveBeenCalledTimes(7);
    expect(result.current.goal).toEqual(GOAL);
    await act(() => vi.advanceTimersByTimeAsync(1));
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
    expect(mockGetGoal).toHaveBeenCalledWith("conv", { strict: false });
  });

  it.each(["capability", "runner"])(
    "retains only a runner reconnect's last snapshot when the %s read fails",
    async (boundary) => {
      mockGetGoal.mockResolvedValueOnce({ goal: GOAL }).mockRejectedValueOnce(new Error("offline"));
      const { result, rerender } = renderHook(
        ({ connected }) =>
          useGoalState(
            "conv",
            boundary === "capability" ? connected : true,
            boundary === "runner" ? connected : true,
          ),
        { initialProps: { connected: true } },
      );
      await waitFor(() => expect(result.current.goal).toEqual(GOAL));

      rerender({ connected: false });
      rerender({ connected: true });
      await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(2));

      expect(result.current.goal).toEqual(boundary === "runner" ? GOAL : null);
    },
  );

  it.each(
    ["capability", "runner"].flatMap((boundary) =>
      ["success", "failure"].map((outcome) => ({ boundary, outcome })),
    ),
  )(
    "ignores a pending dialog read's $outcome and old setter after the $boundary reconnects",
    async ({ boundary, outcome }) => {
      const oldRead = deferredGoalRead();
      mockGetGoal
        .mockResolvedValueOnce({ goal: GOAL })
        .mockReturnValueOnce(oldRead.promise)
        .mockResolvedValueOnce({ goal: null });
      const { result, rerender } = renderHook(
        ({ connected }) =>
          useGoalState(
            "conv",
            boundary === "capability" ? connected : true,
            boundary === "runner" ? connected : true,
          ),
        { initialProps: { connected: true } },
      );
      await act(async () => {});
      const oldSetter = result.current.setGoal;
      const oldRefresh = result.current.refreshGoal();

      rerender({ connected: false });
      rerender({ connected: true });
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
