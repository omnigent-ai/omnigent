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
    const next = appendScopePage(current, sessionRowsPage([row("a", 40), row("b", 20)], true));
    expect(next.pages[0].data.map((session) => session.id)).toEqual(["a", "b"]);
    expect(next.windowSize).toBe(60);
    expect(next.pages[0].last_id).toBe("b");
  });
});
