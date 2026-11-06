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
import { titleToken } from "@/test/sidebarMockHelpers";

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
  it("keeps the fetched boundary when a pushed update bumps a scope's cursor row", () => {
    const mine = [row("m1", 100), row("m2", 80), row("m3", 20)];
    // Fetched as s1@90, s2@70; s2 is the shared cursor.
    const mineBoundary = { id: "m3", updated_at: 20, created_at: 0 };
    const sharedBoundary = { id: "s2", updated_at: 70, created_at: 0 };
    // The shared cursor row's agent becomes active; the stream patches it in place.
    const bumped = [row("s1", 90), row("s2", 200)];
    const ids = mergeScopeRows(
      mine,
      bumped,
      true,
      true,
      "m3",
      "s2",
      "updated_at",
      mineBoundary,
      sharedBoundary,
    ).rows.map((r) => r.id);
    // Rows above the fetched shared cursor stay visible, the bumped row moves
    // up, and m3 (below that cursor) is still buffered until shared advances.
    expect(ids).toEqual(["s2", "m1", "s1", "m2"]);
    // Without the fetched boundary the live bumped row would hide everything else.
    expect(mergeScopeRows(mine, bumped, true, true, "m3", "s2").rows.map((r) => r.id)).toEqual([
      "s2",
    ]);
  });
  it("records the cursor row's fetched position as the scope boundary", () => {
    const page = sessionRowsPage([row("a", 30), row("b", 20)], true);
    expect(refreshScopeWindow(undefined, page, 30).boundary).toMatchObject({
      id: "b",
      updated_at: 20,
    });
    const appended = appendScopePage(
      refreshScopeWindow(undefined, page, 30),
      sessionRowsPage([row("c", 10)], true),
      30,
    );
    expect(appended.boundary).toMatchObject({ id: "c", updated_at: 10 });
  });
  it("keeps the older boundary when a capped refresh overlaps the loaded window", () => {
    const loaded = refreshScopeWindow(
      undefined,
      sessionRowsPage([row("a", 50), row("b", 40), row("old", 10)], true),
      100,
    );
    const capped = { ...loaded, windowSize: 240 };
    // The refresh returns only the newest rows and ends inside the loaded window,
    // so the older cursor (and its fetched position) still marks completeness.
    const next = refreshScopeWindow(
      capped,
      sessionRowsPage([row("a", 60), row("b", 40)], true),
      100,
    );
    expect(next.pages[0].last_id).toBe("old");
    expect(next.boundary).toMatchObject({ id: "old", updated_at: 10 });
  });
  it("falls back to the last loaded row when the cursor row was filtered out", () => {
    const page = { ...sessionRowsPage([row("a", 30), row("b", 20)], true), last_id: "side_chat" };
    const window = refreshScopeWindow(undefined, page, 30);
    expect(window.boundary).toMatchObject({ id: "b", updated_at: 20 });
    // b's agent becomes active; the merge still measures against b as fetched.
    const bumped = [row("a", 30), row("b", 90)];
    expect(
      mergeScopeRows(
        bumped,
        [row("s1", 25), row("s2", 10)],
        true,
        true,
        "side_chat",
        "s2",
        "updated_at",
        window.boundary,
        { id: "s2", updated_at: 10, created_at: 0 },
      ).rows.map((r) => r.id),
    ).toEqual(["b", "a", "s1"]);
  });
  it("ignores exhausted and disabled scopes as pagination boundaries", () => {
    expect(
      mergeScopeRows([row("m", 20)], [row("s", 100)], true, false).rows.map((r) => r.id),
    ).toEqual(["s", "m"]);
    expect(mergeScopeRows([row("m", 20)], [], false, false).rows).toHaveLength(1);
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
    const cursor = (session: Conversation) =>
      sortBy === "title" ? titleToken(session) : session.id;
    const page = (rows: Conversation[], hasMore = false) => ({
      ...sessionRowsPage(rows, hasMore),
      last_id: cursor(rows[rows.length - 1]),
    });
    expect(
      mergeScopeRows(
        [newest, oldest],
        [middle],
        true,
        true,
        cursor(oldest),
        cursor(middle),
        sortBy,
      ).rows.map((r) => r.id),
    ).toEqual(["new", "middle"]);
    const current = {
      pages: [page([newest, middle], true)],
      pageParams: [undefined],
      windowSize: 2,
    };
    const appended = appendScopePage(current, page([oldest]), 1, sortBy);
    expect(appended.pages[0].data.map((r) => r.id)).toEqual(["new", "middle", "old"]);
    const refreshed = refreshScopeWindow(
      appended,
      page([newest, { ...middle, updated_at: 99 }], true),
      2,
      sortBy,
    );
    expect(refreshed.pages[0].data.map((r) => r.id)).toEqual(["new", "middle", "old"]);
    expect(refreshed.pages[0].last_id).toBe(cursor(oldest));
  },
);

it("bounds a Name merge at each token's position after a capped refresh", () => {
  const titled = (id: string, title: string) => ({ ...row(id, 0), title });
  const cached = [titled("b0", "B000"), titled("b1", "B001"), titled("b2", "B002")];
  const current = {
    pages: [{ ...sessionRowsPage(cached, false), last_id: titleToken(cached[2]) }],
    pageParams: [undefined],
    windowSize: 4,
  };
  // Another client adds A000-A002; the capped refresh fetches only two of them.
  const prefix = [titled("a0", "A000"), titled("a1", "A001")];
  const refreshed = refreshScopeWindow(
    current,
    { ...sessionRowsPage(prefix, true), last_id: titleToken(prefix[1]) },
    2,
    "title",
  );
  const mine = refreshed.pages[0];
  expect(mine.data.map((r) => r.id)).toEqual(["a0", "a1", "b0", "b1", "b2"]);
  expect(mine.last_id).toBe(titleToken(prefix[1]));
  // The sidebar also passes the fetched boundary; Name bounds at the token instead.
  const mergeIds = (mineRows: Conversation[], shared: Conversation[], sharedCursor?: string) =>
    mergeScopeRows(
      mineRows,
      shared,
      true,
      Boolean(sharedCursor),
      mine.last_id,
      sharedCursor,
      "title",
      refreshed.boundary,
    ).rows.map((r) => r.id);
  expect(mergeIds(mine.data, [])).toEqual(["a0", "a1"]);
  // A shared tail before Mine's sets the boundary instead.
  const shared = [titled("s0", "A0005")];
  expect(mergeIds(mine.data, shared, titleToken(shared[0]))).toEqual(["a0", "s0"]);
  // A boundary row renamed since the fetch leaves the token's position in place.
  const renamed = mine.data.map((r) => (r.id === "a1" ? { ...r, title: "Zulu" } : r));
  expect(mergeIds(renamed, [])).toEqual(["a0"]);
  // Mine's token still bounds the merge when none of its rows are loaded.
  expect(mergeIds([], [...shared, titled("s1", "B000")])).toEqual(["s0"]);
  // A non-ASCII key decodes from the token's UTF-8 bytes.
  const accented = [titled("u0", "Ä"), titled("u1", "Ö"), titled("u2", "Ü")];
  const merged = mergeScopeRows(accented, [], true, false, titleToken(accented[1]), null, "title");
  expect(merged.rows.map((r) => r.id)).toEqual(["u0", "u1"]);
});

it("decodes server-issued Name tokens and ignores malformed ones", () => {
  const titled = (id: string, title: string) => ({ ...row(id, 0), title });
  const rows = [titled("a", "Apple"), titled("e", "Éclair \u{1F600}"), titled("o", "Øresund")];
  // Copied from the server's _encode_title_cursor for rows[1].
  const serverToken = "title.WyIww4ljbGFpciDwn5iAIiwiZSJd";
  expect(titleToken(rows[1])).toBe(serverToken);
  const shared = [titled("u", "Übung")];
  const mergedIds = (cursor: string) =>
    mergeScopeRows(rows, shared, true, false, cursor, null, "title").rows.map((r) => r.id);
  expect(mergedIds(serverToken)).toEqual(["a", "e"]);
  // Bad base64, non-JSON, a string or three-member payload, and non-string members bound at
  // Mine's last row, like an unknown id.
  for (const malformed of [
    "title.!!",
    "title.bm90IGpzb24",
    "title.ImFiIg",
    "title.WyIwYSIsIngiLCJ5Il0",
    "title.WzEsMl0",
  ]) {
    expect(mergedIds(malformed)).toEqual(["a", "e", "o"]);
  }
});

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

it("truncates long name keys to the server's 512 code points", () => {
  expect(titleSortKey(`A${"b".repeat(600)}`)).toBe(`0a${"b".repeat(510)}`);
  // Astral characters count once, as in the server's stored key.
  const emoji = titleSortKey("\u{1F600}".repeat(600));
  expect(Array.from(emoji)).toHaveLength(512);
  expect(emoji).toBe(`0${"\u{1F600}".repeat(511)}`);
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
