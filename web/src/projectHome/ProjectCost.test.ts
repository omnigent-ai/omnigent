import { describe, expect, it } from "vitest";
import { describeTicketCost, type TicketCost } from "./ProjectCost";

function cost(overrides: Partial<TicketCost>): TicketCost {
  return { cost_usd: 0, session_count: 0, unpriced_sessions: 0, sessions: [], ...overrides };
}

describe("describeTicketCost", () => {
  it("says when nothing is linked yet", () => {
    expect(describeTicketCost(cost({}))).toBe("No linked agent sessions yet");
  });

  it("sums priced sessions", () => {
    expect(describeTicketCost(cost({ cost_usd: 3.456, session_count: 2 }))).toBe(
      "$3.46 across 2 linked sessions",
    );
  });

  it("calls out sessions with no recorded cost instead of showing $0", () => {
    expect(describeTicketCost(cost({ session_count: 1, unpriced_sessions: 1 }))).toBe(
      "Cost unknown for 1 linked session",
    );
    expect(describeTicketCost(cost({ cost_usd: 1, session_count: 3, unpriced_sessions: 1 }))).toBe(
      "$1.00 across 3 linked sessions; cost unknown for 1",
    );
  });
});
