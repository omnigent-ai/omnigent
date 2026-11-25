// The details a stack reads ahead, with the details read's own options: their staleness is a
// function of the query, which must be worked out before telling fresh from stale.

import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import { pullRequestInfoReadAheadOptions } from "@/newPullRequestPage/hooks/usePullRequests";

import { stackWarmupReads } from "./usePullRequestStackWarmup";

const url = "https://github.com/acme/app/pull/100";
const reads = {
  known: () => true,
  readsOnThisMac: () => true,
  blocked: () => false,
  target: () => ({
    provider: "github" as const,
    host: "github.com",
    repository: "acme/app",
    number: 100,
  }),
  scope: () => ["mac-read", "user", "github", "github.com"],
} as unknown as Parameters<typeof stackWarmupReads>[1]["reads"];

function detailsRead(client: QueryClient) {
  return stackWarmupReads([url], {
    queryClient: client,
    reads,
    sessionFor: () => "pull-requests-page",
    sessionless: () => true,
  })[0]!;
}

describe("reading a member's details ahead", () => {
  const options = pullRequestInfoReadAheadOptions("pull-requests-page", url, reads, true)!;

  it("goes by the details read's own staleness, a function of the query", () => {
    expect(typeof options.staleTime).toBe("function");
  });

  it("needs no request for details read moments ago", () => {
    const client = new QueryClient();
    client.setQueryData(options.queryKey, { pr: { url } } as never);
    expect(detailsRead(client).settled()).toBe(true);
  });

  it("needs one for details never read, read too long ago, or invalidated", async () => {
    const cold = new QueryClient();
    expect(detailsRead(cold).settled()).toBe(false);
    const old = new QueryClient();
    old.setQueryData(options.queryKey, { pr: { url } } as never, {
      updatedAt: Date.now() - 31_000,
    });
    expect(detailsRead(old).settled()).toBe(false);
    const invalidated = new QueryClient();
    invalidated.setQueryData(options.queryKey, { pr: { url } } as never);
    await invalidated.invalidateQueries({ queryKey: options.queryKey, refetchType: "none" });
    expect(detailsRead(invalidated).settled()).toBe(false);
  });
});
