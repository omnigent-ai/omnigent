import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getSeenCommentIds, markCommentsSeen, useSeenCommentIds } from "./useSeenComments";

const STORAGE_KEY = "omnigent:seen-comment-ids";

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("markCommentsSeen", () => {
  it("persists ids with the current wall-clock time in seconds", () => {
    vi.useFakeTimers({ now: 5_000_000 });
    markCommentsSeen(["c1", "c2"]);
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
    expect(stored["c1"]).toBe(5_000);
    expect(stored["c2"]).toBe(5_000);
  });

  it("keeps the first-seen time on repeated marks", () => {
    // First-seen wins: re-opening a file must not refresh the entry's
    // age, or frequently-viewed comments would never be prune-eligible.
    vi.useFakeTimers({ now: 1_000_000 });
    markCommentsSeen(["c1"]);
    vi.setSystemTime(2_000_000);
    markCommentsSeen(["c1"]);
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
    expect(stored["c1"]).toBe(1_000);
  });

  it("does not write storage for an empty id list", () => {
    markCommentsSeen([]);
    // null (not "{}"): an empty mark must be a true no-op, otherwise
    // every FileViewer mount of a comment-less file churns storage.
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("prunes the oldest entries past the 1000-entry cap", () => {
    // Seed a full registry with seenAt 1..1000 (c0 oldest).
    const full = Object.fromEntries(Array.from({ length: 1000 }, (_, i) => [`c${i}`, i + 1]));
    localStorage.setItem(STORAGE_KEY, JSON.stringify(full));
    vi.useFakeTimers({ now: 9_000_000 });
    markCommentsSeen(["new"]);
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
    // Cap holds: 1001st entry evicted exactly one (the oldest), not
    // zero (unbounded growth) and not more (data loss).
    expect(Object.keys(stored)).toHaveLength(1000);
    expect(stored["new"]).toBe(9_000);
    expect(stored["c0"]).toBeUndefined();
    expect(stored["c999"]).toBe(1000);
  });
});

describe("getSeenCommentIds", () => {
  it("returns the persisted ids as a set", () => {
    markCommentsSeen(["c1", "c2"]);
    expect(getSeenCommentIds()).toEqual(new Set(["c1", "c2"]));
  });

  it("returns an empty set when nothing is stored", () => {
    expect(getSeenCommentIds()).toEqual(new Set());
  });

  it("returns a referentially stable set while storage is unchanged", () => {
    // useSyncExternalStore re-renders (or loops) on snapshot identity —
    // a fresh Set per call would make every subscriber re-render forever.
    markCommentsSeen(["c1"]);
    expect(getSeenCommentIds()).toBe(getSeenCommentIds());
  });

  it("treats corrupt storage as empty", () => {
    localStorage.setItem(STORAGE_KEY, "not valid json!!!");
    expect(getSeenCommentIds()).toEqual(new Set());
  });

  it("treats non-object storage values as empty", () => {
    // An array would otherwise leak its indices ("0", "1", ...) as ids.
    localStorage.setItem(STORAGE_KEY, JSON.stringify([1, 2, 3]));
    expect(getSeenCommentIds()).toEqual(new Set());
  });
});

describe("useSeenCommentIds", () => {
  it("updates subscribed components when a comment is marked seen", () => {
    // The reactive contract the inbox depends on: FileViewer marks a
    // comment seen → the sidebar badge / inbox page (subscribed via
    // this hook) re-render with the new set in the same tab. If the
    // listener notification is dropped, this stays stale until reload.
    const { result } = renderHook(() => useSeenCommentIds());
    expect(result.current.has("c1")).toBe(false);
    act(() => markCommentsSeen(["c1"]));
    expect(result.current.has("c1")).toBe(true);
  });

  it("updates when another tab writes the registry (storage event)", () => {
    // Cross-tab contract: marking a comment seen in a second tab's
    // FileViewer must clear it from this tab's inbox. The browser
    // delivers that write as a `storage` event (never fired in the
    // writing tab); if the subscription ignores it, the inbox lists
    // the item until a manual reload — the bug this guards against,
    // since the app disables refetchOnWindowFocus.
    const { result } = renderHook(() => useSeenCommentIds());
    expect(result.current.has("c_other_tab")).toBe(false);
    act(() => {
      // Simulate the other tab: write localStorage directly (no
      // same-tab listener notification), then dispatch the storage
      // event the browser would fire here.
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ c_other_tab: 1_000 }));
      window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
    });
    expect(result.current.has("c_other_tab")).toBe(true);
  });
});
