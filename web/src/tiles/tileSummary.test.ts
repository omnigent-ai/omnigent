import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import { activityNotes, usageTotals } from "./tileSummary";

const row = (overrides: Partial<Conversation>) =>
  ({ id: "conv_a", updated_at: 0, ...overrides }) as Conversation;

describe("activityNotes", () => {
  it("lists waiting questions, blockers and background work", () => {
    expect(
      activityNotes(
        row({
          pending_elicitations_count: 2,
          blocked_on: "approval",
          background_work_running: true,
        }),
      ),
    ).toEqual(["2 questions waiting", "Blocked on approval", "Background work running"]);
    expect(activityNotes(row({ pending_elicitations_count: 1 }))).toEqual(["1 question waiting"]);
    expect(activityNotes(row({}))).toEqual([]);
  });
});

describe("usageTotals", () => {
  const usage = (input: number | null, output: number | null, total: number | null = null) => ({
    inputTokens: input,
    outputTokens: output,
    totalTokens: total,
    cacheReadInputTokens: null,
    cacheCreationInputTokens: null,
    totalCostUsd: null,
  });

  it("sums every model, falling back to input plus output", () => {
    expect(usageTotals({ a: usage(100, 20, 130), b: usage(10, 5) })).toEqual({
      input: 110,
      output: 25,
      total: 145,
    });
  });

  it("is null when nothing was reported", () => {
    expect(usageTotals(null)).toBeNull();
    expect(usageTotals({})).toBeNull();
  });
});
