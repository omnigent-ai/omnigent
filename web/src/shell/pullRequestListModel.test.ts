import type { PullRequestListStats } from "@/lib/pullRequestListStats";
import { describe, expect, it } from "vitest";
import type { MergeReadinessStatus, MergeReadinessView } from "@/lib/mergeReadiness";
import type {
  PullRequest,
  PullRequestAssociation,
  PullRequestChecks,
  PullRequestReviewer,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { DEFAULT_PULL_REQUEST_LIST_DISPLAY } from "@/lib/pullRequestListPreferences";
import {
  checkResult,
  hostListStatsTargets,
  type PullRequestListStatsRow,
  displayAfter,
  effectiveOrder,
  orderAvailable,
  filterCounts,
  matchesFilter,
  effectiveMergeReadiness,
  groupRows,
  matchesQuery,
  needsViewer,
  reviewRequestedFrom,
  nextOrder,
  pullRequestListRow,
  pullRequestListState,
  pullRequestListViewSettings,
  type PullRequestListViewFacts,
  pullRequestReviewStatus,
  repositoryLabels,
  sortRows,
  storedPullRequestHint,
  viewerLogin,
  visibleGroups,
  type PullRequestListRow,
} from "./pullRequestListModel";
import {
  EXAMPLE_DEVICE_HOST,
  withPullRequestIntegrations,
  exampleMergeReadinessSource,
} from "@/test/pullRequestIntegrations";

// Merge readiness from a stand-in source, as a build that registers one shows it.
withPullRequestIntegrations({
  mergeReadiness: exampleMergeReadinessSource(async () => ({ kind: "checking" })),
});

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

const NO_CHECKS: PullRequestChecks = { passing: 0, failing: 0, pending: 0, total: 0, runs: [] };

function association(over: Partial<PullRequestAssociation> = {}): PullRequestAssociation {
  return {
    url: `https://github.com/acme/app/pull/${over.number ?? 1}`,
    host: "github.com",
    repository: "acme/app",
    number: 1,
    relationship: "created",
    ...over,
  };
}

function pullRequest(over: Partial<PullRequest> = {}): PullRequest {
  return {
    number: 1,
    title: "Fix retries",
    state: "OPEN",
    url: "https://github.com/acme/app/pull/1",
    is_draft: false,
    author: "ada",
    base_ref: "main",
    head_ref: "ada/retry",
    checks: NO_CHECKS,
    ...over,
  };
}

function row(over: Partial<PullRequestListRow> = {}): PullRequestListRow {
  return {
    url: `https://github.com/acme/app/pull/${over.number ?? 1}`,
    number: 1,
    title: "Fix retries",
    repository: "acme/app",
    state: "open",
    author: "ada",
    headRef: "ada/retry",
    labels: [],
    checks: NO_CHECKS,
    ci: { source: "live", checks: NO_CHECKS },
    stateRead: true,
    checksIncomplete: false,
    review: undefined,
    reviewers: [],
    stats: undefined,
    needsYou: false,
    inferred: false,
    ...over,
  };
}

const reviewer = (
  login: string,
  state: PullRequestReviewer["state"],
  kind: PullRequestReviewer["kind"] = "user",
): PullRequestReviewer => ({ login, state, kind });

describe("pullRequestListState", () => {
  it.each([
    [{ state: "OPEN" }, "open"],
    [{ state: "open", is_draft: true }, "draft"],
    [{ state: "MERGED", is_draft: true }, "merged"],
    [{ state: "CLOSED" }, "closed"],
    [{ state: null }, "unknown"],
    [{}, "unknown"],
  ] as const)("%j → %s", (summary, expected) => {
    expect(pullRequestListState(summary)).toBe(expected);
  });
});

describe("pullRequestReviewStatus", () => {
  it("ranks requested changes over approvals over waiting reviewers", () => {
    expect(pullRequestReviewStatus(undefined)).toBeUndefined();
    expect(pullRequestReviewStatus([])).toBeUndefined();
    expect(pullRequestReviewStatus([reviewer("bo", "COMMENTED")])).toBeUndefined();
    expect(pullRequestReviewStatus([reviewer("bo", "PENDING")])).toBe("review_requested");
    expect(pullRequestReviewStatus([reviewer("bo", "PENDING"), reviewer("cy", "APPROVED")])).toBe(
      "approved",
    );
    expect(
      pullRequestReviewStatus([reviewer("cy", "APPROVED"), reviewer("bo", "CHANGES_REQUESTED")]),
    ).toBe("changes_requested");
  });
});

describe("reviewRequestedFrom", () => {
  it("is the account's own pending request, in any case, not a team's or a finished review", () => {
    expect(reviewRequestedFrom(pullRequest({ reviewers: [reviewer("Bo", "PENDING")] }), "bo")).toBe(
      true,
    );
    expect(reviewRequestedFrom(pullRequest({ reviewers: [reviewer("bo", "PENDING")] }), null)).toBe(
      false,
    );
    expect(
      reviewRequestedFrom(pullRequest({ reviewers: [reviewer("bo", "PENDING", "team")] }), "bo"),
    ).toBe(false);
    expect(
      reviewRequestedFrom(pullRequest({ reviewers: [reviewer("bo", "APPROVED")] }), "bo"),
    ).toBe(false);
  });
});

describe("needsViewer", () => {
  it("needs a reviewer whose review is still requested, ignoring case and teams", () => {
    const pr = pullRequest({ reviewers: [reviewer("Bo", "PENDING")] });
    expect(needsViewer(pr, "bo")).toBe(true);
    expect(needsViewer(pr, "cy")).toBe(false);
    expect(needsViewer(pullRequest({ reviewers: [reviewer("bo", "APPROVED")] }), "bo")).toBe(false);
    expect(needsViewer(pullRequest({ reviewers: [reviewer("bo", "PENDING", "team")] }), "bo")).toBe(
      false,
    );
  });

  it("needs the author when a reviewer requested changes or a check failed", () => {
    expect(needsViewer(pullRequest(), "ada")).toBe(false);
    expect(
      needsViewer(pullRequest({ reviewers: [reviewer("bo", "CHANGES_REQUESTED")] }), "ADA"),
    ).toBe(true);
    expect(
      needsViewer(pullRequest({ checks: { ...NO_CHECKS, failing: 1, total: 1 } }), "ada"),
    ).toBe(true);
  });

  it("lets known merge readiness decide the author's part", () => {
    const failingCheck = pullRequest({ checks: { ...NO_CHECKS, failing: 1, total: 1 } });
    // An advisory GitHub failure doesn't need the author once the source says the PR waits.
    expect(needsViewer(failingCheck, "ada", "pending_approval")).toBe(false);
    expect(needsViewer(pullRequest(), "ada", "policy_failing")).toBe(true);
    expect(needsViewer(pullRequest(), "ada", "ready")).toBe(true);
    // A reviewer's own request still counts, and someone else's PR never asks its author's part.
    expect(
      needsViewer(pullRequest({ reviewers: [reviewer("bo", "PENDING")] }), "bo", "ci_running"),
    ).toBe(true);
    expect(needsViewer(pullRequest(), "bo", "policy_failing")).toBe(false);
  });

  it("never needs anyone for drafts, finished PRs, or an unknown account", () => {
    const failing = { checks: { ...NO_CHECKS, failing: 1, total: 1 } };
    expect(needsViewer(pullRequest({ ...failing, is_draft: true }), "ada")).toBe(false);
    expect(needsViewer(pullRequest({ ...failing, state: "MERGED" }), "ada")).toBe(false);
    expect(needsViewer(pullRequest(failing), null)).toBe(false);
  });
});

describe("pullRequestListRow", () => {
  const listStats = (fields: Partial<PullRequestListStats>): PullRequestListStats => ({
    additions: 1,
    deletions: 1,
    changedFiles: 1,
    updatedAt: 0,
    headSha: "h2",
    dispatchedAt: 2_000,
    extended: true,
    ...fields,
  });

  it("takes state from the newest-sent read: a later batch beats an earlier overview", () => {
    const pr = pullRequest({ head_sha: "h1", checks: { ...NO_CHECKS, total: 1, passing: 1 } });
    const merged = pullRequestListRow(
      association(),
      pr,
      null,
      listStats({ state: "MERGED", isDraft: false, ci: "failing", ciHeadSha: "h2" }),
      undefined,
      1_000,
    );
    expect(merged.state).toBe("merged");
    // The overview's passing checks were for h1, so they don't show on h2.
    expect(merged.ci).toEqual({ source: "batch", rollup: "failing" });
    const newerOverview = pullRequestListRow(
      association(),
      pr,
      null,
      listStats({ state: "MERGED", headSha: "h1", ciHeadSha: "h1", ci: "failing" }),
      undefined,
      3_000,
    );
    expect(newerOverview.state).toBe("open");
    expect(newerOverview.ci).toEqual({ source: "live", checks: pr.checks });
  });

  it("shows a Example Host row's state and CI from its batch alone", () => {
    const deviceHostRow = pullRequestListRow(
      association({ state: undefined }),
      undefined,
      null,
      listStats({ state: "OPEN", isDraft: true, ci: "pending", ciHeadSha: "h2" }),
    );
    expect(deviceHostRow.state).toBe("draft");
    expect(deviceHostRow.ci).toEqual({ source: "batch", rollup: "pending" });
  });

  it("keeps the stored state from an older shell's batch, which reports none", () => {
    const legacyRow = pullRequestListRow(
      association({ state: "MERGED" }),
      undefined,
      null,
      listStats({ extended: false }),
    );
    expect(legacyRow.state).toBe("merged");
    expect(legacyRow.ci).toBeNull();
  });

  it("prefers the live PR over the stored association", () => {
    const stored = association({ title: "Old title", state: "OPEN", relationship: "inferred" });
    const live = pullRequest({
      title: "  New title ",
      state: "MERGED",
      labels: [{ name: "bug", color: null, description: null }],
      checks: { ...NO_CHECKS, partial: true },
    });
    expect(pullRequestListRow(stored, live, "ada")).toMatchObject({
      title: "New title",
      state: "merged",
      author: "ada",
      headRef: "ada/retry",
      labels: ["bug"],
      checksIncomplete: true,
      inferred: true,
    });
  });

  it("uses the stored state and title until the live PR loads", () => {
    const loading = pullRequestListRow(
      association({ title: " ", state: "CLOSED" }),
      undefined,
      "ada",
    );
    expect(loading).toMatchObject({ title: undefined, state: "closed", checks: undefined });
    expect(loading.needsYou).toBe(false);
    expect(loading.checksIncomplete).toBe(false);
  });

  it("marks unreadable checks as incomplete", () => {
    const live = pullRequest({ checks_supported: false });
    expect(pullRequestListRow(association(), live, null).checksIncomplete).toBe(true);
  });

  it("names the author by login alone, whatever page a read named", () => {
    const named = pullRequest({
      author: "ada",
      author_profile: { url: "https://github.com/ada", kind: "User" },
    });
    expect(pullRequestListRow(association(), named, null)).not.toHaveProperty("authorProfile");
    expect(pullRequestListRow(association(), named, null).author).toBe("ada");
  });
});

describe("filterCounts", () => {
  it("counts each status and the rows that need you", () => {
    const rows = [
      row({ number: 1, needsYou: true }),
      row({ number: 2 }),
      row({ number: 3, state: "draft" }),
      row({ number: 4, state: "merged" }),
      row({ number: 5, state: "unknown" }),
    ];
    expect(filterCounts(rows)).toEqual({
      all: 5,
      attention: 1,
      open: 2,
      draft: 1,
      merged: 1,
      closed: 0,
    });
  });

  it("leaves merged and closed PRs out of All where All lists only PRs in progress", () => {
    const rows = [
      row({ number: 1 }),
      row({ number: 2, state: "draft" }),
      row({ number: 3, state: "merged" }),
      row({ number: 4, state: "closed" }),
    ];
    expect(filterCounts(rows, { allInProgress: true })).toMatchObject({
      all: 2,
      merged: 1,
      closed: 1,
    });
    expect(
      rows.filter((r) => matchesFilter(r, "all", { allInProgress: true })).map((r) => r.number),
    ).toEqual([1, 2]);
    expect(rows.filter((r) => matchesFilter(r, "all")).map((r) => r.number)).toEqual([1, 2, 3, 4]);
  });
});

describe("matchesQuery", () => {
  const candidate = row({
    number: 42,
    title: "Retry uploads",
    author: "Ada",
    labels: ["flaky"],
    headRef: "ada/upload-retry",
  });

  it.each(["", "  ", "#42", "retry", "acme/app", "upload-retry", "ada flaky", "RETRY ADA"])(
    "matches %j",
    (query) => {
      expect(matchesQuery(candidate, query)).toBe(true);
    },
  );

  it.each(["#43", "retry bo", "closed"])("doesn't match %j", (query) => {
    expect(matchesQuery(candidate, query)).toBe(false);
  });
});

describe("sorting and grouping", () => {
  const rows = [
    row({ number: 1, title: "beta", state: "merged", repository: "acme/web" }),
    row({ number: 2, title: "Alpha", state: "open" }),
    row({ number: 3, title: "gamma", state: "open", needsYou: true }),
    row({ number: 4, title: "delta", state: "draft", repository: "acme/web" }),
  ];
  const numbers = (sorted: PullRequestListRow[]) => sorted.map((candidate) => candidate.number);

  it("orders by status with rows that need you first, then the newest number", () => {
    expect(numbers(sortRows(rows, "status"))).toEqual([3, 2, 4, 1]);
    expect(numbers(sortRows(rows, "number"))).toEqual([4, 3, 2, 1]);
    expect(numbers(sortRows(rows, "title"))).toEqual([2, 1, 4, 3]);
    expect(numbers(sortRows(rows, "repository"))).toEqual([3, 2, 4, 1]);
  });

  it("reverses an order without moving ties or rows that lack a value", () => {
    expect(numbers(sortRows(rows, "number", "ascending"))).toEqual([1, 2, 3, 4]);
    expect(numbers(sortRows(rows, "status", "descending"))).toEqual([1, 4, 2, 3]);
    const untitled = [...rows, row({ number: 5, title: undefined })];
    expect(numbers(sortRows(untitled, "title")).at(-1)).toBe(5);
    expect(numbers(sortRows(untitled, "title", "descending")).at(-1)).toBe(5);
  });

  it("orders by last update and by diff size, rows without stats last", () => {
    const stats = (updatedAt: number | undefined, additions?: number, deletions?: number) => ({
      updatedAt,
      additions,
      deletions,
      changedFiles: undefined,
      headSha: undefined,
      dispatchedAt: 0,
      extended: false,
    });
    const withStats = [
      row({ number: 1, stats: stats(3_000, 10, 0) }),
      row({ number: 2, stats: stats(1_000, 2_000, 200) }),
      row({ number: 3, stats: undefined }),
      row({ number: 4, stats: stats(2_000, 4, 6) }),
      row({ number: 5, stats: stats(undefined, 1, 1) }),
    ];
    expect(numbers(sortRows(withStats, "updated"))).toEqual([1, 4, 2, 5, 3]);
    expect(numbers(sortRows(withStats, "updated", "ascending"))).toEqual([2, 4, 1, 5, 3]);
    expect(numbers(sortRows(withStats, "size"))).toEqual([2, 4, 1, 5, 3]);
    expect(numbers(sortRows(withStats, "size", "ascending"))).toEqual([5, 4, 1, 2, 3]);
  });

  it("falls back to status order while update times and sizes are unavailable", () => {
    expect(effectiveOrder({ order: "updated", direction: "descending" }, false)).toEqual({
      order: "status",
      direction: "ascending",
    });
    expect(effectiveOrder({ order: "size", direction: "ascending" }, false).order).toBe("status");
    expect(effectiveOrder({ order: "updated", direction: "ascending" }, true)).toEqual({
      order: "updated",
      direction: "ascending",
    });
    expect(effectiveOrder({ order: "title", direction: "descending" }, false)).toEqual({
      order: "title",
      direction: "descending",
    });
  });

  it("flips a header's direction on a second click and starts new orders naturally", () => {
    const current = { order: "updated", direction: "descending" } as const;
    expect(nextOrder(current, "updated")).toEqual({ order: "updated", direction: "ascending" });
    expect(nextOrder(current, "title")).toEqual({ order: "title", direction: "ascending" });
    expect(nextOrder(current, "number")).toEqual({ order: "number", direction: "descending" });
  });

  it("groups by status in lifecycle order, or by repository alphabetically", () => {
    expect(groupRows(rows, "status").map((group) => [group.title, numbers(group.rows)])).toEqual([
      ["Open", [2, 3]],
      ["Draft", [4]],
      ["Merged", [1]],
    ]);
    expect(groupRows(rows, "repository").map((group) => group.title)).toEqual([
      "acme/app",
      "acme/web",
    ]);
    expect(groupRows(rows, "none")).toEqual([{ id: "all", title: "All", rows }]);
  });

  describe("with a registered merge readiness", () => {
    const universe = (number: number, mergeReadiness: MergeReadinessStatus, needsYou = false) =>
      row({
        number,
        repository: "example-org/universe",
        state: "open",
        mergeReadiness,
        needsYou,
      });
    const mixed = [
      universe(11, "policy_failing"),
      universe(12, "ready"),
      universe(13, "pending_approval"),
      row({ number: 14, state: "open" }),
      row({ number: 15, state: "merged" }),
      universe(16, "unknown"),
      universe(17, "policy_failing", true),
    ];

    it("orders by merge status, other PRs by their lifecycle among them", () => {
      expect(numbers(sortRows(mixed, "status"))).toEqual([12, 14, 17, 11, 13, 15, 16]);
      expect(numbers(sortRows(mixed, "status", "descending"))).toEqual([
        16, 15, 13, 11, 17, 14, 12,
      ]);
    });

    it("groups by merge status in display order", () => {
      expect(
        groupRows(mixed, "status").map((group) => [group.title, group.state, numbers(group.rows)]),
      ).toEqual([
        ["Ready", "open", [12]],
        ["Open", "open", [14]],
        ["Policy checks failing", "open", [11, 17]],
        ["Pending approval", "open", [13]],
        ["Merged", "merged", [15]],
        ["Merge status unknown", "open", [16]],
      ]);
    });

    it("keeps the lifecycle order while no row has merge readiness", () => {
      const plain = mixed.map(({ mergeReadiness: _, ...rest }) => rest);
      expect(numbers(sortRows(plain, "status"))).toEqual([17, 16, 14, 13, 12, 11, 15]);
      expect(groupRows(plain, "status").map((group) => group.title)).toEqual(["Open", "Merged"]);
    });

    describe("from the views the list reads", () => {
      const HEAD = "a".repeat(40);
      const known = (status: MergeReadinessStatus): MergeReadinessView => ({
        kind: "known",
        readiness: { status, reasons: { failingJobs: [], failingRequirements: [] } },
        headSha: HEAD,
      });
      const listRow = (
        number: number,
        view: MergeReadinessView | undefined,
        over: Partial<PullRequest> = {},
      ) =>
        pullRequestListRow(
          association({ number, repository: "example-org/universe" }),
          pullRequest({ number, author: "ada", ...over }),
          "ada",
          undefined,
          view,
        );
      const grouped = (list: PullRequestListRow[]) =>
        groupRows(list, "status").map((group) => [group.title, numbers(group.rows)]);

      it("keeps a failed, expired or loading read as Unknown or Pending", () => {
        const before = [listRow(1, known("ready")), listRow(2, known("ready"))];
        expect(grouped(before)).toEqual([["Ready", [1, 2]]]);
        for (const failure of ["service", "github", "credential"] as const) {
          const after = [listRow(1, known("ready")), listRow(2, { kind: "unknown", failure })];
          expect(grouped(after)).toEqual([
            ["Ready", [1]],
            ["Merge status unknown", [2]],
          ]);
        }
        const checking = [listRow(1, known("ready")), listRow(2, { kind: "checking" })];
        expect(grouped(checking)).toEqual([
          ["Ready", [1]],
          ["Pending", [2]],
        ]);
      });

      it("stays in merge-status order when every read fails", () => {
        const failed = [
          listRow(1, { kind: "unknown", failure: "service" }),
          listRow(2, { kind: "unknown", failure: "credential" }),
          row({ number: 3, state: "open" }),
        ];
        expect(grouped(failed)).toEqual([
          ["Open", [3]],
          ["Merge status unknown", [1, 2]],
        ]);
      });

      it("uses GitHub status when merge status is off", () => {
        const off = [listRow(1, { kind: "off" }), listRow(2, undefined)];
        expect(off.every((candidate) => candidate.mergeReadinessView === undefined)).toBe(true);
        expect(grouped(off)).toEqual([["Open", [1, 2]]]);
      });

      it("lets a confirmed merge or close beat a readiness read before it", () => {
        const merged = listRow(1, known("ready"), { state: "MERGED" });
        const closed = listRow(2, known("ready"), { state: "CLOSED" });
        expect(merged.mergeReadinessView).toBeUndefined();
        expect(effectiveMergeReadiness(known("ready"), "closed")).toBeUndefined();
        expect(grouped([merged, closed, listRow(3, known("ready"))])).toEqual([
          ["Ready", [3]],
          ["Merged", [1]],
          ["Closed", [2]],
        ]);
      });

      it("leaves Needs you to GitHub when the read failed", () => {
        const failing = { ...NO_CHECKS, failing: 1, total: 1 };
        expect(listRow(1, known("pending"), { checks: failing }).needsYou).toBe(false);
        expect(
          listRow(1, { kind: "unknown", failure: "service" }, { checks: failing }).needsYou,
        ).toBe(true);
      });
    });
  });

  it("filters, searches, sorts, and groups together", () => {
    const groups = visibleGroups(rows, {
      query: "a",
      filter: "open",
      grouping: "status",
      order: "number",
    });
    expect(groups.map((group) => [group.title, numbers(group.rows)])).toEqual([["Open", [3, 2]]]);
    expect(
      visibleGroups(rows, { query: "", filter: "attention", grouping: "none", order: "status" })[0]
        .rows,
    ).toEqual([rows[2]]);
  });
});

describe("repositoryLabels", () => {
  it("is absent for a single repository", () => {
    expect(repositoryLabels([row(), row({ number: 2 })])).toBeUndefined();
  });

  it("shortens names, keeping owners when two repositories share a name", () => {
    const labels = repositoryLabels([
      row({ repository: "acme/app" }),
      row({ repository: "acme/web" }),
      row({ repository: "fork/web" }),
    ]);
    expect(Object.fromEntries(labels!)).toEqual({
      "acme/app": "app",
      "acme/web": "acme/web",
      "fork/web": "fork/web",
    });
  });
});

describe("checkResult", () => {
  const checks = (over: Partial<PullRequestChecks>): PullRequestChecks => ({
    ...NO_CHECKS,
    ...over,
  });

  it("reports no checks, distinguishing ones that couldn't load", () => {
    expect(checkResult(NO_CHECKS)).toEqual({ tone: "none", description: "No checks reported" });
    expect(checkResult(NO_CHECKS, true).description).toBe("Checks couldn’t be loaded");
  });

  it("puts failures before running checks and counts both", () => {
    expect(checkResult(checks({ total: 4, passing: 1, failing: 2, pending: 1 }))).toEqual({
      tone: "failing",
      count: 2,
      description: "2 failing checks · 1 of 4 passed",
    });
    expect(checkResult(checks({ total: 3, passing: 2, failing: 1 })).description).toBe(
      "1 failing check · 2 of 3 passed",
    );
    expect(checkResult(checks({ total: 3, passing: 1, pending: 2 }))).toEqual({
      tone: "pending",
      count: 2,
      description: "2 running · 1 of 3 passed",
    });
    expect(checkResult(checks({ total: 2, passing: 2 }))).toEqual({
      tone: "passing",
      description: "All checks passed · 2 of 2 passed",
    });
  });

  it("doesn't count cancelled or skipped checks as passing", () => {
    expect(checkResult(checks({ total: 1, cancelled: 1 }))).toEqual({
      tone: "cancelled",
      count: 1,
      description: "1 cancelled check · 0 of 1 passed",
    });
    expect(checkResult(checks({ total: 3, passing: 1, cancelled: 2 })).description).toBe(
      "2 cancelled checks · 1 of 3 passed",
    );
    expect(checkResult(checks({ total: 2, skipped: 2 }))).toEqual({
      tone: "skipped",
      description: "All 2 checks skipped",
    });
    expect(checkResult(checks({ total: 3, passing: 2, skipped: 1 }))).toEqual({
      tone: "passing",
      description: "All checks passed · 2 passed, 1 skipped",
    });
    // Failures and running checks still outrank cancellations in a mixed run.
    expect(checkResult(checks({ total: 3, passing: 1, failing: 1, cancelled: 1 })).tone).toBe(
      "failing",
    );
    expect(checkResult(checks({ total: 3, passing: 1, pending: 1, cancelled: 1 })).tone).toBe(
      "pending",
    );
  });

  it("doesn't claim a pass when the counts don't cover every check", () => {
    expect(checkResult(checks({ total: 3, passing: 2 }))).toEqual({
      tone: "pending",
      description: "Some checks unavailable · 2 of 3 passed",
    });
  });

  it("never claims an incomplete list passed", () => {
    expect(checkResult(checks({ total: 2, passing: 2 }), true)).toEqual({
      tone: "pending",
      count: undefined,
      description: "Some checks unavailable · 2 of 2 passed",
    });
    expect(checkResult(checks({ total: 2, passing: 1, failing: 1 }), true).description).toBe(
      "At least 1 failing check · 1 of 2 passed",
    );
  });
});

describe("viewerLogin", () => {
  it("prefers the pinned account, then the active one", () => {
    const accounts = [
      { login: "ada", active: false },
      { login: "bo", active: true },
    ];
    expect(viewerLogin({ selected_account: "ada", accounts })).toBe("ada");
    expect(viewerLogin({ selected_account: null, accounts })).toBe("bo");
    expect(viewerLogin({ selected_account: null, accounts: null })).toBeNull();
  });
});

describe("storedPullRequestHint", () => {
  it("says how to load details, with the last sync when known", () => {
    expect(storedPullRequestHint({}, false)).toBe(
      "To load the PR details, please reconnect the session’s host.",
    );
    expect(storedPullRequestHint({ state_checked_at: 0 }, true)).toBe(
      "To load the PR details, resume the session’s sandbox.",
    );
    expect(storedPullRequestHint({ state_checked_at: 1_800_000_000 }, true)).toMatch(
      /^Last sync time: .+\. To load the PR details, resume the session’s sandbox\.$/,
    );
  });
});

describe("pullRequestListViewSettings", () => {
  const facts = (overrides: Partial<PullRequestListViewFacts> = {}): PullRequestListViewFacts => ({
    filter: "all",
    counts: { all: 3, attention: 0, open: 2, draft: 0, merged: 1, closed: 0 },
    display: DEFAULT_PULL_REQUEST_LIST_DISPLAY,
    order: "updated",
    statsAvailable: true,
    modified: false,
    ...overrides,
  });
  const values = (input: PullRequestListViewFacts) =>
    Object.fromEntries(
      pullRequestListViewSettings(input).settings.map((setting) => [
        setting.label,
        [setting.current, setting.choices.map((choice) => choice.label)],
      ]),
    );

  it("lists the pills the list shows and every display choice, with the applied ones current", () => {
    expect(values(facts())).toEqual({
      "Pull request filter": ["all", ["All", "Open", "Merged"]],
      "Group pull requests by": ["status", ["Status", "Repository", "None"]],
      "Order pull requests by": [
        "updated",
        ["Last updated", "Status", "Number", "Title", "Repository", "Diff size"],
      ],
    });
    expect(pullRequestListViewSettings(facts()).reset).toBeNull();
  });

  it("keeps the filter applied even with nothing left, and offers the reset once the display changed", () => {
    const result = pullRequestListViewSettings(
      facts({
        filter: "draft",
        counts: { all: 3, attention: 1, open: 2, draft: 0, merged: 1, closed: 0 },
        modified: true,
      }),
    );
    expect(result.settings[0]!.choices.map((choice) => choice.label)).toEqual([
      "All",
      "Needs You",
      "Open",
      "Draft",
      "Merged",
    ]);
    expect(result.reset?.label).toBe("Reset the pull request display");
  });

  it("leaves out orders that need stats the list can't read", () => {
    const order = pullRequestListViewSettings(facts({ order: "status", statsAvailable: false }))
      .settings[2]!;
    expect(order.current).toBe("status");
    expect(order.choices.map((choice) => choice.label)).toEqual([
      "Status",
      "Number",
      "Title",
      "Repository",
    ]);
  });
});

describe("displayAfter", () => {
  const display = { grouping: "repository", order: "title", direction: "descending" } as const;

  it("applies each change, ordering in the new order's natural direction", () => {
    const defaults = DEFAULT_PULL_REQUEST_LIST_DISPLAY;
    expect(displayAfter(display, { kind: "grouping", grouping: "none" }, defaults)).toEqual({
      ...display,
      grouping: "none",
    });
    expect(displayAfter(display, { kind: "order", order: "number" }, defaults)).toEqual({
      grouping: "repository",
      order: "number",
      direction: "descending",
    });
    expect(displayAfter(display, { kind: "order", order: "repository" }, defaults).direction).toBe(
      "ascending",
    );
    expect(displayAfter(display, { kind: "reset" }, defaults)).toBe(defaults);
    expect(displayAfter(display, { kind: "filter", filter: "open" }, defaults)).toBe(display);
  });
});

describe("orderAvailable", () => {
  it("holds size and update-time orders until stats are available", () => {
    expect(orderAvailable("updated", false)).toBe(false);
    expect(orderAvailable("size", false)).toBe(false);
    expect(orderAvailable("title", false)).toBe(true);
    expect(orderAvailable("size", true)).toBe(true);
  });
});

describe("hostListStatsTargets", () => {
  const statsRow = (url: string, overrides: Partial<PullRequestListStatsRow> = {}) => ({
    association: { url, repository: "acme/app", number: 7, state: "OPEN", is_draft: false },
    offline: false,
    pr: { state: "MERGED", is_draft: false, head_sha: "head" },
    ...overrides,
  });

  it("reads GitHub rows through the session's host, at the head and state the host answered", () => {
    expect(hostListStatsTargets([statsRow("https://github.com/acme/app/pull/7")], "s1")).toEqual([
      {
        url: "https://github.com/acme/app/pull/7",
        source: { kind: "host", sessionId: "s1", host: "github.com" },
        repository: "acme/app",
        number: 7,
        headSha: "head",
        open: false,
      },
    ]);
  });

  it("leaves out offline rows and other code hosts", () => {
    expect(
      hostListStatsTargets(
        [
          statsRow("https://github.com/acme/app/pull/7", { offline: true }),
          statsRow("https://2.host.example.com/org/repo/pull/9"),
        ],
        "s1",
      ),
    ).toEqual([]);
  });
});
