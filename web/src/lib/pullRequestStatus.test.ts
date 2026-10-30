import { describe, expect, it } from "vitest";
import {
  getPullRequestStatus,
  hasNewerPullRequestLifecycle,
  sortPullRequestsByStatus,
  type PullRequestStatusSummary,
} from "./pullRequestStatus";

describe("hasNewerPullRequestLifecycle", () => {
  it.each([
    { stored: { state: "open" }, live: { state: "OPEN", is_draft: false }, expected: false },
    { stored: { state: "OPEN" }, live: { state: "OPEN", is_draft: true }, expected: false },
    { stored: { state: "UNKNOWN" }, live: { state: "OPEN" }, expected: false },
    { stored: { state: null }, live: { state: "OPEN" }, expected: false },
    { stored: { state: "MERGED" }, live: { state: "OPEN" }, expected: true },
    {
      stored: { state: "MERGED", is_draft: true },
      live: { state: "merged", is_draft: false },
      expected: false,
    },
    { stored: { state: "OPEN" }, live: { state: "CLOSED" }, expected: true },
    {
      stored: { state: "OPEN", is_draft: true },
      live: { state: "OPEN", is_draft: false },
      expected: true,
    },
    {
      stored: { state: "OPEN", is_draft: false },
      live: { state: "OPEN", is_draft: true },
      expected: true,
    },
  ])("compares only known lifecycle fields: %j", ({ stored, live, expected }) => {
    expect(hasNewerPullRequestLifecycle({ ...stored, state_checked_at: 3 }, live, 2000)).toBe(
      expected,
    );
  });

  it.each([undefined, 1, 2])(
    "ignores a lifecycle change without a newer timestamp: %s",
    (state_checked_at) => {
      expect(
        hasNewerPullRequestLifecycle(
          { state: "MERGED", state_checked_at },
          { state: "OPEN" },
          2000,
        ),
      ).toBe(false);
    },
  );
});

describe("getPullRequestStatus", () => {
  it.each(["MERGED", "CLOSED"])(
    "keeps %s terminal despite stale draft and check metadata",
    (state) => {
      expect(
        getPullRequestStatus({
          state,
          is_draft: true,
          merge_state_status: "DIRTY",
          checks: { failing: 1, pending: 1 },
        }),
      ).toBe(state.toLowerCase());
    },
  );

  it("does not infer merge readiness from passing checks or missing state", () => {
    expect(getPullRequestStatus({ state: "OPEN", checks: { failing: 0, pending: 0 } })).toBe(
      "open",
    );
    expect(getPullRequestStatus({ state: "OPEN", merge_state_status: "BLOCKED" })).toBe("open");
    expect(getPullRequestStatus({ merge_state_status: "CLEAN" })).toBe("unknown");
    expect(getPullRequestStatus(null)).toBe("unknown");
  });

  it.each([
    { checks: { failing: 1, pending: 1 }, expected: "failed" },
    { checks: { failing: 0, pending: 1 }, expected: "pending" },
    { checks: { failing: 0, pending: 0, partial: true }, expected: "open" },
    { checks_supported: false, expected: "open" },
    { is_draft: true, expected: "draft" },
  ])("does not show ready with unresolved metadata: $expected", ({ expected, ...extra }) => {
    expect(getPullRequestStatus({ state: "OPEN", merge_state_status: "CLEAN", ...extra })).toBe(
      expected,
    );
  });

  it("uses the provider's merge state and accepts its normalized casing", () => {
    expect(
      getPullRequestStatus({
        state: "open",
        merge_state_status: "dirty",
        checks: { failing: 1, pending: 1 },
      }),
    ).toBe("conflict");
    expect(getPullRequestStatus({ state: "OPEN", merge_state_status: "CLEAN" })).toBe("ready");
  });
});

describe("sortPullRequestsByStatus", () => {
  it("prioritizes repairs on drafts while keeping ordinary drafts below ready PRs", () => {
    const prs: (PullRequestStatusSummary & { id: string })[] = [
      { id: "ready", state: "OPEN", merge_state_status: "CLEAN" },
      { id: "running-draft", state: "OPEN", is_draft: true, checks: { failing: 0, pending: 1 } },
      { id: "failed-draft", state: "OPEN", is_draft: true, checks: { failing: 1, pending: 1 } },
      { id: "conflicted-draft", state: "OPEN", is_draft: true, merge_state_status: "DIRTY" },
    ];
    expect(sortPullRequestsByStatus(prs).map((pr) => [pr.id, getPullRequestStatus(pr)])).toEqual([
      ["conflicted-draft", "conflict"],
      ["failed-draft", "failed"],
      ["ready", "ready"],
      ["running-draft", "draft"],
    ]);
  });

  it("puts conflicts, failing checks, and merge-ready PRs before inactive PRs without mutating the list", () => {
    const prs: (PullRequestStatusSummary & { id: string })[] = [
      { id: "merged", state: "MERGED" },
      { id: "draft", state: "OPEN", is_draft: true },
      { id: "open-a", state: "OPEN" },
      { id: "closed", state: "CLOSED" },
      { id: "ready", state: "OPEN", merge_state_status: "CLEAN" },
      { id: "unknown" },
      { id: "pending", state: "OPEN", checks: { failing: 0, pending: 1 } },
      { id: "open-b", state: "OPEN" },
      { id: "failed", state: "OPEN", checks: { failing: 1, pending: 0 } },
      { id: "conflict", state: "OPEN", merge_state_status: "DIRTY" },
    ];
    const original = [...prs];
    expect(sortPullRequestsByStatus(prs).map((pr) => pr.id)).toEqual([
      "conflict",
      "failed",
      "ready",
      "pending",
      "open-a",
      "open-b",
      "draft",
      "unknown",
      "merged",
      "closed",
    ]);
    expect(prs).toEqual(original);
  });
});
