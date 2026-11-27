import { afterEach, describe, expect, it } from "vitest";

import {
  RECENT_SESSION_HISTORY_LIMIT,
  readRecentSessionHistory,
  recentSessionOrder,
  recordSessionVisit,
  steppedRecentIndex,
  writeRecentSessionHistory,
} from "./recentSessionHistory";

const STORAGE_KEY = "omnigent:recent-session-history";

afterEach(() => localStorage.clear());

describe("recordSessionVisit", () => {
  it("puts a new visit first", () => {
    expect(recordSessionVisit(["b", "c"], "a")).toEqual(["a", "b", "c"]);
  });

  it("moves a revisited session to the front instead of truncating after it", () => {
    expect(recordSessionVisit(["a", "b", "c", "d"], "c")).toEqual(["c", "a", "b", "d"]);
  });

  it("keeps a repeated visit to the newest session unchanged", () => {
    expect(recordSessionVisit(["a", "b"], "a")).toEqual(["a", "b"]);
  });

  it("drops the oldest visits past the limit", () => {
    const full = Array.from({ length: RECENT_SESSION_HISTORY_LIMIT }, (_, index) => `s${index}`);
    const next = recordSessionVisit(full, "new");
    expect(next).toHaveLength(RECENT_SESSION_HISTORY_LIMIT);
    expect(next[0]).toBe("new");
    expect(next).not.toContain(`s${RECENT_SESSION_HISTORY_LIMIT - 1}`);
  });
});

describe("recentSessionOrder", () => {
  const liveIn =
    (...ids: string[]) =>
    (id: string) =>
      ids.includes(id);
  const live = liveIn("a", "b", "c", "d");

  it("lists visited sessions most recent first, then the rest in sidebar order", () => {
    expect(recentSessionOrder(["c", "a"], ["a", "b", "c", "d"], live)).toEqual([
      "c",
      "a",
      "b",
      "d",
    ]);
  });

  it("keeps a live visit the sidebar hides, such as one in a collapsed folder", () => {
    expect(recentSessionOrder(["a", "b", "c"], ["a", "c"], live)).toEqual(["a", "b", "c"]);
  });

  it("skips deleted and archived sessions, even when the sidebar lists archives", () => {
    expect(
      recentSessionOrder(["gone", "b", "archived", "a"], ["archived", "a"], liveIn("a", "b")),
    ).toEqual(["b", "a"]);
  });

  it("leaves out never-visited sessions the sidebar does not show", () => {
    expect(recentSessionOrder([], ["c"], live)).toEqual(["c"]);
  });

  it("is empty when no session is live", () => {
    expect(recentSessionOrder(["a"], ["a"], liveIn())).toEqual([]);
  });

  it("lists each session once", () => {
    expect(recentSessionOrder(["a", "a"], ["a", "a", "b"], live)).toEqual(["a", "b"]);
  });
});

describe("steppedRecentIndex", () => {
  it("steps toward older visits and back", () => {
    expect(steppedRecentIndex(["a", "b", "c"], 0, 1)).toBe(1);
    expect(steppedRecentIndex(["a", "b", "c"], 1, -1)).toBe(0);
  });

  it("wraps at both ends", () => {
    expect(steppedRecentIndex(["a", "b", "c"], 2, 1)).toBe(0);
    expect(steppedRecentIndex(["a", "b", "c"], 0, -1)).toBe(2);
  });

  it("passes over sessions that stopped being eligible, in either direction", () => {
    const stillLive = (id: string) => id !== "c";
    expect(steppedRecentIndex(["a", "b", "c", "d"], 1, 1, stillLive)).toBe(3);
    expect(steppedRecentIndex(["a", "b", "c", "d"], 3, -1, stillLive)).toBe(1);
  });

  it("has nowhere to go in an empty order or when nothing is eligible", () => {
    expect(steppedRecentIndex([], 0, 1)).toBeUndefined();
    expect(steppedRecentIndex(["a", "b"], 0, 1, () => false)).toBeUndefined();
  });
});

describe("persisted history", () => {
  it("round-trips through localStorage", () => {
    writeRecentSessionHistory(["a", "b"]);
    expect(readRecentSessionHistory()).toEqual(["a", "b"]);
  });

  it.each([
    { name: "missing", value: null },
    { name: "malformed JSON", value: "{nope" },
    { name: "not a list", value: JSON.stringify({ a: 1 }) },
  ])("reads as empty when $name", ({ value }) => {
    if (value !== null) localStorage.setItem(STORAGE_KEY, value);
    expect(readRecentSessionHistory()).toEqual([]);
  });

  it("keeps only string ids, up to the limit", () => {
    const stored = [1, "a", null, ...Array.from({ length: 60 }, (_, index) => `s${index}`)];
    localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
    const history = readRecentSessionHistory();
    expect(history[0]).toBe("a");
    expect(history).toHaveLength(RECENT_SESSION_HISTORY_LIMIT);
  });
});
