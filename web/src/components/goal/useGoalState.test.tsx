import type * as GoalApiModule from "@/lib/goalApi";

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
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

describe("useGoalState", () => {
  it("loads the goal when enabled for a conversation", async () => {
    mockGetGoal.mockResolvedValueOnce({ goal: GOAL });

    const { result } = renderHook(() => useGoalState("conv", true));

    await waitFor(() => expect(result.current.goal).toEqual(GOAL));
    expect(mockGetGoal).toHaveBeenCalledWith("conv");
  });

  it("clears a loaded goal when disabled", async () => {
    mockGetGoal.mockResolvedValueOnce({ goal: GOAL });

    const { result, rerender } = renderHook(({ enabled }) => useGoalState("conv", enabled), {
      initialProps: { enabled: true },
    });
    await waitFor(() => expect(result.current.goal).toEqual(GOAL));

    rerender({ enabled: false });
    expect(result.current.goal).toBeNull();
    expect(mockGetGoal).toHaveBeenCalledTimes(1);
  });

  it("fails closed when a turn-edge read errors", async () => {
    mockGetGoal.mockResolvedValueOnce({ goal: GOAL }).mockRejectedValueOnce(new Error("offline"));

    const { result, rerender } = renderHook(
      ({ turnActive }) => useGoalState("conv", true, turnActive),
      { initialProps: { turnActive: false } },
    );
    await waitFor(() => expect(result.current.goal).toEqual(GOAL));

    rerender({ turnActive: true });
    await waitFor(() => expect(result.current.goal).toBeNull());
  });

  it("does not let an older turn-edge read overwrite a newer dialog update", async () => {
    let resolveStaleRead: (response: { goal: Goal | null }) => void = () => {};
    mockGetGoal.mockResolvedValueOnce({ goal: null }).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveStaleRead = resolve;
      }),
    );
    const { result, rerender } = renderHook(
      ({ turnActive }) => useGoalState("conv", true, turnActive),
      { initialProps: { turnActive: false } },
    );
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(1));
    rerender({ turnActive: true });
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(2));

    const saved = { ...GOAL, objective: "Saved in the dialog" };
    act(() => result.current.setGoal(saved));
    await act(async () => resolveStaleRead({ goal: GOAL }));

    expect(result.current.goal).toEqual(saved);
  });

  it("keeps setGoal stable across renders", () => {
    const { result, rerender } = renderHook(
      ({ turnActive }) => useGoalState("conv", false, turnActive),
      { initialProps: { turnActive: false } },
    );
    const first = result.current.setGoal;
    rerender({ turnActive: true });
    expect(result.current.setGoal).toBe(first);
  });

  // A typed /goal sets the goal on the runner, outside this hook's writes, and
  // the turn-start read can reach the runner before it stores the goal.
  it.each([
    {
      name: "reloads the goal when a turn starts and ends",
      reads: [null, GOAL, { ...GOAL, status: "complete" }],
    },
    {
      name: "shows a typed goal at turn end when the turn-start read was too early",
      reads: [null, null, GOAL],
    },
  ])("$name", async ({ reads }) => {
    for (const goal of reads) mockGetGoal.mockResolvedValueOnce({ goal });
    const { result, rerender } = renderHook(
      ({ turnActive }) => useGoalState("conv", true, turnActive),
      { initialProps: { turnActive: false } },
    );
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(1));
    expect(result.current.goal).toBeNull();

    rerender({ turnActive: true });
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.goal).toEqual(reads[1]));

    rerender({ turnActive: false });
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(result.current.goal).toEqual(reads[2]));
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
