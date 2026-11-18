// The PR panel's poll for a session whose PRs the server
// tracks: 10 s while there is something to watch, 30 s for an open PR whose checks settled,
// and resting once it is merged or closed, since each poll is a full PR read.

import { describe, expect, it } from "vitest";
import {
  computePanelPullRequestPollInterval,
  computePullRequestPollInterval,
  type PullRequestChecks,
  type PullRequestInfo,
} from "@/hooks/usePullRequests";

const checks = (over: Partial<PullRequestChecks> = {}): PullRequestChecks => ({
  passing: 0,
  failing: 0,
  pending: 0,
  total: 0,
  runs: [],
  ...over,
});
// A fully-resolved, ready session: git repo + gh + auth + repo + open PR.
const ready = (over: Partial<PullRequestInfo> = {}): PullRequestInfo => ({
  object: "session.github.info",
  available: true,
  gh_available: true,
  authenticated: true,
  branch: "feature",
  repo: { name_with_owner: "o/r" },
  base_ref: "main",
  pr: {
    number: 1,
    title: "t",
    state: "OPEN",
    url: "u",
    is_draft: false,
    author: null,
    base_ref: "main",
    head_ref: "feature",
    checks: checks({ total: 2, passing: 2 }),
  },
  ...over,
});
const tracked = (pr: Partial<NonNullable<PullRequestInfo["pr"]>> = {}): PullRequestInfo =>
  ready({ tracking_available: true, pr: { ...ready().pr!, ...pr } });

describe("computePanelPullRequestPollInterval", () => {
  it("polls a tracked PR every 10 s while its checks run", () => {
    expect(computePanelPullRequestPollInterval(tracked({ checks: checks({ total: 2, pending: 1 }) }))).toBe(
      10_000,
    );
    expect(computePanelPullRequestPollInterval(tracked({ checks: checks() }))).toBe(10_000);
    expect(computePanelPullRequestPollInterval(ready({ tracking_available: true, pr: null }))).toBe(
      10_000,
    );
  });

  it("keeps watching an open tracked PR whose checks settled for outside pushes, reviews and merges", () => {
    expect(computePanelPullRequestPollInterval(tracked())).toBe(30_000);
    expect(computePanelPullRequestPollInterval(tracked({ checks: checks({ total: 2, failing: 2 }) }))).toBe(
      30_000,
    );
  });

  it("rests a tracked PR that is merged or closed", () => {
    expect(computePanelPullRequestPollInterval(tracked({ state: "MERGED" }))).toBe(false);
    expect(computePanelPullRequestPollInterval(tracked({ state: "CLOSED" }))).toBe(false);
  });

  it("polls an untracked session as before", () => {
    const pending = ready({ pr: { ...ready().pr!, checks: checks({ total: 2, pending: 1 }) } });
    for (const info of [undefined, ready(), pending, ready({ pr: null })]) {
      expect(computePanelPullRequestPollInterval(info)).toBe(computePullRequestPollInterval(info));
    }
    expect(computePanelPullRequestPollInterval(pending)).toBe(5_000);
  });
});
