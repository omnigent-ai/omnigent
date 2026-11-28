// Check Again reports a change only where the fresh read shows that very change made: a closed
// PR keeps its draft flag, so it never confirms Ready or Convert, and a report goes once.

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  type CheckedChange,
  changeMadeBy,
  withdrawalMadeBy,
  type FreshPullRequestFacts,
  keepUnconfirmedReport,
  resetChangeAttemptsForTests,
  settleUnconfirmedReport,
} from "./changeAttempts";

const facts = (
  state: FreshPullRequestFacts["state"],
  isDraft = false,
  reviewers: FreshPullRequestFacts["reviewers"] = null,
): FreshPullRequestFacts => ({ state, isDraft, reviewers, autoMerge: "unknown" });

beforeEach(() => resetChangeAttemptsForTests());

describe("changeMadeBy", () => {
  it.each<[CheckedChange, FreshPullRequestFacts, string | null]>([
    ["pull_request_close", facts("CLOSED"), "closed"],
    ["pull_request_close", facts("OPEN"), null],
    ["pull_request_close", facts("MERGED"), null],
    ["pull_request_reopen", facts("OPEN"), "reopened"],
    ["pull_request_reopen", facts("CLOSED"), null],
    ["pull_request_reopen", facts("MERGED"), null],
    ["ready_for_review", facts("OPEN", false), "marked_ready"],
    ["ready_for_review", facts("OPEN", true), null],
    ["ready_for_review", facts("CLOSED", false), null],
    ["ready_for_review", facts("CLOSED", true), null],
    ["ready_for_review", facts("MERGED", false), null],
    ["convert_to_draft", facts("OPEN", true), "converted_to_draft"],
    ["convert_to_draft", facts("OPEN", false), null],
    ["convert_to_draft", facts("CLOSED", true), null],
    ["convert_to_draft", facts("CLOSED", false), null],
    ["convert_to_draft", facts("MERGED", true), null],
  ])("%s with %o reports %s", (change, read, made) => {
    expect(changeMadeBy(change, read)).toBe(made);
  });
});

describe("withdrawalMadeBy", () => {
  const user = (login: string, state = "PENDING") => ({ login, kind: "user", state });
  it.each<[string, FreshPullRequestFacts["reviewers"], string]>([
    // Removing the last reviewer leaves none: that's the withdrawal made.
    ["no reviewers left", [], "removed"],
    ["another reviewer still asked", [user("bob")], "removed"],
    ["their review stays, the request gone", [user("Octo", "APPROVED")], "removed"],
    [
      "a team of the same name still asked",
      [{ login: "octo", kind: "team", state: "PENDING" }],
      "removed",
    ],
    ["still asked, in another case", [user("OCTO")], "still_asked"],
    // An answer without reviewers can't say: not the same as none.
    ["no reviewers field", null, "cant_tell"],
  ])("%s: %s", (_case, reviewers, made) => {
    expect(withdrawalMadeBy("octo", facts("OPEN", false, reviewers))).toBe(made);
  });
});

describe("settleUnconfirmedReport", () => {
  it("reports the change made, once, through the attempt's own report", () => {
    const succeeded = vi.fn();
    keepUnconfirmedReport("attempt-a", { succeeded });
    settleUnconfirmedReport("attempt-b", "pull_request_close", facts("CLOSED"));
    expect(succeeded).not.toHaveBeenCalled();
    settleUnconfirmedReport("attempt-a", "pull_request_close", facts("CLOSED"));
    settleUnconfirmedReport("attempt-a", "pull_request_close", facts("CLOSED"));
    expect(succeeded.mock.calls).toEqual([["closed"]]);
  });

  it("drops the report where the read contradicts the change", () => {
    const succeeded = vi.fn();
    keepUnconfirmedReport("attempt-a", { succeeded });
    settleUnconfirmedReport("attempt-a", "ready_for_review", facts("CLOSED", false));
    settleUnconfirmedReport("attempt-a", "ready_for_review", facts("OPEN", false));
    expect(succeeded).not.toHaveBeenCalled();
  });
});
