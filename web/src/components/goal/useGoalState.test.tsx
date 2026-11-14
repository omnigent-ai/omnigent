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
      .mockResolvedValueOnce({ goal: null });
    const { result, rerender } = renderHook(({ id }) => useGoalState(id, true), {
      initialProps: { id: "first" },
    });
    const updatePreviousGoal = result.current.setGoal;
    rerender({ id: "second" });
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
});
