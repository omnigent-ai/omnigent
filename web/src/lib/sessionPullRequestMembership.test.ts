import { describe, expect, it } from "vitest";

import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";
import { pullRequestKey } from "@/lib/pullRequestReference";

import {
  MAX_SESSION_PULL_REQUESTS,
  linksPullRequest,
  sessionPullRequestMembership,
  sessionPullRequestsFull,
} from "./sessionPullRequestMembership";

const url = (number: number) => `https://github.com/acme/app/pull/${number}`;
const row = (
  number: number,
  relationship: PullRequestAssociation["relationship"] = "attached",
): Pick<PullRequestAssociation, "url" | "relationship"> => ({ url: url(number), relationship });

describe("sessionPullRequestMembership", () => {
  it("trusts a synced stored list over a live payload of the host's raw rows", () => {
    // A server that couldn't store the host's report answers with its raw rows.
    const membership = sessionPullRequestMembership({
      stored: { prs: [row(1), row(2)], synced: true },
      live: [row(1, "worked_on"), row(2, "worked_on"), row(3, "worked_on")],
    });
    expect(membership.authoritative).toBe(true);
    expect(linksPullRequest(membership, url(1))).toBe(true);
    expect(linksPullRequest(membership, url(3))).toBe(false);
  });

  it("drops a PR unlinked elsewhere even while a stale live payload still lists it", () => {
    const membership = sessionPullRequestMembership({
      stored: { prs: [row(1)], synced: true },
      live: [row(1), row(2)],
    });
    expect(linksPullRequest(membership, url(2))).toBe(false);
  });

  it("keeps a stored PR the live payload hasn't caught up with", () => {
    const membership = sessionPullRequestMembership({
      stored: { prs: [row(1), row(2)], synced: true },
      live: [row(1)],
    });
    expect(linksPullRequest(membership, url(2))).toBe(true);
  });

  it.each([
    { case: "the stored list isn't synced", stored: { prs: [row(1)], synced: false } },
    { case: "the stored list hasn't answered", stored: undefined },
  ])("joins the live payload and the stored list while $case", ({ stored }) => {
    const membership = sessionPullRequestMembership({ stored, live: [row(2)] });
    expect(membership.authoritative).toBe(false);
    expect(linksPullRequest(membership, url(2))).toBe(true);
    expect(linksPullRequest(membership, url(1))).toBe(stored !== undefined);
  });

  it("keeps a synced list the truth when its refresh failed and the live rows disagree", () => {
    // React Query keeps the last good list through a failed refetch; an unlink removed #2.
    const membership = sessionPullRequestMembership({
      stored: { prs: [row(1)], synced: true },
      live: [row(1), row(2)],
    });
    expect(membership.authoritative).toBe(true);
    expect(linksPullRequest(membership, url(2))).toBe(false);
  });

  it("never counts a PR only inferred from the branch", () => {
    const membership = sessionPullRequestMembership({
      stored: { prs: [row(1), row(5, "inferred")], synced: true },
      live: [row(5, "inferred")],
    });
    expect(linksPullRequest(membership, url(5))).toBe(false);
    expect(membership.keys.size).toBe(1);
  });

  it("matches a PR however its link is written", () => {
    const membership = sessionPullRequestMembership({
      stored: { prs: [row(1)], synced: true },
      live: undefined,
    });
    expect(linksPullRequest(membership, "https://GitHub.com/Acme/App/pull/1/")).toBe(true);
    // The same repository and number on another host is a different PR.
    expect(linksPullRequest(membership, "https://ghe.example.com/acme/app/pull/1")).toBe(false);
  });

  it("keeps this Mac's own links, which the stored list carries", () => {
    const deviceHost = "https://2.example-host.example.com/org/repo/pull/20002346";
    const membership = sessionPullRequestMembership({
      stored: { prs: [{ url: deviceHost, relationship: "attached" }], synced: true },
      live: [],
    });
    expect(linksPullRequest(membership, deviceHost)).toBe(true);
  });
});

describe("sessionPullRequestsFull", () => {
  const linking = (count: number) =>
    sessionPullRequestMembership({
      stored: {
        prs: Array.from({ length: count }, (_, index) => row(index + 1)),
        synced: true,
      },
      live: undefined,
    });

  it("has room for the 50th PR", () => {
    expect(sessionPullRequestsFull(linking(MAX_SESSION_PULL_REQUESTS - 1), url(999))).toBe(false);
  });

  it("has no room for a 51st distinct PR", () => {
    expect(sessionPullRequestsFull(linking(MAX_SESSION_PULL_REQUESTS), url(999))).toBe(true);
  });

  it("isn't full for a PR it already links at 50", () => {
    expect(sessionPullRequestsFull(linking(MAX_SESSION_PULL_REQUESTS), url(1))).toBe(false);
  });

  it("counts neither inferred rows nor the host's raw rows toward the limit", () => {
    const membership = sessionPullRequestMembership({
      stored: {
        prs: [
          ...Array.from({ length: MAX_SESSION_PULL_REQUESTS - 1 }, (_, index) => row(index + 1)),
          row(500, "inferred"),
        ],
        synced: true,
      },
      live: Array.from({ length: 60 }, (_, index) => row(index + 100, "worked_on")),
    });
    expect(sessionPullRequestsFull(membership, url(999))).toBe(false);
  });
});

describe("sessionPullRequestMembership, with pending intents", () => {
  const stored = { prs: [row(1), row(2)], synced: true };

  it("shows a pending link and hides a pending unlink before the server's list does", () => {
    const membership = sessionPullRequestMembership({
      stored,
      live: undefined,
      intents: {
        [pullRequestKey(url(3))]: { action: "link" },
        [pullRequestKey(url(2))]: { action: "unlink" },
      },
    });
    expect(linksPullRequest(membership, url(3))).toBe(true);
    expect(linksPullRequest(membership, url(2))).toBe(false);
    expect(linksPullRequest(membership, url(1))).toBe(true);
  });

  it("counts a pending link toward the limit", () => {
    const full = sessionPullRequestMembership({
      stored: {
        prs: Array.from({ length: MAX_SESSION_PULL_REQUESTS - 1 }, (_, index) => row(index + 1)),
        synced: true,
      },
      live: undefined,
      intents: { [pullRequestKey(url(900))]: { action: "link" } },
    });
    expect(sessionPullRequestsFull(full, url(901))).toBe(true);
  });
});
