import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import type { PullRequestListStats, PullRequestListStatsRead } from "./pullRequestListStats";

import {
  ROW_BACKSTOP_STAGGER_MS,
  ROW_HOURLY_BACKSTOP_MS,
  ROW_SNAPSHOT_BACKSTOP_MS,
  followableRowUrls,
  latestRowReadFailed,
  newestRowSnapshot,
  rowBackstopAfter,
  rowBackstopIn,
  rowBackstopStagger,
  rowFollowsSnapshot,
  rowSnapshotFollowable,
  snapshotReadDue,
  type RowOverviewState,
} from "./pullRequestRowSnapshots";

const URL = "https://github.com/acme/app/pull/1";
const PR = { url: URL, repository: "acme/app", number: 1 };
const SCOPE = ["mac-read", "user-1", "github", "github.com"];

function batch(
  fingerprint: string | undefined,
  dispatchedAt: number,
  reader: string | null = "mac-octo",
): PullRequestListStatsRead {
  const stats = { dispatchedAt, extended: true, ...(fingerprint ? { fingerprint } : {}) };
  return {
    stats: new Map([[URL, stats as PullRequestListStats]]),
    // The PRs the batch asked about.
    hostHeads: new Map([[URL, "head"]]),
    dispatchedAt,
    ...(reader ? { reader } : {}),
  };
}

function row(over: Partial<RowOverviewState> = {}, data: RowOverviewState["data"] = {}) {
  return {
    data: { followed_snapshot: "seen", dispatched_at: 100, ...data },
    error: null,
    dataUpdatedAt: 1_000,
    errorUpdatedAt: 0,
    fetchStatus: "idle",
    ...over,
  } satisfies RowOverviewState;
}

describe("newestRowSnapshot", () => {
  // A batch of PR 1 and one other PR, `other`, so each test batch has its own key.
  const key = (other: number) => [
    ...SCOPE,
    "pull_request_list_stats",
    "mac",
    null,
    "acme/app#1",
    `acme/app#${other}`,
  ];

  it("takes the newest batch under the row's scope that asked about its PR", () => {
    const client = new QueryClient();
    client.setQueryData(key(2), batch("older", 10));
    client.setQueryData(key(3), batch("newest", 30));
    // A newer batch that didn't ask about the PR, or another user's scope, says nothing of it.
    client.setQueryData(
      [...SCOPE, "pull_request_list_stats", "mac", null, "acme/app#9"],
      batch("elsewhere", 40),
    );
    client.setQueryData(
      [
        "mac-read",
        "user-2",
        "github",
        "github.com",
        "pull_request_list_stats",
        "mac",
        null,
        "acme/app#1",
      ],
      batch("other user", 60),
    );
    expect(newestRowSnapshot(client, PR, SCOPE)).toBe("newest");
    expect(rowSnapshotFollowable(client, PR, SCOPE)).toBe(true);
    // Names match as GitHub does, whatever their case.
    expect(newestRowSnapshot(client, { ...PR, repository: "Acme/App" }, SCOPE)).toBe("newest");
    const unasked = {
      url: "https://github.com/acme/app/pull/5",
      repository: "acme/app",
      number: 5,
    };
    expect(newestRowSnapshot(client, unasked, SCOPE)).toBeUndefined();
    expect(newestRowSnapshot(client, PR, [])).toBeUndefined();
  });

  it("finds every row's newest batch in one walk of the cache, as each row alone would", () => {
    const client = new QueryClient();
    const second = { url: "https://github.com/acme/app/pull/2", repository: "acme/app", number: 2 };
    const otherScope = ["mac-read", "user-1", "github", "ghe.example.com"];
    const both = (stats: [string, string | undefined][], at: number) => ({
      ...batch(undefined, at),
      stats: new Map(
        stats.map(([url, fingerprint]) => [
          url,
          { dispatchedAt: at, extended: true, ...(fingerprint ? { fingerprint } : {}) },
        ]),
      ),
    });
    client.setQueryData(
      key(2),
      both(
        [
          [URL, "first"],
          [second.url, "second, older"],
        ],
        10,
      ),
    );
    // The newest batch for PR 2 has no snapshot of it; PR 1 isn't in it.
    client.setQueryData(
      [...SCOPE, "pull_request_list_stats", "mac", null, "acme/app#2", "acme/app#4"],
      both([[second.url, undefined]], 20),
    );
    const elsewhere = { url: "https://ghe.example.com/a/b/pull/7", repository: "a/b", number: 7 };
    client.setQueryData(
      [...otherScope, "pull_request_list_stats", "mac", null, "a/b#7"],
      both([[elsewhere.url, "enterprise"]], 5),
    );
    const rows = [
      { pr: PR, scope: SCOPE },
      { pr: second, scope: SCOPE },
      { pr: elsewhere, scope: otherScope },
      { pr: elsewhere, scope: [] },
    ];
    const walk = vi.spyOn(client.getQueryCache(), "getAll");
    expect(followableRowUrls(client, rows)).toEqual(new Set([URL, elsewhere.url]));
    expect(walk).toHaveBeenCalledTimes(1);
    for (const { pr, scope } of rows.slice(0, 3)) {
      expect(followableRowUrls(client, [{ pr, scope }]).has(pr.url)).toBe(
        rowSnapshotFollowable(client, pr, scope),
      );
    }
  });

  it.each([
    ["without a snapshot of it", batch(undefined, 50)],
    ["naming no reader", batch("unproven", 50, null)],
  ])("has none when the newest batch answered %s, whatever older ones held", (_case, newest) => {
    const client = new QueryClient();
    client.setQueryData(key(2), batch("older", 10));
    client.setQueryData(key(3), newest);
    expect(newestRowSnapshot(client, PR, SCOPE)).toBeUndefined();
    expect(rowSnapshotFollowable(client, PR, SCOPE)).toBe(false);
  });

  it("has none once the newest batch's read fails, and follows again when one answers", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.useFakeTimers({ now: 100 });
    client.setQueryData(key(2), batch("answered", 50));
    await client
      .fetchQuery({
        queryKey: key(2),
        queryFn: () => Promise.reject(new Error("GitHub didn't answer")),
      })
      .catch(() => undefined);
    expect(newestRowSnapshot(client, PR, SCOPE)).toBeUndefined();
    client.setQueryData(key(2), batch("answered again", 200));
    expect(newestRowSnapshot(client, PR, SCOPE)).toBe("answered again");
    vi.useRealTimers();
  });

  it("has none once a new batch's first read fails, though an older one answered", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.useFakeTimers({ now: 100 });
    client.setQueryData(key(2), batch("older", 50));
    // A PR joined the list: its batch has a new key, and its very first read fails.
    const failing = client.fetchQuery({
      queryKey: key(3),
      queryFn: () => Promise.reject(new Error("GitHub didn't answer")),
    });
    // While it reads it says nothing yet; the older answer still decides.
    expect(newestRowSnapshot(client, PR, SCOPE)).toBe("older");
    await failing.catch(() => undefined);
    expect(newestRowSnapshot(client, PR, SCOPE)).toBeUndefined();
    expect(rowSnapshotFollowable(client, PR, SCOPE)).toBe(false);
    client.setQueryData(key(3), batch("recovered", 200));
    expect(newestRowSnapshot(client, PR, SCOPE)).toBe("recovered");
    vi.useRealTimers();
  });
});

describe("a row's pace", () => {
  it("follows only once an answer followed a snapshot, and not after a failure", () => {
    expect(rowFollowsSnapshot(row())).toBe(true);
    expect(rowFollowsSnapshot(row({}, { followed_snapshot: undefined }))).toBe(false);
    const failed = row({ error: new Error("rate limited"), errorUpdatedAt: 2_000 });
    expect(latestRowReadFailed(failed)).toBe(true);
    expect(rowFollowsSnapshot(failed)).toBe(false);
    // A failure the row has since read past no longer decides.
    expect(latestRowReadFailed(row({ error: new Error("old"), errorUpdatedAt: 500 }))).toBe(false);
  });

  it("counts its backstop from the last answer, however often it's asked", () => {
    expect(rowBackstopIn(row(), 1_000)).toBe(ROW_SNAPSHOT_BACKSTOP_MS);
    expect(rowBackstopIn(row(), 1_000 + 31_000)).toBe(ROW_SNAPSHOT_BACKSTOP_MS - 31_000);
    // Overdue, say after a sleep: due at once, never zero or negative.
    expect(rowBackstopIn(row(), 1_000 + 3 * ROW_SNAPSHOT_BACKSTOP_MS)).toBe(1);
  });

  it("spreads hourly backstops over the hour's last 10 minutes, the same place for each link", () => {
    const urls = Array.from(
      { length: 200 },
      (_, index) => `https://github.com/acme/app/pull/${index}`,
    );
    const after = urls.map((url) => rowBackstopAfter(url, true));
    for (const each of after) {
      expect(each).toBeGreaterThan(ROW_HOURLY_BACKSTOP_MS - ROW_BACKSTOP_STAGGER_MS);
      expect(each).toBeLessThanOrEqual(ROW_HOURLY_BACKSTOP_MS);
    }
    // Spread out, not bunched: 200 rows land in at least 8 of the 10 minutes.
    expect(new Set(after.map((each) => Math.floor(each / 60_000))).size).toBeGreaterThanOrEqual(8);
    // Its link alone places it, whatever its case; without the flag, it's the 10-minute backstop.
    expect(rowBackstopStagger(urls[7]!)).toBe(rowBackstopStagger(urls[7]!.toUpperCase()));
    expect(rowBackstopAfter(urls[7]!, true)).toBe(rowBackstopAfter(urls[7]!, true));
    expect(rowBackstopAfter(urls[7]!, false)).toBe(ROW_SNAPSHOT_BACKSTOP_MS);
    expect(rowBackstopIn(row(), 1_000, rowBackstopAfter(urls[7]!, true))).toBe(after[7]);
  });
});

describe("snapshotReadDue", () => {
  const changed = { fingerprint: "changed", reader: "mac-octo", dispatchedAt: 200 };
  const due = (over: Partial<Parameters<typeof snapshotReadDue>[0]> = {}) =>
    snapshotReadDue({
      state: row(),
      snapshot: changed,
      rowReader: "mac-octo",
      held: false,
      ...over,
    });

  it("reads when the row's reader answered a newer, different snapshot", () => {
    expect(due()).toBe(true);
    expect(due({ rowReader: "Mac-Octo" })).toBe(true);
    // A row that never followed one reads once to catch up, even with a batch sent alongside it.
    const unfollowed = row({}, { followed_snapshot: undefined, dispatched_at: 200 });
    expect(due({ state: unfollowed })).toBe(true);
  });

  it.each([
    ["the snapshot it followed", { snapshot: { ...changed, fingerprint: "seen" } }],
    ["no snapshot", { snapshot: undefined }],
    ["a read in flight", { state: row({ fetchStatus: "fetching" }) }],
    ["a held reader", { held: true }],
    ["another reader's batch", { rowReader: "someone-else" }],
    ["a row with no proven reader", { rowReader: undefined }],
    ["a batch sent before the row's last read", { snapshot: { ...changed, dispatchedAt: 100 } }],
    ["a row with no answer yet", { state: { ...row(), data: undefined } }],
    [
      "a failed latest read, whose backoff reads next",
      { state: row({ error: new Error("x"), errorUpdatedAt: 2_000 }) },
    ],
  ])("waits for %s", (_case, over) => {
    expect(due(over as Partial<Parameters<typeof snapshotReadDue>[0]>)).toBe(false);
  });
});
