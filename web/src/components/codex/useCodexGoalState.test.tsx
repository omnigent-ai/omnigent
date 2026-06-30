import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CodexGoal } from "@/lib/codexGoalApi";
import { getCodexGoal } from "@/lib/codexGoalApi";
import { useCodexGoalState } from "./useCodexGoalState";

vi.mock("@/lib/codexGoalApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/codexGoalApi")>();
  return { ...actual, getCodexGoal: vi.fn() };
});

const mockGetCodexGoal = vi.mocked(getCodexGoal);

const GOAL: CodexGoal = {
  threadId: "thread-1",
  objective: "Ship goal mode",
  status: "active",
  tokenBudget: 40000,
  tokensUsed: 1200,
  timeUsedSeconds: 125,
  createdAt: null,
  updatedAt: null,
};

beforeEach(() => {
  mockGetCodexGoal.mockReset();
});

describe("useCodexGoalState", () => {
  it("loads the goal when enabled for a conversation", async () => {
    mockGetCodexGoal.mockResolvedValueOnce({ goal: GOAL });

    const { result } = renderHook(() => useCodexGoalState("conv", true));

    await waitFor(() => expect(result.current.goal).toEqual(GOAL));
    expect(mockGetCodexGoal).toHaveBeenCalledWith("conv");
  });

  it("clears state when disabled and fails closed on errors", async () => {
    mockGetCodexGoal.mockRejectedValueOnce(new Error("offline"));

    const { result, rerender } = renderHook(({ enabled }) => useCodexGoalState("conv", enabled), {
      initialProps: { enabled: true },
    });
    result.current.setGoal(GOAL);
    await waitFor(() => expect(result.current.goal).toBeNull());

    rerender({ enabled: false });
    expect(result.current.goal).toBeNull();
  });
});
