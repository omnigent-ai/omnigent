import { describe, expect, it } from "vitest";
import { __parseCodexGoalBudgetForTest } from "./CodexGoalDialog";

describe("Codex goal budget parsing", () => {
  it("returns null for blank budgets and parses positive safe integers", () => {
    expect(__parseCodexGoalBudgetForTest(" ")).toBeNull();
    expect(__parseCodexGoalBudgetForTest("40000")).toBe(40000);
  });

  it("rejects non-positive, fractional, and unsafe budgets", () => {
    expect(() => __parseCodexGoalBudgetForTest("0")).toThrow(/positive whole number/);
    expect(() => __parseCodexGoalBudgetForTest("1.5")).toThrow(/positive whole number/);
    expect(() => __parseCodexGoalBudgetForTest("9007199254740992")).toThrow(
      /positive whole number/,
    );
  });
});
