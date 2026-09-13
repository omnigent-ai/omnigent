// Tests for the pure helpers in useGithub — the 404 → reason classifier that
// steers an outdated host to the "update your host" panel state, and the
// panel's poll-interval decision.

import { describe, expect, it } from "vitest";
import {
  computeGithubPollInterval,
  githubNotFoundReason,
  type GithubChecks,
  type GithubInfo,
} from "@/hooks/useGithub";

describe("githubNotFoundReason", () => {
  it("flags an outdated host from its 'resource not found' message", () => {
    // The exact shape an old runner's generic resource lookup returns.
    expect(githubNotFoundReason("Resource 'github' not found")).toBe("host_outdated");
    // Case-insensitive, tolerant of quoting.
    expect(githubNotFoundReason("resource github not found")).toBe("host_outdated");
  });

  it("treats every other 404 as a generic no-workspace reason", () => {
    expect(githubNotFoundReason("workspace directory does not exist on host")).toBe("no_os_env");
    expect(githubNotFoundReason("Resource 'terminal' not found")).toBe("no_os_env");
    expect(githubNotFoundReason(undefined)).toBe("no_os_env");
    expect(githubNotFoundReason("")).toBe("no_os_env");
  });
});

describe("computeGithubPollInterval", () => {
  const checks = (over: Partial<GithubChecks> = {}): GithubChecks => ({
    passing: 0,
    failing: 0,
    pending: 0,
    total: 0,
    runs: [],
    ...over,
  });
  // A fully-resolved, ready session: git repo + gh + auth + repo + open PR.
  const ready = (over: Partial<GithubInfo> = {}): GithubInfo => ({
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

  it("polls setup/availability states the user fixes outside the app", () => {
    // No info yet (initial error / transient failure with no cache).
    expect(computeGithubPollInterval(undefined)).toBe(5_000);
    // Not a git repo / no workspace / outdated host.
    expect(computeGithubPollInterval(ready({ available: false, pr: null }))).toBe(5_000);
    // gh not installed.
    expect(computeGithubPollInterval(ready({ gh_available: false, pr: null }))).toBe(5_000);
    // Not authenticated (the `gh auth switch` / `gh auth login` prompt).
    expect(computeGithubPollInterval(ready({ authenticated: false, pr: null }))).toBe(5_000);
    // Repo unresolved (no upstream).
    expect(computeGithubPollInterval(ready({ repo: null, pr: null }))).toBe(5_000);
    expect(computeGithubPollInterval(ready({ repo: { name_with_owner: null }, pr: null }))).toBe(
      5_000,
    );
  });

  it("polls while waiting for a PR and while an open PR's checks are unsettled", () => {
    // Set up, no PR yet — waiting for one to appear.
    expect(computeGithubPollInterval(ready({ pr: null }))).toBe(5_000);
    // Open PR, checks running.
    expect(
      computeGithubPollInterval(ready({ pr: { ...ready().pr!, checks: checks({ pending: 1 }) } })),
    ).toBe(5_000);
    // Open PR, checks not registered yet (total 0).
    expect(computeGithubPollInterval(ready({ pr: { ...ready().pr!, checks: checks() } }))).toBe(
      5_000,
    );
  });

  it("rests once there is nothing left to watch", () => {
    // Open PR, all checks settled.
    expect(computeGithubPollInterval(ready())).toBe(false);
    // Merged / closed PR — terminal.
    expect(computeGithubPollInterval(ready({ pr: { ...ready().pr!, state: "MERGED" } }))).toBe(
      false,
    );
    expect(computeGithubPollInterval(ready({ pr: { ...ready().pr!, state: "CLOSED" } }))).toBe(
      false,
    );
  });
});
