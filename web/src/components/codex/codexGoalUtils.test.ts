import { describe, expect, it } from "vitest";
import type { CodexGoal } from "@/lib/codexGoalApi";
import {
  canPauseCodexGoal,
  canResumeCodexGoal,
  codexGoalModeDraftForGoal,
  formatCodexGoalStatus,
  formatCodexGoalUsage,
  isCodexGoalUserMode,
} from "./codexGoalUtils";

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

describe("codex goal utils", () => {
  it("formats status and usage", () => {
    expect(formatCodexGoalStatus("budgetLimited")).toBe("budgetLimited");
    expect(formatCodexGoalUsage(GOAL)).toBe("1,200 / 40,000 tokens / 2 min");
    expect(formatCodexGoalUsage({ ...GOAL, tokenBudget: null, timeUsedSeconds: 59 })).toBe(
      "1,200 tokens",
    );
  });

  it("classifies pause/resume and draft modes", () => {
    expect(canPauseCodexGoal(GOAL)).toBe(true);
    expect(canPauseCodexGoal({ ...GOAL, status: "paused" })).toBe(false);
    expect(canResumeCodexGoal({ ...GOAL, status: "paused" })).toBe(true);
    expect(canResumeCodexGoal({ ...GOAL, status: "blocked" })).toBe(true);
    expect(canResumeCodexGoal({ ...GOAL, status: "usageLimited" })).toBe(true);
    expect(canResumeCodexGoal({ ...GOAL, status: "complete" })).toBe(false);
    expect(isCodexGoalUserMode("active")).toBe(true);
    expect(isCodexGoalUserMode("blocked")).toBe(false);
    expect(codexGoalModeDraftForGoal(null)).toBe("active");
    expect(codexGoalModeDraftForGoal({ ...GOAL, status: "paused" })).toBe("paused");
    expect(codexGoalModeDraftForGoal({ ...GOAL, status: "blocked" })).toBe("keep");
  });
});
