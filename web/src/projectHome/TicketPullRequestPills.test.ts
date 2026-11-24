import { describe, expect, it } from "vitest";
import type { TicketPullRequestStatus } from "@/lib/projectTicketsApi";
import { ticketPullRequestPills } from "./TicketPullRequestPills";

function status(overrides: Partial<TicketPullRequestStatus> = {}): TicketPullRequestStatus {
  return {
    url: "https://github.com/example/repo/pull/1",
    state: "OPEN",
    is_draft: false,
    merge_state_status: null,
    checks_supported: true,
    checks: { failing: 0, pending: 0, total: 0 },
    ...overrides,
  };
}

const labels = (value: TicketPullRequestStatus) =>
  ticketPullRequestPills(value).map((pill) => pill.label);

describe("ticketPullRequestPills", () => {
  it.each([
    ["passing", { checks: { failing: 0, pending: 0, total: 5 } }, ["Open", "Checks passing"]],
    ["failing", { checks: { failing: 2, pending: 1, total: 5 } }, ["Open", "2 checks failing"]],
    ["one failing", { checks: { failing: 1, pending: 0, total: 5 } }, ["Open", "1 check failing"]],
    ["running", { checks: { failing: 0, pending: 3, total: 5 } }, ["Open", "Checks running"]],
    ["no CI", {}, ["Open"]],
    [
      "unreadable CI",
      { checks_supported: false, checks: { failing: 1, pending: 0, total: 1 } },
      ["Open"],
    ],
    [
      "draft",
      { is_draft: true, checks: { failing: 0, pending: 1, total: 2 } },
      ["Draft", "Checks running"],
    ],
    [
      "conflicts",
      { merge_state_status: "dirty", checks: { failing: 0, pending: 0, total: 2 } },
      ["Open", "Merge conflicts", "Checks passing"],
    ],
  ] as const)("an open PR with %s CI", (_, overrides, expected) => {
    expect(labels(status(overrides as Partial<TicketPullRequestStatus>))).toEqual(expected);
  });

  it("shows only the final state of a merged or closed PR", () => {
    const checks = { failing: 3, pending: 0, total: 3 };
    expect(labels(status({ state: "MERGED", checks }))).toEqual(["Merged"]);
    expect(labels(status({ state: "CLOSED", checks, merge_state_status: "DIRTY" }))).toEqual([
      "Closed",
    ]);
  });
});
