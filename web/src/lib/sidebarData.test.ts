import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import {
  dedupeSessionRows,
  mergeScopeRows,
  refreshScopeWindow,
  appendScopePage,
  sessionRowsPage,
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
  it("retains family context below the raw cursor without admitting unrelated older rows", () => {
    const family = { id: "family", representative_id: "old", pinned: false };
    const mine = [
      { ...row("recent", 100), family },
      row("cursor", 80),
      { ...row("old", 10), family },
      row("unrelated", 5),
    ];
    const merged = mergeScopeRows(mine, [row("shared", 90)], true, false, "cursor");
    expect(merged.watermark?.id).toBe("cursor");
    expect(merged.rows.map((item) => item.id)).toEqual(["recent", "shared", "cursor", "old"]);
  });
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
  it("retains history when a filtered cursor precedes appended family context", () => {
    const family = { id: "family", representative_id: "representative", pinned: false };
    const current = {
      pages: [
        {
          ...sessionRowsPage(
            [row("recent", 100), row("history", 70), row("older", 40), row("representative", 10)],
            true,
          ),
          last_id: "older",
        },
      ],
      pageParams: [undefined],
      windowSize: 240,
    };
    const incoming = {
      ...sessionRowsPage(
        [
          { ...row("recent", 100), family },
          { ...row("representative", 10), family },
        ],
        true,
      ),
      last_id: "side_chat",
      page_boundary: { id: "recent", created_at: 0, updated_at: 100 },
    };
    const refreshed = refreshScopeWindow(current, incoming, 100);
    expect(refreshed.pages[0].data.map((item) => item.id)).toEqual([
      "recent",
      "history",
      "older",
      "representative",
    ]);
    expect(refreshed.pages[0].last_id).toBe("older");
    expect(
      appendScopePage(refreshed, sessionRowsPage([row("next", 30)]), 30).pages[0].data.map(
        (item) => item.id,
      ),
    ).toEqual(["recent", "history", "older", "next", "representative"]);
  });
  it("retains loaded history beyond the raw cursor when expansion appends older context", () => {
    const family = { id: "family", representative_id: "old", pinned: false };
    const current = {
      pages: [sessionRowsPage([row("cursor", 30), row("history", 20)], true)],
      pageParams: [undefined],
      windowSize: 240,
    };
    const incoming = {
      ...sessionRowsPage(
        [
          { ...row("cursor", 30), family },
          { ...row("old", 10), family },
        ],
        true,
      ),
      last_id: "cursor",
    };
    expect(refreshScopeWindow(current, incoming, 100).pages[0].data.map((item) => item.id)).toEqual(
      ["cursor", "history", "old"],
    );
  });
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

it("uses creation time for scope boundaries, appended pages, and capped refreshes", () => {
  const newest = { ...row("new", 10), created_at: 30 };
  const middle = { ...row("middle", 20), created_at: 20 };
  const oldest = { ...row("old", 30), created_at: 10 };
  expect(
    mergeScopeRows([newest, oldest], [middle], true, true, "old", "middle", "created_at").rows.map(
      (r) => r.id,
    ),
  ).toEqual(["new", "middle"]);
  const current = {
    pages: [sessionRowsPage([newest, middle], true)],
    pageParams: [undefined],
    windowSize: 2,
  };
  const appended = appendScopePage(current, sessionRowsPage([oldest]), 1, "created_at");
  expect(appended.pages[0].data.map((r) => r.id)).toEqual(["new", "middle", "old"]);
  const refreshed = refreshScopeWindow(
    appended,
    sessionRowsPage([newest, { ...middle, updated_at: 99 }], true),
    2,
    "created_at",
  );
  expect(refreshed.pages[0].data.map((r) => r.id)).toEqual(["new", "middle", "old"]);
  expect(refreshed.pages[0].last_id).toBe("old");
});
