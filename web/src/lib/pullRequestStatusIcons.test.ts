import { describe, expect, it } from "vitest";
import {
  MERGE_READINESS_STATUSES,
  type MergeReadinessStatus,
  type MergeReadinessView,
} from "@/lib/mergeReadiness";

import {
  mergeReadinessMeaning,
  PULL_REQUEST_STATUS_ICONS,
  type PullRequestStatusMeaning,
} from "./pullRequestStatusIcons";

function known(status: MergeReadinessStatus): Exclude<MergeReadinessView, { kind: "off" }> {
  return {
    kind: "known",
    readiness: { status, reasons: { failingJobs: [], failingRequirements: [] } },
    headSha: "abc",
  };
}

const REVIEW_MEANINGS: PullRequestStatusMeaning[] = [
  "review-requested",
  "needs-reviewer",
  "approved",
  "changes-requested",
  "returned",
];

// The CI column's glyphs are circles; a review outcome must never look like one.
const CI_CIRCLES = ["CircleCheck", "CircleX", "CircleDot"];

describe("mergeReadinessMeaning", () => {
  it("gives every known readiness a meaning with an icon", () => {
    for (const status of MERGE_READINESS_STATUSES) {
      expect(PULL_REQUEST_STATUS_ICONS[mergeReadinessMeaning(known(status))]).toBeDefined();
    }
  });

  it("reads review readiness with the same meaning GitHub's own review gets", () => {
    expect(mergeReadinessMeaning(known("approved"))).toBe("approved");
    expect(mergeReadinessMeaning(known("changes_requested"))).toBe("changes-requested");
    expect(mergeReadinessMeaning(known("review_required"))).toBe("review-requested");
    expect(mergeReadinessMeaning(known("pending_approval"))).toBe("review-requested");
    expect(mergeReadinessMeaning(known("needs_reviewer"))).toBe("needs-reviewer");
    expect(mergeReadinessMeaning(known("returned"))).toBe("returned");
  });

  it("names a check in flight and each read failure, never as a lifecycle state", () => {
    expect(mergeReadinessMeaning({ kind: "checking" })).toBe("checking");
    expect(mergeReadinessMeaning({ kind: "unknown", failure: "credential" })).toBe("credential");
    expect(mergeReadinessMeaning({ kind: "unknown", failure: "github" })).toBe("readiness-unknown");
    expect(mergeReadinessMeaning({ kind: "unknown", failure: "service" })).toBe(
      "readiness-unknown",
    );
    expect(mergeReadinessMeaning(known("unknown"))).toBe("readiness-unknown");
  });
});

describe("PULL_REQUEST_STATUS_ICONS", () => {
  it("draws review outcomes as people and documents, never the CI column's circles", () => {
    for (const meaning of REVIEW_MEANINGS) {
      expect(CI_CIRCLES).not.toContain(PULL_REQUEST_STATUS_ICONS[meaning].displayName);
    }
  });

  it("keeps Ready and Merge Requested apart from each other and from a merged PR", () => {
    const glyphs = ["ready", "merge-requested", "merged"].map(
      (meaning) => PULL_REQUEST_STATUS_ICONS[meaning as PullRequestStatusMeaning],
    );
    expect(new Set(glyphs).size).toBe(3);
  });
});
