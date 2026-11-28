// The host's branch list as the branch's PR, a question or none, and the user's choice.

import { afterEach, describe, expect, it, vi } from "vitest";

import type { PullRequestInfo } from "@/newPullRequestPage/hooks/usePullRequests";
import type { CodeHostContext } from "@/lib/codeHostContext";
import type * as IdentityModule from "@/lib/identity";
import {
  type BranchCandidate,
  type BranchDiscovery,
  branchChoiceCommands,
  chooseBranchPullRequest,
  contextRepositories,
  parseBranchPullRequests,
  resetBranchChoicesForTests,
  withBranchChoice,
  withBranchOnStoredList,
  withBranchPullRequest,
} from "./branchPullRequest";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));

type Ok = Extract<CodeHostContext, { status: "ok" }>;
const HEAD = "a".repeat(40);

function context(overrides: Partial<Ok> = {}): Ok {
  return {
    status: "ok",
    context_key: "ctx-1",
    branch: "feature",
    head_sha: HEAD,
    push: {
      remote_name: "fork",
      branch: "feature",
      host: "github.com",
      owner: "me",
      repo: "app",
      tracking_sha: HEAD,
    },
    push_unresolved: null,
    remotes: [
      { name: "origin", host: "github.com", owner: "acme", repo: "app" },
      { name: "fork", host: "github.com", owner: "me", repo: "app" },
    ],
    unsupported_remote_count: 0,
    truncated_remotes: false,
    ...overrides,
  };
}

const candidate = (number: number): BranchCandidate => ({
  url: `https://github.com/acme/app/pull/${number}`,
  host: "github.com",
  number,
  title: `PR ${number}`,
  state: "OPEN",
  is_draft: false,
  base_repository: "acme/app",
});

/** A row as the host lists it. */
const row = (number: number, overrides: Record<string, unknown> = {}) => ({
  url: `https://github.com/acme/app/pull/${number}`,
  number,
  title: `PR ${number}`,
  state: "OPEN",
  is_draft: false,
  updated_at: "2026-01-01T00:00:00Z",
  head_ref: "feature",
  ...overrides,
});

const KEY = { sessionId: "conv_1", branch: "feature" };

afterEach(() => resetBranchChoicesForTests());

describe("parseBranchPullRequests", () => {
  it("reads one listed PR as the branch's", () => {
    expect(parseBranchPullRequests({ object: "list", data: [row(7)], has_more: false })).toEqual({
      status: "found",
      evidence: "branch",
      pull_request: candidate(7),
    });
  });

  it("asks which of several, and says when older ones weren't listed", () => {
    const several = parseBranchPullRequests({ data: [row(7), row(8)], has_more: false });
    expect(several).toMatchObject({ status: "ambiguous", incomplete: false });
    // One listed with more past the cap can't claim to be the only one.
    expect(parseBranchPullRequests({ data: [row(7)], has_more: true })).toEqual({
      status: "ambiguous",
      candidates: [candidate(7)],
      incomplete: true,
    });
  });

  it("reads an Enterprise host's PR, and drops malformed rows", () => {
    const enterprise = parseBranchPullRequests({
      data: [
        row(3, { url: "https://code.example.com/acme/app/pull/3", is_draft: true }),
        row(4, { state: "DRAFT" }),
        { url: "not a link", state: "OPEN" },
      ],
    });
    expect(enterprise).toMatchObject({
      status: "found",
      pull_request: { host: "code.example.com", number: 3, is_draft: true },
    });
  });

  it("reads an empty or malformed answer as none", () => {
    expect(parseBranchPullRequests({ data: [] })).toEqual({ status: "none", chosen: false });
    expect(parseBranchPullRequests(null)).toEqual({ status: "none", chosen: false });
  });
});

describe("withBranchChoice", () => {
  const ambiguous = parseBranchPullRequests({ data: [row(7), row(8)] });

  it("applies the choice for the session's branch, for this run", () => {
    expect(withBranchChoice(ambiguous, KEY)).toBe(ambiguous);
    chooseBranchPullRequest(KEY, { url: candidate(8).url });
    expect(withBranchChoice(ambiguous, KEY)).toEqual({
      status: "found",
      evidence: "choice",
      pull_request: candidate(8),
    });
    // Another branch of the same session asks again.
    expect(withBranchChoice(ambiguous, { ...KEY, branch: "other" })).toBe(ambiguous);
    chooseBranchPullRequest(KEY, { none: true });
    expect(withBranchChoice(ambiguous, KEY)).toEqual({ status: "none", chosen: true });
    chooseBranchPullRequest(KEY, null);
    expect(withBranchChoice(ambiguous, KEY)).toBe(ambiguous);
  });

  it("asks again once the chosen PR is gone", () => {
    chooseBranchPullRequest(KEY, { url: "https://github.com/acme/app/pull/1" });
    expect(withBranchChoice(ambiguous, KEY)).toBe(ambiguous);
  });
});

describe("withBranchPullRequest", () => {
  const discovery = (result: BranchDiscovery["result"]): BranchDiscovery => ({
    choiceKey: KEY,
    result,
  });
  const info = (overrides: Partial<PullRequestInfo> = {}): PullRequestInfo => ({
    object: "session.github.info",
    available: true,
    prs: [
      {
        url: "https://github.com/acme/app/pull/99",
        host: "github.com",
        repository: "acme/app",
        number: 99,
        relationship: "inferred",
      },
      {
        url: "https://github.com/acme/app/pull/5",
        host: "github.com",
        repository: "acme/app",
        number: 5,
        relationship: "attached",
      },
    ],
    selected_pr_url: "https://github.com/acme/app/pull/99",
    ...overrides,
  });

  it("replaces the PR the session info inferred and selects the one found", () => {
    const merged = withBranchPullRequest(
      info(),
      discovery({ status: "found", evidence: "branch", pull_request: candidate(7) }),
    );
    expect(merged.prs?.map((pr) => pr.url)).toEqual([
      "https://github.com/acme/app/pull/7",
      "https://github.com/acme/app/pull/5",
    ]);
    expect(merged.selected_pr_url).toBe("https://github.com/acme/app/pull/7");
    expect(merged.prs?.[0]).toMatchObject({ relationship: "inferred", provider: "github" });
  });

  it("keeps a selected linked PR, and drops the inferred one when nothing was found", () => {
    const selectedLinked = withBranchPullRequest(
      info({ selected_pr_url: "https://github.com/acme/app/pull/5" }),
      discovery({ status: "found", evidence: "branch", pull_request: candidate(7) }),
    );
    expect(selectedLinked.selected_pr_url).toBe("https://github.com/acme/app/pull/5");
    const none = withBranchPullRequest(info(), discovery({ status: "none", chosen: false }));
    expect(none.prs?.map((pr) => pr.number)).toEqual([5]);
    expect(none.selected_pr_url).toBe("https://github.com/acme/app/pull/5");
  });
});

describe("branch choice commands and context repositories", () => {
  it("offers each candidate and none, or choosing again after none", () => {
    const ambiguous = branchChoiceCommands({
      choiceKey: KEY,
      result: { status: "ambiguous", candidates: [candidate(1)], incomplete: false },
    });
    expect(ambiguous.map((command) => command.paletteLabel)).toEqual([
      "Choose #1 as this branch’s pull request",
      "This branch has none of these pull requests",
    ]);
    const again = branchChoiceCommands({
      choiceKey: KEY,
      result: { status: "none", chosen: true },
    });
    expect(again.map((command) => command.choice)).toEqual([null]);
  });

  it("lists the context's repositories on known hosts only", () => {
    const known = (host: string) => (host === "github.com" ? "github" : null);
    expect(
      contextRepositories(
        context({
          remotes: [
            { name: "origin", host: "github.com", owner: "acme", repo: "app" },
            { name: "lab", host: "gitlab.com", owner: "acme", repo: "app" },
          ],
        }),
        known,
      ),
    ).toEqual([
      { provider: "github", host: "github.com", repository: "me/app" },
      { provider: "github", host: "github.com", repository: "acme/app" },
    ]);
    expect(contextRepositories({ status: "unsupported" }, known)).toEqual([]);
  });

  it("keeps a repository whose host is still being checked, with no provider yet", () => {
    expect(
      contextRepositories(context({ push: null }), (host) =>
        host === "github.com" ? "pending" : null,
      ),
    ).toEqual([
      { provider: null, host: "github.com", repository: "acme/app" },
      { provider: null, host: "github.com", repository: "me/app" },
    ]);
  });
});

describe("withBranchOnStoredList", () => {
  const stored = [
    {
      url: "https://github.com/acme/app/pull/99",
      host: "github.com",
      repository: "acme/app",
      number: 99,
      relationship: "inferred" as const,
    },
    {
      url: "https://github.com/acme/app/pull/5",
      host: "github.com",
      repository: "acme/app",
      number: 5,
      relationship: "attached" as const,
    },
  ];
  const found = {
    url: "https://github.com/acme/app/pull/7",
    host: "github.com",
    repository: "acme/app",
    number: 7,
    relationship: "inferred" as const,
    found_for_branch: true,
  };

  it("puts the branch PR the host found first, in place of the one the session info inferred", () => {
    expect(
      withBranchOnStoredList(stored, { branch_looked_up: true, prs: [found] }).map(
        (pr) => pr.number,
      ),
    ).toEqual([7, 5]);
  });

  it("drops the inferred one even when the host found none", () => {
    expect(
      withBranchOnStoredList(stored, { branch_looked_up: true, prs: [] }).map((pr) => pr.number),
    ).toEqual([5]);
  });

  it("leaves the stored list alone until the host's lookup answers", () => {
    expect(withBranchOnStoredList(stored, { prs: [found] })).toBe(stored);
  });
});
