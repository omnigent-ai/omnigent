import { describe, expect, it } from "vitest";

import type { PullRequestChecks } from "@/newPullRequestPage/hooks/usePullRequests";
import type { PullRequestListStats } from "@/lib/pullRequestListStats";
import {
  resolvePullRequestListStatus,
  type LivePullRequestObservation,
} from "@/lib/pullRequestListStatus";

const H1 = "1".repeat(40);
const H2 = "2".repeat(40);

const passingChecks: PullRequestChecks = {
  passing: 3,
  failing: 0,
  pending: 0,
  total: 3,
  runs: [],
};

function live(
  dispatchedAt: number,
  pr: Partial<LivePullRequestObservation["pr"]> = {},
): LivePullRequestObservation {
  return {
    dispatchedAt,
    pr: { state: "OPEN", is_draft: false, head_sha: H1, checks: passingChecks, ...pr },
  };
}

function batch(dispatchedAt: number, fields: Partial<PullRequestListStats> = {}) {
  return {
    additions: 1,
    deletions: 1,
    changedFiles: 1,
    updatedAt: 0,
    headSha: H2,
    dispatchedAt,
    extended: true,
    state: "MERGED",
    isDraft: false,
    ci: "failing",
    ciHeadSha: H2,
    ...fields,
  } satisfies PullRequestListStats;
}

describe("resolvePullRequestListStatus", () => {
  it("keeps a newer batch over an overview sent before it, whatever arrived last", () => {
    const status = resolvePullRequestListStatus({ live: live(1_000), batch: batch(2_000) });
    expect(status).toMatchObject({ state: "MERGED", headSha: H2 });
    // H1's passing checks don't land on H2.
    expect(status.ci).toEqual({ source: "batch", rollup: "failing" });
  });

  it("keeps a newer overview over a batch sent before it", () => {
    const status = resolvePullRequestListStatus({
      live: live(3_000, { head_sha: H2 }),
      batch: batch(2_000, { state: "OPEN", ci: "pending" }),
    });
    expect(status).toMatchObject({ state: "OPEN", isDraft: false, headSha: H2 });
    expect(status.ci).toEqual({ source: "live", checks: passingChecks });
  });

  it("keeps the live checks' counts when a later batch at the same head agrees with them", () => {
    const status = resolvePullRequestListStatus({
      live: live(1_000, { head_sha: H2 }),
      batch: batch(2_000, { state: "OPEN", ci: "passing" }),
    });
    expect(status.ci).toEqual({ source: "live", checks: passingChecks });
  });

  it.each([
    ["failing", "passing"],
    ["pending", "passing"],
  ] as const)(
    "shows a newer batch's %s over older live checks that read %s at the same head",
    (rollup, _live) => {
      const status = resolvePullRequestListStatus({
        live: live(1_000, { head_sha: H2 }),
        batch: batch(2_000, { state: "OPEN", ci: rollup }),
      });
      expect(status.ci).toEqual({ source: "batch", rollup });
    },
  );

  it("shows newer live checks over an older batch at the same head, either way round", () => {
    const failingChecks = { ...passingChecks, passing: 2, failing: 1 };
    // A rerun that passed, read live after a batch that saw it failing.
    expect(
      resolvePullRequestListStatus({
        live: live(3_000, { head_sha: H2 }),
        batch: batch(2_000, { state: "OPEN", ci: "failing" }),
      }).ci,
    ).toEqual({ source: "live", checks: passingChecks });
    // And a failure read live after a batch that saw it passing.
    expect(
      resolvePullRequestListStatus({
        live: live(3_000, { head_sha: H2, checks: failingChecks }),
        batch: batch(2_000, { state: "OPEN", ci: "passing" }),
      }).ci,
    ).toEqual({ source: "live", checks: failingChecks });
  });

  it("reopens from a newer read even when an older one said merged", () => {
    const status = resolvePullRequestListStatus({
      live: live(1_000, { state: "MERGED" }),
      batch: batch(2_000, { state: "OPEN", isDraft: true }),
    });
    expect(status).toMatchObject({ state: "OPEN", isDraft: true });
  });

  it("lets a stored change checked after the reads override them, and not an older one", () => {
    const reads = { live: live(1_000), batch: batch(2_000, { state: "OPEN" }) };
    expect(
      resolvePullRequestListStatus({ ...reads, stored: { state: "MERGED", state_checked_at: 3 } }),
    ).toMatchObject({ state: "MERGED" });
    expect(
      resolvePullRequestListStatus({ ...reads, stored: { state: "MERGED", state_checked_at: 1 } }),
    ).toMatchObject({ state: "OPEN" });
  });

  it("uses the stored lifecycle when nothing live answered", () => {
    expect(resolvePullRequestListStatus({ stored: { state: "OPEN", is_draft: true } })).toEqual({
      state: "OPEN",
      isDraft: true,
      lifecycleSource: "stored",
      headSha: undefined,
      ci: null,
    });
    expect(resolvePullRequestListStatus({})).toEqual({
      state: null,
      isDraft: undefined,
      lifecycleSource: null,
      headSha: undefined,
      ci: null,
    });
  });

  it("shows no CI when the newest head has no read at that head", () => {
    // The batch moved to H2 but its rollup is still for H1.
    const status = resolvePullRequestListStatus({
      live: live(1_000),
      batch: batch(2_000, { state: "OPEN", ci: "passing", ciHeadSha: H1 }),
    });
    expect(status.headSha).toBe(H2);
    expect(status.ci).toBeNull();
  });

  it("treats a null rollup as no CI and an older shell's batch as no status at all", () => {
    expect(resolvePullRequestListStatus({ batch: batch(2_000, { ci: null }) }).ci).toBeNull();
    // Fields absent: the batch says nothing about state or CI, so the live read stands.
    const legacy = resolvePullRequestListStatus({
      live: live(1_000),
      batch: batch(2_000, {
        extended: false,
        state: undefined,
        isDraft: undefined,
        ci: undefined,
        ciHeadSha: undefined,
        headSha: H1,
      }),
    });
    expect(legacy).toMatchObject({ state: "OPEN", headSha: H1 });
    expect(legacy.ci).toEqual({ source: "live", checks: passingChecks });
  });

  it("keeps an older host's live checks when no read knows the head", () => {
    const status = resolvePullRequestListStatus({ live: live(1_000, { head_sha: undefined }) });
    expect(status.ci).toEqual({ source: "live", checks: passingChecks });
  });
});
