// Check Again before this Mac's read scope has a reader, on a shell that proves who read each
// answer: the check's own read proves the reader and goes into that reader's record, which settles
// the change only where the reader is the account that acted, in the epoch the read was sent in.

import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
}));

import {
  GITHUB_WRITE_SET_V1,
  resetDesktopPullRequestWritesForTests,
  setPageWriteOperationsForTests,
  writeMacWritesIntent,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import {
  acceptMacRead,
  acceptedMacReader,
  advanceMacReadScope,
  beginMacRead,
  macReadScopeKey,
  resetMacReadScopesForTests,
} from "@/lib/macReadScope";
import { installMacWriteShell, MAC_IDENTITY, removeMacWriteShell } from "@/test/macWriteShell";

import { readFreshPullRequest } from "./freshPullRequestRead";

const PR = "https://github.com/acme/app/pull/42";
const PAGE = "pull-requests-page";
const scope = () => macReadScopeKey("github", "github.com", "user-1");

const OVERVIEW = {
  repository: "acme/app",
  pr: {
    number: 42,
    title: "Ship it",
    state: "MERGED",
    url: PR,
    is_draft: false,
    author: "amy",
    base_ref: "main",
    head_ref: "feature",
    head_sha: "a".repeat(40),
    base_sha: "b".repeat(40),
    body: null,
    checks: { passing: 1, failing: 0, pending: 0, total: 1, runs: [] },
    checks_supported: true,
  },
};

/** A proving shell whose scope has no reader yet, though main names the Mac's account. */
async function bootstrapShell(viewer: () => string = () => MAC_IDENTITY.login) {
  const shell = await installMacWriteShell(
    PR,
    async () => ({}),
    { pull_request_overview: OVERVIEW },
    { proves: true, viewer },
  );
  setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
  resetMacReadScopesForTests();
  expect(acceptedMacReader(scope())).toBeNull();
  return shell;
}

const check = (client: QueryClient) =>
  readFreshPullRequest(client, PAGE, PR, { login: MAC_IDENTITY.login, reader: null });

afterEach(() => {
  writeMacWritesIntent("automatic");
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacReadScopesForTests();
  removeMacWriteShell();
});

describe("Check Again with no proven reader yet", () => {
  it("settles the change from its own read, which proved the account that acted", async () => {
    await bootstrapShell();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    expect(await check(client)).toEqual({
      result: "confirmed",
      facts: { state: "MERGED", isDraft: false, reviewers: null, autoMerge: "unknown" },
    });
    expect(acceptedMacReader(scope())).toBe(MAC_IDENTITY.login);
  });

  it("says nothing when its read proved another account than the one that acted", async () => {
    await bootstrapShell(() => "bob");
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    expect((await check(client)).result).toBe("stale");
  });

  it("says nothing when the scope moved after its answer, before the check finished", async () => {
    await bootstrapShell();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // As the answer goes into the reader's record, this Mac's account changes.
    const stop = client.getQueryCache().subscribe((event) => {
      if (event.type === "updated" && event.query.queryKey.includes(MAC_IDENTITY.login)) {
        stop();
        advanceMacReadScope(scope());
      }
    });
    expect((await check(client)).result).toBe("stale");
  });

  it.each([
    ["sent before it", "before"],
    ["sent while it reads", "during"],
  ] as const)(
    "settles nothing when a list read %s proves Bob the first reader while it reads as the actor",
    async (_when, sent) => {
      const { query } = await bootstrapShell();
      const answer = query.getMockImplementation()!;
      const listRead = sent === "before" ? beginMacRead(scope()) : null;
      query.mockImplementation(async (request) => {
        if ((request as { operation: string }).operation === "pull_request_overview") {
          acceptMacRead(listRead ?? beginMacRead(scope()), "bob");
        }
        return answer(request);
      });
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      expect((await check(client)).result).not.toBe("confirmed");
    },
  );

  it("settles nothing when the reader changed and changed back while it read", async () => {
    const { query } = await bootstrapShell();
    const answer = query.getMockImplementation()!;
    query.mockImplementation(async (request) => {
      acceptMacRead(beginMacRead(scope()), "bob");
      acceptMacRead(beginMacRead(scope()), MAC_IDENTITY.login);
      return answer(request);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    expect((await check(client)).result).not.toBe("confirmed");
  });
});
