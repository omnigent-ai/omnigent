// Check Again on the Pull Requests page reading as its own login: the page shows only reads as
// that login, so the check starts its own read as the account that acts, and once it settles
// the change, the page's view reads the PR again as its login.

import { QueryClient, QueryObserver } from "@tanstack/react-query";
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
import { macNamedReadScopeKey, resetMacReadScopesForTests } from "@/lib/macReadScope";
import { bindMacReadScopes } from "@/lib/macReadScopeQueries";
import { installMacWriteShell, removeMacWriteShell } from "@/test/macWriteShell";

import { confirmFreshPullRequestRead, readFreshPullRequest } from "./freshPullRequestRead";

const PR = "https://github.com/acme/app/pull/42";
const PAGE = "pull-requests-page";
const HEAD = "a".repeat(40);
const BASE = "b".repeat(40);

const OVERVIEW = {
  repository: "acme/app",
  pr: {
    number: 42,
    title: "Ship it",
    state: "OPEN",
    url: PR,
    is_draft: false,
    author: "amy",
    base_ref: "main",
    head_ref: "feature",
    head_sha: HEAD,
    base_sha: BASE,
    body: null,
    checks: { passing: 1, failing: 0, pending: 0, total: 1, runs: [] },
    checks_supported: true,
  },
};

const unbinds: (() => void)[] = [];

afterEach(() => {
  for (const unbind of unbinds.splice(0)) unbind();
  writeMacWritesIntent("automatic");
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacReadScopesForTests();
  removeMacWriteShell();
});

describe("Check Again with only the page's own reads of the PR", () => {
  it("reads once as the acting account, settles the change, and refreshes the page's view", async () => {
    const { query } = await installMacWriteShell(PR, async () => ({}), {
      pull_request_overview: OVERVIEW,
    });
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    unbinds.push(bindMacReadScopes(client));
    // The page's detail, read as bob; no read as the acting account exists yet.
    const namedKey = [
      "github-info",
      PAGE,
      PR,
      ...macNamedReadScopeKey("github.com", "bob", "user-1"),
    ];
    const namedRead = vi.fn(async () => ({ as: "bob", state: "OPEN" }));
    client.setQueryData(namedKey, { as: "bob", state: "DRAFT" });
    const observer = new QueryObserver(client, {
      queryKey: namedKey,
      queryFn: namedRead,
      staleTime: Infinity,
    });
    unbinds.push(observer.subscribe(() => {}));
    query.mockClear();

    const result = await confirmFreshPullRequestRead(client, PAGE, PR, {
      login: "mac-octo",
      reader: null,
    });

    expect(result).toBe("confirmed");
    const overviewReads = query.mock.calls.filter(
      ([request]) => (request as { operation: string }).operation === "pull_request_overview",
    );
    expect(overviewReads).toHaveLength(1);
    expect(overviewReads[0]![0]).toMatchObject({ account: "mac" });
    expect(overviewReads[0]![0]).not.toHaveProperty("macLogin");
    // The page's view, read as its own login, reads the PR again and shows the new state.
    await vi.waitFor(() =>
      expect(client.getQueryData(namedKey)).toEqual({ as: "bob", state: "OPEN" }),
    );
    expect(namedRead).toHaveBeenCalledTimes(1);
  });

  it("says where the read it made found the PR, its draft flag too", async () => {
    await installMacWriteShell(PR, async () => ({}), {
      pull_request_overview: {
        ...OVERVIEW,
        pr: { ...OVERVIEW.pr, state: "CLOSED", is_draft: true },
      },
    });
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    expect(
      await readFreshPullRequest(client, PAGE, PR, { login: "mac-octo", reader: null }),
    ).toEqual({
      result: "confirmed",
      facts: { state: "CLOSED", isDraft: true, reviewers: null, autoMerge: "unknown" },
    });
    expect(
      await readFreshPullRequest(client, PAGE, PR, { login: "someone-else", reader: null }),
    ).toEqual({ result: "stale", facts: null });
  });

  it.each([
    { case: "none asked", reviewers: [], read: [] },
    {
      case: "a user and a team",
      reviewers: [
        { login: "amy", kind: "user", state: "PENDING" },
        { login: "core", kind: "team", state: "PENDING" },
      ],
      read: [
        { login: "amy", kind: "user", state: "PENDING" },
        { login: "core", kind: "team", state: "PENDING" },
      ],
    },
    // A list with any reviewer it can't read can't say who is still asked.
    { case: "one without a kind", reviewers: [{ login: "amy", state: "PENDING" }], read: null },
    {
      case: "one of a kind it doesn't know",
      reviewers: [
        { login: "amy", kind: "user", state: "PENDING" },
        { login: "bot", kind: "app", state: "PENDING" },
      ],
      read: null,
    },
    { case: "one that isn't an object", reviewers: [null], read: null },
  ])("reads the PR's reviewers: $case", async ({ reviewers, read }) => {
    await installMacWriteShell(PR, async () => ({}), {
      pull_request_overview: { ...OVERVIEW, pr: { ...OVERVIEW.pr, reviewers } },
    });
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const fresh = await readFreshPullRequest(client, PAGE, PR, { login: "mac-octo", reader: null });
    expect(fresh.facts?.reviewers).toEqual(read);
  });

  it.each([
    {
      case: "on",
      pr: { auto_merge: { merge_method: "SQUASH", enabled_by: "amy", enabled_at: null } },
      read: "on",
    },
    { case: "off", pr: { auto_merge: null }, read: "off" },
    // An older shell's overview has no auto-merge: that isn't off.
    { case: "left out", pr: {}, read: "unknown" },
    { case: "garbled", pr: { auto_merge: "on" }, read: "unknown" },
  ])("reads the PR's auto-merge: $case", async ({ pr, read }) => {
    await installMacWriteShell(PR, async () => ({}), {
      pull_request_overview: { ...OVERVIEW, pr: { ...OVERVIEW.pr, ...pr } },
    });
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const fresh = await readFreshPullRequest(client, PAGE, PR, { login: "mac-octo", reader: null });
    expect(fresh.facts?.autoMerge).toBe(read);
  });

  it("settles nothing, and refreshes nothing, when the acting account changed", async () => {
    const { query } = await installMacWriteShell(PR, async () => ({}), {
      pull_request_overview: OVERVIEW,
    });
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    query.mockClear();
    expect(
      await confirmFreshPullRequestRead(client, PAGE, PR, { login: "someone-else", reader: null }),
    ).toBe("stale");
    expect(query).not.toHaveBeenCalled();
  });
});
