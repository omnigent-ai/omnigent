import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import {
  compareSessionRows,
  dedupeSessionRows,
  mergeScopeRows,
  refreshScopeWindow,
  appendScopePage,
  sessionRowsPage,
  titleSortKey,
} from "./sidebarData";

const row = (id: string, updated_at: number): Conversation => ({
  id,
  updated_at,
  created_at: 0,
  object: "conversation",
  title: id,
  labels: {},
  permission_level: 4,
});

describe("scope merge", () => {
  it("buffers rows below the shallower tail until both scopes advance", () => {
    const mine = [row("m1", 100), row("m2", 80), row("m3", 20)];
    const shared = [row("s1", 90), row("s2", 70)];
    expect(mergeScopeRows(mine, shared, true, true).rows.map((r) => r.id)).toEqual([
      "m1",
      "s1",
      "m2",
      "s2",
    ]);
    expect(
      mergeScopeRows(mine, [...shared, row("s3", 10)], true, true).rows.map((r) => r.id),
    ).toEqual(["m1", "s1", "m2", "s2", "m3"]);
  });
  it("ignores exhausted and disabled scopes as pagination boundaries", () => {
    expect(
      mergeScopeRows([row("m", 20)], [row("s", 100)], true, false).rows.map((r) => r.id),
    ).toEqual(["s", "m"]);
    expect(mergeScopeRows([row("m", 20)], [], false, false).rows).toHaveLength(1);
  });
  it("keeps the given ids listed past the shallower tail", () => {
    const mine = [row("m1", 100), row("m2", 20)];
    const shared = [row("s1", 90)];
    const merged = mergeScopeRows(
      mine,
      shared,
      true,
      true,
      "m2",
      "s1",
      "updated_at",
      new Set(["m2"]),
    );
    expect(merged.rows.map((r) => r.id)).toEqual(["m1", "s1", "m2"]);
    expect(merged.watermark?.id).toBe("s1");
  });
  it("deduplicates overlapping sources and picks the newest row", () => {
    expect(dedupeSessionRows([row("same", 10), row("same", 20)])).toEqual([row("same", 20)]);
  });
});

describe("scope refresh", () => {
  it("replaces a loaded window instead of retaining removed rows", () => {
    const current = {
      pages: [sessionRowsPage([row("removed", 40), row("a", 30)], true)],
      pageParams: [undefined],
      windowSize: 60,
    };
    const next = refreshScopeWindow(current, sessionRowsPage([row("a", 30)], true), 60);
    expect(next.pages[0].data.map((session) => session.id)).toEqual(["a"]);
  });
  it("keeps only history older than the refreshed prefix beyond the cap", () => {
    const current = {
      pages: [sessionRowsPage([row("removed", 100), row("boundary", 30), row("old", 20)], true)],
      pageParams: [undefined],
      windowSize: 240,
    };
    const next = refreshScopeWindow(
      current,
      sessionRowsPage([row("new", 110), row("boundary", 30)], true),
      200,
    );
    expect(next.pages[0].data.map((session) => session.id)).toEqual(["new", "boundary", "old"]);
    expect(next.pages[0].last_id).toBe("old");
    expect(next.windowSize).toBe(240);
  });
  it("drops older history when a refresh covers the entire scope", () => {
    const current = {
      pages: [sessionRowsPage([row("gone", 40)], true)],
      pageParams: [undefined],
      windowSize: 240,
    };
    expect(refreshScopeWindow(current, sessionRowsPage([]), 200).pages[0].data).toEqual([]);
  });
  it("rewinds pagination across a gap while retaining older rows beyond the cap", () => {
    const current = {
      pages: [sessionRowsPage([row("old", 10)], false)],
      pageParams: [undefined],
      windowSize: 240,
    };
    const next = refreshScopeWindow(current, sessionRowsPage([row("new", 50)], true), 200);
    expect(next.pages[0].data.map((session) => session.id)).toEqual(["new", "old"]);
    expect(next.pages[0].last_id).toBe("new");
    expect(next.pages[0].has_more).toBe(true);
    expect(
      mergeScopeRows(next.pages[0].data, [], true, false, "new").rows.map((session) => session.id),
    ).toEqual(["new"]);
  });
  it("deduplicates appended pages and grows the requested window", () => {
    const current = {
      pages: [sessionRowsPage([row("a", 40)], true)],
      pageParams: [undefined],
      windowSize: 30,
    };
    const next = appendScopePage(current, sessionRowsPage([row("a", 40), row("b", 20)], true), 30);
    expect(next.pages[0].data.map((session) => session.id)).toEqual(["a", "b"]);
    expect(next.windowSize).toBe(60);
    expect(next.pages[0].last_id).toBe("b");
  });
  it("keeps equal-updated_at rows in one order across Load more and the next refresh", () => {
    // Same-second rows arrive in the server's tie order, which is not id order.
    const first = [row("b", 100), row("c", 100), row("a", 100)];
    const second = [row("e", 100), row("d", 100)];
    const loaded = refreshScopeWindow(undefined, sessionRowsPage(first, true), 3);
    const afterLoadMore = appendScopePage(loaded, sessionRowsPage(second, false), 3);
    const refreshed = refreshScopeWindow(
      afterLoadMore,
      sessionRowsPage([...first, ...second], false),
      6,
    );
    expect(refreshed.pages[0].data.map((session) => session.id)).toEqual(
      afterLoadMore.pages[0].data.map((session) => session.id),
    );
    // The continuation cursor stays the server's last row, not the last displayed one.
    expect(refreshed.pages[0].last_id).toBe("d");
  });
});

it.each(["created_at", "title"] as const)(
  "uses %s for scope boundaries, appended pages, and capped refreshes",
  (sortBy) => {
    // Newest-created first and A to Z list these alike, and opposite to Updated.
    const newest = { ...row("new", 10), created_at: 30, title: "Apple" };
    const middle = { ...row("middle", 20), created_at: 20, title: "banana" };
    const oldest = { ...row("old", 30), created_at: 10, title: "Cherry" };
    expect(
      mergeScopeRows([newest, oldest], [middle], true, true, "old", "middle", sortBy).rows.map(
        (r) => r.id,
      ),
    ).toEqual(["new", "middle"]);
    const current = {
      pages: [sessionRowsPage([newest, middle], true)],
      pageParams: [undefined],
      windowSize: 2,
    };
    const appended = appendScopePage(current, sessionRowsPage([oldest]), 1, sortBy);
    expect(appended.pages[0].data.map((r) => r.id)).toEqual(["new", "middle", "old"]);
    const refreshed = refreshScopeWindow(
      appended,
      sessionRowsPage([newest, { ...middle, updated_at: 99 }], true),
      2,
      sortBy,
    );
    expect(refreshed.pages[0].data.map((r) => r.id)).toEqual(["new", "middle", "old"]);
    expect(refreshed.pages[0].last_id).toBe("old");
  },
);

it("keys names like the server: folded case, untitled last", () => {
  expect(["beta", null, "Alpha", "ab", "a-c", ""].map(titleSortKey).sort()).toEqual([
    "0a-c",
    "0ab",
    "0alpha",
    "0beta",
    "1",
    "1",
  ]);
});

it("orders names like every server dialect: ASCII-only folding, code points, id ties", () => {
  const titled = (id: string, title: string) => ({ ...row(id, 0), title });
  const rows = [
    titled("b2", "\u{1F600} smile"),
    titled("b1", "\uFF21 wide"),
    titled("a8", "\u00E9clair"),
    titled("a9", "\u00C9clair"),
    titled("a7", "zeta"),
    titled("a6", "Zeta"),
    titled("a2", "y "),
    titled("a3", "y"),
    titled("c1", ""),
    titled("a1", ""),
  ];
  // Trailing spaces count, accented capitals stay unfolded, and the emoji
  // sorts after U+FF21 by code point.
  expect(rows.sort((x, y) => compareSessionRows(x, y, "title")).map((r) => r.id)).toEqual([
    "a3",
    "a2",
    "a6",
    "a7",
    "a9",
    "a8",
    "b1",
    "b2",
    "a1",
    "c1",
  ]);
});
