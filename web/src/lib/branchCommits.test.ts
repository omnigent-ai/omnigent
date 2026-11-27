import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ComparisonField,
  ComparisonKind,
  ComparisonProblem,
  type ListBranchCommitsResponse,
  type WorkingCopyBase,
} from "@/lib/workspaceSidebarApi";
import {
  DEFAULT_WORKSPACE_SIDEBAR_SOURCE,
  setWorkspaceSidebarSourceForTests,
  WorkspaceMethodUnavailableError,
} from "@/lib/workspaceSidebarSource";
import { commitRevisions, type RevisionSpec } from "@/lib/customComparison";

import {
  BRANCH_COMMITS_FAILED,
  BRANCH_COMMITS_NOT_SERVED,
  BRANCH_RANGE,
  branchCommitsView,
  commitsRange,
  fetchBranchCommits,
} from "./branchCommits";

const SESSION = "conv_commits";
const OID = "a".repeat(40);

const BASE: WorkingCopyBase = {
  kind: ComparisonKind.REVISIONS,
  head: "h".repeat(40),
  base: "b".repeat(40),
  reference: "origin/main",
  referenceCommit: "r".repeat(40),
  mergeBase: "b".repeat(40),
  baseDisplay: "origin/main",
  headDisplay: "HEAD",
  headIsWorkingCopy: false,
};

function answering(listBranchCommits: (sessionId: string, range: RevisionSpec) => unknown) {
  const spy = vi.fn(listBranchCommits);
  setWorkspaceSidebarSourceForTests({
    ...DEFAULT_WORKSPACE_SIDEBAR_SOURCE,
    listBranchCommits: (sessionId, range) =>
      Promise.resolve(spy(sessionId, range)).then(
        (response) => response as ListBranchCommitsResponse,
      ),
  });
  return spy;
}

afterEach(() => setWorkspaceSidebarSourceForTests(null));

describe("fetchBranchCommits", () => {
  it("asks the host for the default branch's commits, and keeps what it can read of each", async () => {
    const listed = answering(() => ({
      base: BASE,
      commits: [
        {
          object: OID,
          subject: "Add the parser",
          authorName: "Ada",
          authorTimeUnix: 1_700_000_000n,
          root: false,
        },
        {
          object: "not-a-commit",
          subject: "dropped",
          authorName: "",
          authorTimeUnix: 0n,
          root: false,
        },
        { object: "b".repeat(64), subject: "", authorName: "", authorTimeUnix: 0n, root: true },
      ],
      truncated: true,
    }));
    await expect(fetchBranchCommits(SESSION)).resolves.toEqual({
      kind: "listed",
      commits: [
        {
          oid: OID,
          subject: "Add the parser",
          authorName: "Ada",
          authoredAtMs: 1_700_000_000_000,
          root: false,
        },
        { oid: "b".repeat(64), subject: "", authorName: "", authoredAtMs: null, root: true },
      ],
      truncated: true,
      baseDisplay: "origin/main",
    });
    expect(listed).toHaveBeenCalledWith(SESSION, BRANCH_RANGE);
  });

  it("asks for the range it's given", async () => {
    const listed = answering(() => ({ base: BASE, commits: [], truncated: false }));
    await fetchBranchCommits(SESSION, { base: "main", head: "feature" });
    expect(listed).toHaveBeenCalledWith(SESSION, { base: "main", head: "feature" });
  });

  it("names the base by its reference when the host gives no display name", async () => {
    answering(() => ({ base: { ...BASE, baseDisplay: "" }, commits: [], truncated: false }));
    await expect(fetchBranchCommits(SESSION)).resolves.toMatchObject({
      baseDisplay: "origin/main",
    });
  });

  it("passes on the host's problem", async () => {
    answering(() => ({
      base: {
        ...BASE,
        unavailable: {
          problem: ComparisonProblem.NO_BRANCH_POINT,
          reference: "origin/main",
          shallow: true,
          detail: "",
          field: ComparisonField.UNSPECIFIED,
        },
      },
      commits: [],
      truncated: false,
    }));
    await expect(fetchBranchCommits(SESSION)).resolves.toEqual({
      kind: "unavailable",
      problem: { reason: "no_branch_point", transient: false, ref: "origin/main", shallow: true },
    });
  });

  it("says so while no route serves the commits", async () => {
    await expect(fetchBranchCommits(SESSION)).rejects.toBeInstanceOf(
      WorkspaceMethodUnavailableError,
    );
  });
});

describe("commitsRange", () => {
  const TYPED = { base: "v1", head: "feature" };
  const OTHER = { base: "main", head: "" };

  it("lists the session's branch without a typed comparison", () => {
    expect(commitsRange(null, OTHER)).toEqual(BRANCH_RANGE);
  });

  it("lists a typed comparison's own base and head, the working copy as HEAD", () => {
    expect(commitsRange(TYPED, BRANCH_RANGE)).toEqual(TYPED);
    expect(commitsRange({ base: "v1", head: "" }, BRANCH_RANGE)).toEqual({ base: "v1", head: "" });
  });

  it("keeps the previous range while a single commit is shown on its own", () => {
    expect(commitsRange(commitRevisions(OID), TYPED)).toBe(TYPED);
    expect(commitsRange(commitRevisions("b".repeat(64), { root: true }), TYPED)).toBe(TYPED);
    // A typed range that ends at a commit, but from another base, is listed as typed.
    expect(commitsRange({ base: "main", head: OID }, TYPED)).toEqual({ base: "main", head: OID });
  });
});

describe("branchCommitsView", () => {
  it("is loading until answered, and shows a listing as listed", () => {
    expect(branchCommitsView(undefined, null)).toEqual({ kind: "loading" });
    const listing = { kind: "listed" as const, commits: [], truncated: false, baseDisplay: "main" };
    expect(branchCommitsView(listing, null)).toBe(listing);
  });

  it("offers Try Again only for problems that may pass", () => {
    expect(
      branchCommitsView(
        { kind: "unavailable", problem: { reason: "timeout", transient: true } },
        null,
      ),
    ).toMatchObject({ kind: "unavailable", canRetry: true });
    expect(
      branchCommitsView(
        { kind: "unavailable", problem: { reason: "not_a_repository", transient: false } },
        null,
      ),
    ).toMatchObject({ kind: "unavailable", canRetry: false });
  });

  it("says the host doesn't list commits yet, without Try Again", () => {
    expect(
      branchCommitsView(undefined, new WorkspaceMethodUnavailableError("listBranchCommits")),
    ).toEqual({ kind: "unavailable", message: BRANCH_COMMITS_NOT_SERVED, canRetry: false });
  });

  it("shows the host's own refusal, or a generic failure, with Try Again", () => {
    expect(branchCommitsView(undefined, new Error("git log failed"))).toEqual({
      kind: "unavailable",
      message: "git log failed",
      canRetry: true,
    });
    expect(branchCommitsView(undefined, "no reason")).toEqual({
      kind: "unavailable",
      message: BRANCH_COMMITS_FAILED,
      canRetry: true,
    });
  });
});
