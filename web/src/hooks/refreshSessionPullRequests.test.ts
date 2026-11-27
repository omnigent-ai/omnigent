import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { readsFresh } from "@/lib/pullRequestFreshReads";

import { refreshSessionPullRequests } from "./refreshSessionPullRequests";

const mocks = vi.hoisted(() => ({
  clearRecovery: vi.fn(),
  fastTrack: vi.fn(() => Promise.resolve([])),
  mergeReadiness: vi.fn(() => Promise.resolve()),
}));
vi.mock("@/lib/hostTransportRecovery", () => ({
  clearSessionHostTransportRecovery: mocks.clearRecovery,
}));
vi.mock("@/hooks/usePullRequestCommits", () => ({
  refreshPullRequestFastTrack: mocks.fastTrack,
}));
vi.mock("@/hooks/useMergeReadiness", () => ({
  refreshMergeReadiness: mocks.mergeReadiness,
}));

describe("refreshSessionPullRequests", () => {
  it("refetches the session's pull request queries, the shared stats and stacks", async () => {
    const queryClient = new QueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    await refreshSessionPullRequests(queryClient, "conv_1");

    expect(mocks.clearRecovery).toHaveBeenCalledWith("conv_1");
    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      // The session's PR reads go by one predicate.
      undefined,
      ["session-pull-requests", "conv_1"],
      // Every stack's members and GitHub's own stacks, which aren't the session's own queries.
      ["pull-request-stack-members"],
      ["pull-request-native-stack"],
    ]);
    expect(mocks.fastTrack).toHaveBeenCalledWith(queryClient);
    expect(mocks.mergeReadiness).toHaveBeenCalledWith(queryClient);
  });

  it("reads again the session's own PR reads, and not another session's", async () => {
    const queryClient = new QueryClient();
    const linked = "https://github.com/acme/app/pull/7";
    const keys = {
      own: ["pr-page-github-changed-files", "conv_1", linked, "base:head"],
      otherSession: ["pr-page-github-changed-files", "conv_2", linked, "base:head"],
      notRead: ["merge-checks", "conv_1", linked],
    };
    for (const key of Object.values(keys)) queryClient.setQueryData(key, {});

    await refreshSessionPullRequests(queryClient, "conv_1");

    const invalidated = (key: readonly unknown[]) =>
      queryClient.getQueryState(key)?.isInvalidated === true;
    expect(invalidated(keys.own)).toBe(true);
    expect(invalidated(keys.otherSession)).toBe(false);
    expect(invalidated(keys.notRead)).toBe(false);
  });

  it("reads every pull request fresh while it runs, and not after", async () => {
    const queryClient = new QueryClient();
    const during: boolean[] = [];
    mocks.mergeReadiness.mockImplementationOnce(async () => {
      during.push(readsFresh("https://github.com/acme/app/pull/7"));
    });
    await refreshSessionPullRequests(queryClient, "conv_1");
    expect(during).toEqual([true]);
    expect(readsFresh("https://github.com/acme/app/pull/7")).toBe(false);
  });
});
