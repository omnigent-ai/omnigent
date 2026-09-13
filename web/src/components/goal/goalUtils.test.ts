import { describe, expect, it } from "vitest";
import type { Goal } from "@/lib/goalApi";
import {
  canPauseGoal,
  canResumeGoal,
  formatGoalStatus,
  formatGoalUsage,
  goalModeDraftForGoal,
  isGoalUserMode,
} from "./goalUtils";

const GOAL: Goal = {
  objective: "Ship goal mode",
  status: "active",
  tokenBudget: 40000,
  tokensUsed: 1200,
  timeUsedSeconds: 125,
  createdAt: null,
  updatedAt: null,
};

describe("goal utils", () => {
  it("formats status and usage", () => {
    expect(formatGoalStatus("budgetLimited")).toBe("budgetLimited");
    expect(formatGoalUsage(GOAL)).toBe("1,200 / 40,000 tokens / 2 min");
    expect(formatGoalUsage({ ...GOAL, tokenBudget: null, timeUsedSeconds: 59 })).toBe(
      "1,200 tokens",
    );
  });

  it("classifies pause/resume and draft modes", () => {
    expect(canPauseGoal(GOAL)).toBe(true);
    expect(canPauseGoal({ ...GOAL, status: "paused" })).toBe(false);
    expect(canResumeGoal({ ...GOAL, status: "paused" })).toBe(true);
    expect(canResumeGoal({ ...GOAL, status: "blocked" })).toBe(true);
    expect(canResumeGoal({ ...GOAL, status: "usageLimited" })).toBe(true);
    expect(canResumeGoal({ ...GOAL, status: "complete" })).toBe(false);
    expect(isGoalUserMode("active")).toBe(true);
    expect(isGoalUserMode("blocked")).toBe(false);
    expect(goalModeDraftForGoal(null)).toBe("active");
    expect(goalModeDraftForGoal({ ...GOAL, status: "paused" })).toBe("paused");
    expect(goalModeDraftForGoal({ ...GOAL, status: "blocked" })).toBe("keep");
  });
});
