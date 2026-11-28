import { describe, expect, it } from "vitest";

import type {
  PullRequestAssociation,
  PullRequestChecks,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type { MergeReadinessView, MergeReadinessStatus } from "@/lib/mergeReadiness";
import { groupRows, type PullRequestListRow } from "@/shell/pullRequestListModel";
import type { PullRequestListEntry } from "@/shell/PullRequestListRows";
import {
  compactAge,
  DEFAULT_PAGE_DISPLAY,
  effectivePageSort,
  pageSortAvailable,
  githubAvatarUrl,
  groupPageRows,
  initials,
  isDefaultDisplay,
  pageCi,
  pageStatus,
  pageGroupings,
  rangeSelection,
  rowLinkTooltip,
  repositoriesSummary,
  repositoryChoices,
  retainSelection,
  showsGroupHeader,
  sortAfterHeaderClick,
  sortPageRows,
  pageDisplayModified,
  pageSortOptions,
  SEARCH_ORDER_DISPLAY,
  shownPageDisplay,
  statusDescription,
  statusSaidByState,
} from "./pullRequestListPageModel";
import {
  exampleMergeReadinessSource,
  withPullRequestIntegrations,
} from "@/test/pullRequestIntegrations";

// Merge readiness from a stand-in source, as a build that registers one shows it.
withPullRequestIntegrations({
  mergeReadiness: exampleMergeReadinessSource(async () => ({ kind: "checking" })),
});

const NO_CHECKS: PullRequestChecks = { passing: 0, failing: 0, pending: 0, total: 0, runs: [] };

function row(over: Partial<PullRequestListRow> = {}): PullRequestListRow {
  const number = over.number ?? 1;
  return {
    url: `https://github.com/acme/app/pull/${number}`,
    number,
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

function stats(updatedAt: number, additions = 1, deletions = 1) {
  return {
    additions,
    deletions,
    changedFiles: 1,
    updatedAt,
    headSha: "head",
    dispatchedAt: 0,
    extended: true,
  };
}

function readiness(status: MergeReadinessStatus): MergeReadinessView {
  return {
    kind: "known",
    headSha: "head",
    readiness: {
      status,
      reasons: { failingJobs: [], failingRequirements: [] },
    } as never,
  };
}

const association: PullRequestAssociation = {
  url: "https://github.com/acme/app/pull/1",
  host: "github.com",
  repository: "acme/app",
  number: 1,
  relationship: "created",
};

function entry(
  rowOver: Partial<PullRequestListRow> = {},
  over: Partial<PullRequestListEntry> = {},
): PullRequestListEntry {
  return {
    association,
    row: row(rowOver),
    pr: undefined,
    offline: false,
    status: "ready",
    error: undefined,
    mergeHead: undefined,
    mergeHint: undefined,
    mergeUnavailable: false,
    ...over,
  };
}

const status = (value: PullRequestListEntry, mergeRequested = false) =>
  pageStatus(value, { sandboxAsleep: false, mergeRequested });

describe("groupPageRows", () => {
  it("keeps open pull requests in one Open group whatever their merge readiness says", () => {
    const rows = [
      row({
        number: 1,
        mergeReadinessView: readiness("policy_failing"),
        mergeReadiness: "policy_failing",
      }),
      row({
        number: 2,
        mergeReadinessView: readiness("needs_reviewer"),
        mergeReadiness: "needs_reviewer",
      }),
      row({ number: 3, state: "merged" }),
    ];

    const groups = groupPageRows(rows, "status");

    expect(groups.map((group) => [group.title, group.rows.map((value) => value.number)])).toEqual([
      ["Open", [1, 2]],
      ["Merged", [3]],
    ]);
    // The classic list splits the same rows by readiness; the new list doesn't change that.
    expect(groupRows(rows, "status").length).toBe(3);
  });

  it("orders lifecycle groups open, draft, merged, closed", () => {
    const rows = (["closed", "merged", "draft", "open"] as const).map((state, index) =>
      row({ number: index + 1, state }),
    );
    expect(groupPageRows(rows, "status").map((group) => group.state)).toEqual([
      "open",
      "draft",
      "merged",
      "closed",
    ]);
  });

  it("groups by author case-insensitively, with an unknown author last", () => {
    const rows = [
      row({ number: 1, author: "zoe" }),
      row({ number: 2, author: null }),
      row({ number: 3, author: "Ada" }),
      row({ number: 4, author: "ada" }),
    ];
    expect(groupPageRows(rows, "author").map((group) => [group.title, group.rows.length])).toEqual([
      ["Ada", 2],
      ["zoe", 1],
      ["Unknown author", 1],
    ]);
  });

  it("puts everything in one group without grouping", () => {
    expect(groupPageRows([row(), row({ number: 2 })], "none")).toEqual([
      expect.objectContaining({ id: "all", rows: expect.any(Array) }),
    ]);
  });
});

describe("sortPageRows", () => {
  it("sorts status by lifecycle, with rows that need you first within it", () => {
    const rows = [
      row({ number: 1, state: "merged", needsYou: true }),
      row({ number: 2, state: "open" }),
      row({ number: 3, state: "open", needsYou: true }),
      row({ number: 4, state: "draft" }),
    ];
    expect(
      sortPageRows(rows, { key: "status", direction: "ascending" }).map((value) => value.number),
    ).toEqual([3, 2, 4, 1]);
  });

  it("breaks ties by the most recent update, then by URL", () => {
    const rows = [
      row({ number: 1, stats: stats(100) }),
      row({ number: 2, stats: stats(300) }),
      row({ number: 3, stats: stats(300) }),
    ];
    expect(
      sortPageRows(rows, { key: "status", direction: "ascending" }).map((value) => value.number),
    ).toEqual([2, 3, 1]);
  });

  it("orders by size and update time, treating missing stats as the smallest", () => {
    const rows = [
      row({ number: 1, stats: stats(1, 10, 10) }),
      row({ number: 2 }),
      row({ number: 3, stats: stats(2, 1, 1) }),
    ];
    expect(
      sortPageRows(rows, { key: "size", direction: "descending" }).map((value) => value.number),
    ).toEqual([1, 3, 2]);
    expect(
      sortPageRows(rows, { key: "updated", direction: "descending" }).map((value) => value.number),
    ).toEqual([3, 1, 2]);
    expect(
      sortPageRows(rows, { key: "size", direction: "ascending" }).map((value) => value.number),
    ).toEqual([2, 3, 1]);
  });

  it.each([
    ["number", "descending", [3, 2, 1]],
    ["title", "ascending", [2, 1, 3]],
    ["author", "ascending", [3, 1, 2]],
  ] as const)("sorts by %s %s", (key, direction, expected) => {
    const rows = [
      row({ number: 1, title: "b", author: "bob" }),
      row({ number: 2, title: "A", author: null }),
      row({ number: 3, title: "c", author: "ada" }),
    ];
    expect(sortPageRows(rows, { key, direction }).map((value) => value.number)).toEqual(expected);
  });
});

describe("sorting choices", () => {
  it("flips the direction on the same header and starts a new key at its natural direction", () => {
    const sort = { key: "updated", direction: "descending" } as const;
    expect(sortAfterHeaderClick(sort, "updated")).toEqual({
      key: "updated",
      direction: "ascending",
    });
    expect(sortAfterHeaderClick(sort, "title")).toEqual({ key: "title", direction: "ascending" });
    expect(sortAfterHeaderClick(sort, "number")).toEqual({
      key: "number",
      direction: "descending",
    });
  });

  it("keeps status order until the stats an order needs arrive", () => {
    expect(effectivePageSort({ key: "updated", direction: "descending" }, false)).toEqual({
      key: "status",
      direction: "ascending",
    });
    expect(effectivePageSort({ key: "updated", direction: "descending" }, true).key).toBe(
      "updated",
    );
    expect(effectivePageSort({ key: "title", direction: "ascending" }, false).key).toBe("title");
  });

  it("gives the same status order each time it stands in, so the rows aren't sorted again", () => {
    const waiting = { key: "updated", direction: "descending" } as const;
    expect(effectivePageSort(waiting, false)).toBe(effectivePageSort({ ...waiting }, false));
  });

  it("knows the default display", () => {
    expect(isDefaultDisplay(DEFAULT_PAGE_DISPLAY)).toBe(true);
    expect(isDefaultDisplay({ ...DEFAULT_PAGE_DISPLAY, grouping: "author" })).toBe(false);
  });
});

describe("showsGroupHeader", () => {
  it("hides a lone open group's header unless it can be removed", () => {
    const base = {
      grouping: "status",
      groupCount: 1,
      collapsed: false,
      offersRemoval: false,
    } as const;
    expect(showsGroupHeader(base)).toBe(false);
    expect(showsGroupHeader({ ...base, offersRemoval: true })).toBe(true);
    expect(showsGroupHeader({ ...base, groupCount: 2 })).toBe(true);
    expect(showsGroupHeader({ ...base, grouping: "none", groupCount: 2 })).toBe(false);
  });

  it("keeps a lone group's header while its menus offer an action, such as Hide Repository", () => {
    const lone = {
      grouping: "repository",
      groupCount: 1,
      collapsed: false,
      offersRemoval: false,
    } as const;
    expect(showsGroupHeader(lone)).toBe(false);
    expect(showsGroupHeader({ ...lone, actionCount: 1 })).toBe(true);
  });

  it("keeps a collapsed lone group's header, so its rows stay reachable", () => {
    expect(
      showsGroupHeader({
        grouping: "status",
        groupCount: 1,
        collapsed: true,
        offersRemoval: false,
      }),
    ).toBe(true);
  });
});

describe("selection", () => {
  const order = ["a", "b", "c", "d"];

  it("selects the range between the anchor and the target either way", () => {
    expect(rangeSelection(order, "b", "d")).toEqual(["b", "c", "d"]);
    expect(rangeSelection(order, "d", "b")).toEqual(["b", "c", "d"]);
    expect(rangeSelection(order, undefined, "c")).toEqual(["c"]);
    expect(rangeSelection(order, "gone", "c")).toEqual(["c"]);
    expect(rangeSelection(order, "a", "gone")).toEqual([]);
  });

  it("drops rows that left the list and keeps the same set when none did", () => {
    const selection = new Set(["a", "c"]);
    expect(retainSelection(selection, order)).toBe(selection);
    expect([...retainSelection(selection, ["a", "b"])]).toEqual(["a"]);
  });
});

describe("pageStatus", () => {
  it("names a pending reviewer Review Requested, never Review Required, without readiness", () => {
    expect(status(entry({ review: "review_requested" }))).toMatchObject({
      label: "Review Requested",
      tone: "warning",
    });
    expect(status(entry({ review: "changes_requested" })).label).toBe("Changes Requested");
    expect(status(entry({ review: "approved" })).label).toBe("Approved");
    expect(status(entry()).label).toBe("Open");
  });

  it("uses merge readiness where it's read, in Title Case, with its attention", () => {
    expect(
      status(entry({ mergeReadinessView: readiness("review_required"), needsYou: false })),
    ).toMatchObject({ kind: "readiness", label: "Review Required", tone: "warning" });
    expect(
      status(entry({ mergeReadinessView: readiness("policy_failing"), needsYou: true })),
    ).toMatchObject({ label: "Policy", tone: "danger", attention: true });
  });

  it("shows finished pull requests by their lifecycle, whatever reviewers said", () => {
    expect(status(entry({ state: "merged", review: "approved" }))).toMatchObject({
      label: "Merged",
      tone: "merged",
    });
    expect(status(entry({ state: "draft", review: "review_requested" })).label).toBe("Draft");
  });

  it("waits calmly for GitHub's rate limit, keeping the state a row last read", () => {
    const limited = Object.assign(new Error("GitHub is limiting requests from this Mac."), {
      code: "rate_limited",
    });
    expect(status(entry({ stateRead: false }, { status: "error", error: limited }))).toMatchObject({
      kind: "waiting",
      label: "Waiting for GitHub",
      tone: "muted",
    });
    expect(
      status(entry({ stateRead: true, review: "approved" }, { status: "error", error: limited })),
    ).toMatchObject({ label: "Approved" });
  });

  it("explains a failed refresh, an offline host, a merge in flight and a first load", () => {
    expect(status(entry({}, { status: "error", error: new Error("rate limited") }))).toMatchObject({
      kind: "issue",
      label: "Couldn’t Refresh",
      detail: "Error: rate limited",
    });
    expect(status(entry({}, { offline: true }))).toMatchObject({
      kind: "offline",
      label: "Host Offline",
    });
    expect(
      pageStatus(entry({}, { offline: true }), { sandboxAsleep: true, mergeRequested: false })
        .label,
    ).toBe("Sandbox Asleep");
    expect(status(entry(), true)).toMatchObject({ kind: "merging", label: "Merge Requested" });
    expect(status(entry({ stateRead: false }, { status: "loading" })).kind).toBe("loading");
  });

  it("gives a review outcome the same meaning with or without merge readiness", () => {
    for (const [review, readinessStatus, meaning] of [
      ["approved", "approved", "approved"],
      ["changes_requested", "changes_requested", "changes-requested"],
      ["review_requested", "review_required", "review-requested"],
    ] as const) {
      expect(status(entry({ review })).meaning).toBe(meaning);
      expect(status(entry({ mergeReadinessView: readiness(readinessStatus) })).meaning).toBe(
        meaning,
      );
    }
  });

  it("names every page state's meaning, so each draws its own icon", () => {
    expect(status(entry({}, { offline: true })).meaning).toBe("offline");
    expect(
      pageStatus(entry({}, { offline: true }), { sandboxAsleep: true, mergeRequested: false })
        .meaning,
    ).toBe("asleep");
    expect(status(entry({}, { status: "error", error: new Error("boom") })).meaning).toBe(
      "refresh-failed",
    );
    expect(status(entry(), true).meaning).toBe("merge-requested");
    expect(status(entry({ mergeReadinessView: { kind: "checking" } })).meaning).toBe("checking");
    expect(
      status(entry({ mergeReadinessView: { kind: "unknown", failure: "credential" } })).meaning,
    ).toBe("credential");
  });
});

describe("statusSaidByState", () => {
  const saidByState = (value: PullRequestListEntry) =>
    statusSaidByState(status(value), value.row.state);

  it("leaves the cell to the state glyph only where it says the same thing", () => {
    expect(saidByState(entry())).toBe(true);
    expect(saidByState(entry({ state: "draft" }))).toBe(true);
    expect(saidByState(entry({ state: "merged", review: "approved" }))).toBe(true);
    expect(saidByState(entry({ state: "closed" }))).toBe(true);
    expect(saidByState(entry({ state: "unknown" }))).toBe(true);
    expect(saidByState(entry({ mergeReadinessView: readiness("open") }))).toBe(true);
    expect(saidByState(entry({ state: "open", mergeReadinessView: readiness("draft") }))).toBe(
      false,
    );
  });

  it("always shows a review outcome, a readiness read failure and every page state", () => {
    expect(saidByState(entry({ review: "approved" }))).toBe(false);
    expect(saidByState(entry({ review: "review_requested" }))).toBe(false);
    expect(saidByState(entry({ mergeReadinessView: readiness("unknown") }))).toBe(false);
    for (const failure of ["credential", "github", "service"] as const) {
      expect(saidByState(entry({ mergeReadinessView: { kind: "unknown", failure } }))).toBe(false);
    }
    expect(saidByState(entry({ mergeReadinessView: { kind: "checking" } }))).toBe(false);
    expect(saidByState(entry({}, { offline: true }))).toBe(false);
    expect(saidByState(entry({}, { status: "error", error: new Error("boom") }))).toBe(false);
  });
});

describe("statusDescription", () => {
  it("names the status, then says why where it can", () => {
    expect(statusDescription(status(entry({ review: "review_requested" })))).toBe(
      "Review Requested",
    );
    expect(
      statusDescription(
        status(entry({ mergeReadinessView: { kind: "unknown", failure: "service" } })),
      ),
    ).toBe(
      "Merge status unknown. Couldn't reach the merge status service. The app will try again.",
    );
  });
});

describe("pageCi", () => {
  const live = (checks: Partial<PullRequestChecks>) =>
    ({ source: "live", checks: { ...NO_CHECKS, ...checks } }) as const;

  it("shows a stats rollup as a glyph alone, with no counts or progress", () => {
    expect(pageCi(row({ ci: { source: "batch", rollup: "pending" } }), false)).toEqual({
      tone: "pending",
      title: "Checks running",
    });
    // An older head's live checks never decorate a newer rollup.
    expect(
      pageCi(
        row({
          ci: { source: "batch", rollup: "failing" },
          checks: { ...NO_CHECKS, failing: 3, total: 3 },
        }),
        false,
      ),
    ).toEqual({ tone: "failing", title: "Checks failing" });
  });

  it("counts failing checks and measures running ones from a live read", () => {
    expect(pageCi(row({ ci: live({ failing: 2, passing: 1, total: 3 }) }), false)).toMatchObject({
      tone: "failing",
      count: 2,
    });
    expect(pageCi(row({ ci: live({ pending: 1, passing: 3, total: 4 }) }), false)).toMatchObject({
      tone: "pending",
      progress: 0.75,
    });
    expect(pageCi(row({ ci: live({ passing: 2, total: 2 }) }), false)).toMatchObject({
      tone: "passing",
    });
    expect(pageCi(row({ ci: live({}) }), false)?.tone).toBe("none");
  });

  it("shows nothing for finished pull requests, unread CI, or live checks while offline", () => {
    expect(pageCi(row({ state: "merged", ci: live({ passing: 1, total: 1 }) }), false)).toBeNull();
    expect(pageCi(row({ ci: null }), false)).toBeNull();
    expect(pageCi(row({ ci: live({ passing: 1, total: 1 }) }), true)).toBeNull();
  });
});

describe("avatars", () => {
  it("reads GitHub's public picture only for github.com logins", () => {
    expect(githubAvatarUrl("ada-l", "github.com")).toBe(
      "https://avatars.githubusercontent.com/ada-l?s=64",
    );
    expect(githubAvatarUrl("ada", "github.example.com")).toBeUndefined();
    expect(githubAvatarUrl("ada", null)).toBeUndefined();
    expect(githubAvatarUrl("renovate[bot]", "github.com")).toBeUndefined();
    expect(githubAvatarUrl("org/team", "github.com")).toBeUndefined();
  });

  it.each([
    ["Ada Lovelace", "AL"],
    ["olaf-geirsson_data", "OG"],
    ["ada", "AD"],
    ["  ", "?"],
  ])("gives %s the initials %s", (value, expected) => {
    expect(initials(value)).toBe(expected);
  });
});

describe("compactAge", () => {
  const now = new Date(2026, 10, 7, 12).getTime();
  const ago = (seconds: number) => compactAge(now - seconds * 1000, now);

  it.each([
    [30, "now"],
    [12 * 60, "12m"],
    [5 * 3600, "5h"],
    [3 * 86_400, "3d"],
    [14 * 86_400, "2w"],
    [34 * 86_400, "4w"],
  ])("shows %ss ago as %s", (seconds, expected) => {
    expect(ago(seconds)).toBe(expected);
  });

  it("switches to a calendar date after five weeks, with the year once it differs", () => {
    expect(compactAge(new Date(2026, 8, 1).getTime(), now)).toBe(
      new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(
        new Date(2026, 8, 1),
      ),
    );
    expect(compactAge(new Date(2025, 0, 15).getTime(), now)).toBe(
      new Intl.DateTimeFormat(undefined, { month: "short", year: "numeric" }).format(
        new Date(2025, 0, 15),
      ),
    );
  });

  it("never shows a future update as negative", () => {
    expect(compactAge(now + 60_000, now)).toBe("now");
  });
});

describe("pageSortAvailable", () => {
  it("holds size and update-time sorts until stats are ready", () => {
    expect(pageSortAvailable("updated", false)).toBe(false);
    expect(pageSortAvailable("size", false)).toBe(false);
    expect(pageSortAvailable("title", false)).toBe(true);
    expect(pageSortAvailable("updated", true)).toBe(true);
  });
});

describe("repository filter", () => {
  const rows = [
    { repository: "Acme/App" },
    { repository: "acme/docs" },
    { repository: "acme/app" },
  ];

  it("offers each repository once, ignoring case, most rows first", () => {
    expect(repositoryChoices(rows)).toEqual([
      { key: "acme/app", repository: "Acme/App", count: 2 },
      { key: "acme/docs", repository: "acme/docs", count: 1 },
    ]);
  });

  it("keeps a picked repository the rows no longer name, with none", () => {
    expect(repositoryChoices(rows, ["acme/gone"]).at(-1)).toEqual({
      key: "acme/gone",
      repository: "acme/gone",
      count: 0,
    });
  });

  it("names one repository, or how many", () => {
    expect(repositoriesSummary(["acme/app"])).toBe("acme/app");
    expect(repositoriesSummary(["acme/app", "acme/docs"])).toBe("2 repositories");
  });
});

describe("a search's own order", () => {
  it("shows while the kept display is the default, and never on a list without one", () => {
    expect(shownPageDisplay(DEFAULT_PAGE_DISPLAY, true, undefined)).toEqual(SEARCH_ORDER_DISPLAY);
    expect(shownPageDisplay(DEFAULT_PAGE_DISPLAY, true, false)).toEqual(SEARCH_ORDER_DISPLAY);
    const picked = { grouping: "author" as const, sort: DEFAULT_PAGE_DISPLAY.sort };
    expect(shownPageDisplay(picked, true, true)).toEqual(picked);
    // A view kept before choices were recorded: a display other than the default was picked.
    expect(shownPageDisplay(picked, true, undefined)).toEqual(picked);
    // A search order kept from a saved view falls back on a list in no such order.
    expect(shownPageDisplay({ ...SEARCH_ORDER_DISPLAY }, false, true)).toEqual({
      grouping: "none",
      sort: DEFAULT_PAGE_DISPLAY.sort,
    });
    expect(pageSortOptions(true)[0]?.title).toBe("Search order");
    expect(pageSortOptions(false).map((option) => option.id)).not.toContain("search");
  });

  it("keeps a picked display that equals the default, until Reset clears the choice", () => {
    // Group by Status, then Order by Last updated: the default's values, picked on purpose.
    expect(shownPageDisplay(DEFAULT_PAGE_DISPLAY, true, true)).toEqual(DEFAULT_PAGE_DISPLAY);
    expect(pageDisplayModified(DEFAULT_PAGE_DISPLAY, true, true)).toBe(true);
    expect(pageDisplayModified(DEFAULT_PAGE_DISPLAY, true, false)).toBe(false);
    // Elsewhere the default needs no reset, however it was reached.
    expect(pageDisplayModified(DEFAULT_PAGE_DISPLAY, false, true)).toBe(false);
  });
});

describe("groupPageRows by agent", () => {
  it("groups this agent's own rows first, then each sub-agent by its name", () => {
    const rows = [
      row({ number: 1, sessionSource: { session: { id: "child-b", name: "Docs" }, alsoIn: [] } }),
      row({ number: 2, sessionSource: { session: null, alsoIn: [] } }),
      row({ number: 3, sessionSource: { session: { id: "child-a", name: "Auth" }, alsoIn: [] } }),
      row({ number: 4, sessionSource: { session: { id: "child-a", name: "Auth" }, alsoIn: [] } }),
    ];

    const groups = groupPageRows(rows, "session");

    expect(groups.map((group) => [group.title, group.rows.map((value) => value.number)])).toEqual([
      ["This agent", [2]],
      ["Auth", [3, 4]],
      ["Docs", [1]],
    ]);
  });
});

describe("pageGroupings", () => {
  it("offers Agent, stored as the session grouping, only where sub-agents join the list", () => {
    expect(pageGroupings(true).map((option) => [option.id, option.title])).toEqual([
      ["status", "Status"],
      ["repository", "Repository"],
      ["author", "Author"],
      ["session", "Agent"],
      ["none", "No grouping"],
    ]);
    expect(pageGroupings(false).map((option) => option.id)).not.toContain("session");
  });
});

describe("rowLinkTooltip", () => {
  it("names the repository and branch, which narrow rows leave out", () => {
    expect(rowLinkTooltip(row(), "#1")).toBe("Fix retries\nacme/app\nada/retry");
  });

  it("names the agent that linked the row and every other agent that links it", () => {
    const linked = row({
      sessionSource: {
        session: { id: "child-a", name: "Auth" },
        alsoIn: [
          { id: "child-b", name: "Docs" },
          { id: "child-c", name: "Billing" },
          { id: "child-d", name: "Search" },
        ],
      },
    });
    expect(rowLinkTooltip(linked, "#1")).toBe(
      "Fix retries\nacme/app\nada/retry\nLinked by Auth\nAlso in Docs, Billing, Search",
    );
  });

  it("names only the other agents for a row the viewed session links itself", () => {
    const own = row({
      headRef: null,
      sessionSource: { session: null, alsoIn: [{ id: "child-b", name: "Docs" }] },
    });
    expect(rowLinkTooltip(own, "#1")).toBe("Fix retries\nacme/app\nAlso in Docs");
  });
});
