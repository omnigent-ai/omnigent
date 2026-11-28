import { describe, expect, it } from "vitest";
import type { MergeReadinessStatus, MergeReadinessView } from "@/lib/mergeReadiness";

import {
  mergeReadinessApplies,
  pullRequestHeaderStatus,
  pullRequestStatusDisplay,
  sortPullRequestsByDisplay,
} from "./pullRequestStatusDisplay";
import {
  exampleMergeReadinessSource,
  withPullRequestIntegrations,
} from "@/test/pullRequestIntegrations";

// Merge readiness from a stand-in source, as a build that registers one shows it.
withPullRequestIntegrations({
  mergeReadiness: exampleMergeReadinessSource(async () => ({ kind: "checking" })),
});

function known(status: MergeReadinessStatus): MergeReadinessView {
  return {
    kind: "known",
    readiness: { status, reasons: { failingJobs: [], failingRequirements: [] } },
    headSha: "abc",
  };
}

const OPEN_CLEAN = { state: "OPEN", merge_state_status: "CLEAN" };

describe("mergeReadinessApplies", () => {
  it("applies to open PRs while on, and never once GitHub confirms a merge or close", () => {
    expect(mergeReadinessApplies(undefined, "OPEN")).toBe(false);
    expect(mergeReadinessApplies({ kind: "off" }, "OPEN")).toBe(false);
    expect(mergeReadinessApplies({ kind: "checking" }, "OPEN")).toBe(true);
    expect(mergeReadinessApplies(known("ready"), null)).toBe(true);
    expect(mergeReadinessApplies(known("ready"), "merged")).toBe(false);
    expect(mergeReadinessApplies(known("ready"), "CLOSED")).toBe(false);
  });
});

describe("pullRequestStatusDisplay", () => {
  it("keeps GitHub's status when merge readiness is off or missing", () => {
    expect(pullRequestStatusDisplay(OPEN_CLEAN)).toMatchObject({ key: "ready" });
    expect(pullRequestStatusDisplay(OPEN_CLEAN, { kind: "off" })).toMatchObject({ key: "ready" });
    expect(pullRequestStatusDisplay(null)).toMatchObject({ key: "unknown" });
  });

  it("shows merge readiness with its label, tone and icon", () => {
    const display = pullRequestStatusDisplay(OPEN_CLEAN, known("changes_requested"));
    expect(display).toMatchObject({
      key: "merge:changes_requested",
      label: "Changes requested",
      className: "text-status-red",
      priority: 1,
    });
    expect(display.Icon.displayName).toBe("FileDiff");
  });

  it("names checking and failed reads, sorting them after actionable states", () => {
    expect(pullRequestStatusDisplay(OPEN_CLEAN, { kind: "checking" })).toMatchObject({
      key: "merge:checking",
      priority: 6,
    });
    const failed = pullRequestStatusDisplay(OPEN_CLEAN, {
      kind: "unknown",
      failure: "service",
    });
    expect(failed).toMatchObject({ key: "merge:unknown", priority: 6 });
  });

  it("lets a confirmed merge beat an older readiness read", () => {
    expect(pullRequestStatusDisplay({ state: "MERGED" }, known("conflicts"))).toMatchObject({
      key: "merged",
    });
  });
});

describe("pullRequestStatusDisplay's badge fields", () => {
  it("gives GitHub's statuses a tint, a short label and a reason", () => {
    expect(pullRequestStatusDisplay({ state: "OPEN", merge_state_status: "DIRTY" })).toMatchObject({
      label: "Merge conflicts",
      short: "Conflicts",
      tone: "red",
      detail: "Resolve the merge conflicts with the base branch.",
    });
    expect(
      pullRequestStatusDisplay({ state: "OPEN", checks: { failing: 2, pending: 0 } }),
    ).toMatchObject({ short: "Checks failing", tone: "red", detail: "2 checks are failing." });
    expect(
      pullRequestStatusDisplay({ state: "OPEN", checks: { failing: 1, pending: 0 } }).detail,
    ).toBe("1 check is failing.");
  });

  it("takes merge readiness's short label, tint and reason where it applies", () => {
    expect(pullRequestStatusDisplay(OPEN_CLEAN, known("pending_approval"))).toMatchObject({
      label: "Pending approval",
      short: "In review",
      tone: "yellow",
      detail: "Waiting for a requested reviewer to approve.",
    });
  });
});

describe("pullRequestHeaderStatus", () => {
  it("leaves out what the lifecycle pill already says and what's still loading", () => {
    const shown = (pr: Parameters<typeof pullRequestStatusDisplay>[0], view?: MergeReadinessView) =>
      pullRequestHeaderStatus(pullRequestStatusDisplay(pr, view))?.key ?? null;
    expect(shown({ state: "OPEN" })).toBeNull();
    expect(shown({ state: "OPEN", is_draft: true })).toBeNull();
    expect(shown({ state: "MERGED" })).toBeNull();
    expect(shown(null)).toBeNull();
    expect(shown(OPEN_CLEAN, { kind: "checking" })).toBeNull();
    expect(shown(OPEN_CLEAN, known("draft"))).toBeNull();
    expect(shown(OPEN_CLEAN)).toBe("ready");
    expect(shown(OPEN_CLEAN, known("conflicts"))).toBe("merge:conflicts");
    // A merge status that couldn't be read says so, with its reason.
    expect(shown(OPEN_CLEAN, { kind: "unknown", failure: "credential" })).toBe("merge:unknown");
  });
});

describe("sortPullRequestsByDisplay", () => {
  it("orders by readiness where it applies and keeps equal statuses in place", () => {
    const prs = [
      { id: "draft", state: "OPEN", is_draft: true },
      { id: "open-a", state: "OPEN" },
      { id: "conflicts", state: "OPEN", mergeReadiness: known("conflicts") },
      { id: "open-b", state: "OPEN" },
      { id: "merged", state: "MERGED", mergeReadiness: known("conflicts") },
    ];
    expect(sortPullRequestsByDisplay(prs).map((pr) => pr.id)).toEqual([
      "conflicts",
      "open-a",
      "open-b",
      "draft",
      "merged",
    ]);
  });
});
