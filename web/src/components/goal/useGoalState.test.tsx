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

const GOAL_COMPLETE: Goal = { ...GOAL, status: "complete" };

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

  it("clears state when disabled and fails closed on errors", async () => {
    mockGetGoal.mockRejectedValueOnce(new Error("offline"));

    const { result, rerender } = renderHook(({ enabled }) => useGoalState("conv", enabled), {
      initialProps: { enabled: true },
    });
    result.current.setGoal(GOAL);
    await waitFor(() => expect(result.current.goal).toBeNull());

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

  it("refetches when refresh is called", async () => {
    mockGetGoal
      .mockResolvedValueOnce({ goal: GOAL })
      .mockResolvedValueOnce({ goal: GOAL_COMPLETE });

    const { result } = renderHook(() => useGoalState("conv", true));
    await waitFor(() => expect(result.current.goal).toEqual(GOAL));

    act(() => {
      result.current.refresh();
    });

    await waitFor(() => expect(result.current.goal).toEqual(GOAL_COMPLETE));
    expect(mockGetGoal).toHaveBeenCalledTimes(2);
  });

  it("refresh is a stable reference across renders", () => {
    mockGetGoal.mockResolvedValue({ goal: null });

    const { result, rerender } = renderHook(() => useGoalState("conv", true));
    const first = result.current.refresh;
    rerender();
    expect(result.current.refresh).toBe(first);
  });

  it("never calls getGoal when disabled (non-codex-native sessions)", () => {
    // Polly-Claude and Polly-Codex sessions pass enabled=false; verify no
    // fetch is made so the server's 400 INVALID_INPUT path is never hit.
    renderHook(() => useGoalState("conv", false));
    expect(mockGetGoal).not.toHaveBeenCalled();
  });
});
