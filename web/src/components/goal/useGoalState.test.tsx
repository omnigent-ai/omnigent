import type * as GoalApiModule from "@/lib/goalApi";

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getGoal, type Goal } from "@/lib/goalApi";
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

  it("keeps a dialog update when an older goal read finishes", async () => {
    let resolveRead!: (response: { goal: Goal | null }) => void;
    mockGetGoal.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveRead = resolve;
      }),
    );
    const { result } = renderHook(() => useGoalState("conv", true));
    act(() => result.current.setGoal(GOAL));

    await act(async () => resolveRead({ goal: null }));

    expect(result.current.goal).toEqual(GOAL);
  });

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

  it("invalidates a pending dialog refresh when a goal is explicitly updated", async () => {
    const read = deferredGoalRead();
    mockGetGoal.mockResolvedValueOnce({ goal: GOAL }).mockReturnValueOnce(read.promise);
    const { result } = renderHook(() => useGoalState("conv", true));
    await act(async () => {});
    let refresh!: Promise<void>;
    act(() => {
      refresh = result.current.refreshGoal();
    });
    const pausedGoal = { ...GOAL, status: "paused" as const };
    act(() => result.current.setGoal(pausedGoal));

    await act(async () => {
      read.resolve({ goal: null });
      await refresh;
    });

    expect(result.current.goal).toEqual(pausedGoal);
  });

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

  it("keeps the last goal across a transient poll failure", async () => {
    vi.useFakeTimers();
    mockGetGoal.mockResolvedValueOnce({ goal: GOAL }).mockRejectedValueOnce(new Error("offline"));
    const { result } = renderHook(() => useGoalState("conv", true));
    await act(async () => {});

    await act(() => vi.advanceTimersByTimeAsync(5_000));

    expect(result.current.goal).toEqual(GOAL);
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
