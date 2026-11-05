import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const identity = vi.hoisted(() => ({
  server: "https://omni.example" as string | null,
  user: "alice" as string | null,
}));
vi.mock("./host", () => ({ getOmnigentServerIdentity: () => identity.server }));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => identity.user }));

import {
  readViewed,
  useViewedFiles,
  viewedStorageKey,
  writeViewed,
  type PullRequestIdentity,
} from "./pullRequestViewed";

const KEY_PREFIX = "omnigent:pr-viewed:v1:";
const DAY_MS = 24 * 60 * 60 * 1000;
const SERVER = "https://omni.example";
const PR: PullRequestIdentity = {
  provider: "github",
  host: "github.com",
  repository: "acme/app",
  number: 7,
};

function storedKeys(): string[] {
  return Object.keys(localStorage).filter((key) => key.startsWith(KEY_PREFIX));
}

function failStorageWrites() {
  return vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("full", "QuotaExceededError");
  });
}

function stored(paths: string[], updatedAt = Date.now()): string {
  return JSON.stringify({ v: 1, paths, updatedAt });
}

/** Deliver a `storage` event as another tab's change would. */
function storageEvent(key: string | null, storageArea: Storage = localStorage) {
  act(() => {
    window.dispatchEvent(new StorageEvent("storage", { key, storageArea }));
  });
}

beforeEach(() => {
  localStorage.clear();
  // A cleared-storage event also drops the module's in-memory fallback values.
  window.dispatchEvent(new StorageEvent("storage", { key: null }));
  identity.server = SERVER;
  identity.user = "alice";
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("viewedStorageKey", () => {
  it("isolates servers, users, pull requests and heads", () => {
    const keys = new Set([
      viewedStorageKey("https://a", "alice", PR, "head1"),
      viewedStorageKey("https://b", "alice", PR, "head1"),
      viewedStorageKey("https://a", "bob", PR, "head1"),
      viewedStorageKey("https://a", "alice", { ...PR, number: 70 }, "head1"),
      viewedStorageKey("https://a", "alice", { ...PR, repository: "acme/other" }, "head1"),
      viewedStorageKey("https://a", "alice", { ...PR, host: "ghe.example" }, "head1"),
      viewedStorageKey("https://a", "alice", { ...PR, provider: "gitlab" }, "head1"),
      viewedStorageKey("https://a", "alice", PR, "head2"),
    ]);
    expect(keys.size).toBe(8);
  });

  it("treats a missing provider as GitHub and ignores host and repository case", () => {
    const key = viewedStorageKey("https://a", "alice", PR, "head1");
    expect(viewedStorageKey("https://a", "alice", { ...PR, provider: undefined }, "head1")).toBe(
      key,
    );
    expect(
      viewedStorageKey(
        "https://a",
        "alice",
        { ...PR, host: "GitHub.com", repository: "Acme/App" },
        "head1",
      ),
    ).toBe(key);
  });
});

describe("readViewed / writeViewed", () => {
  const key = viewedStorageKey("https://a", "alice", PR, "head1");

  it("round-trips deduplicated paths", () => {
    expect(writeViewed(key, ["a.ts", "b.ts", "a.ts"])).toEqual({ persisted: true });
    expect(readViewed(key)).toEqual(new Set(["a.ts", "b.ts"]));
  });

  it.each([
    ["malformed JSON", "{not json"],
    ["an unsupported version", JSON.stringify({ v: 2, paths: ["a.ts"], updatedAt: 1 })],
    ["non-string paths", JSON.stringify({ v: 1, paths: [1], updatedAt: 1 })],
    ["a non-array paths field", JSON.stringify({ v: 1, paths: "a.ts", updatedAt: 1 })],
    ["a non-numeric timestamp", JSON.stringify({ v: 1, paths: ["a.ts"], updatedAt: "1" })],
    ["a missing timestamp", JSON.stringify({ v: 1, paths: ["a.ts"] })],
  ])("reads %s as empty", (_label, value) => {
    localStorage.setItem(key, value);
    expect(readViewed(key)).toEqual(new Set());
  });

  it("prunes entries older than 90 days, including corrupt ones, and leaves other keys alone", () => {
    vi.useFakeTimers({ now: 100 * DAY_MS });
    const stale = viewedStorageKey("https://a", "alice", PR, "old");
    const recent = viewedStorageKey("https://a", "alice", { ...PR, number: 2 }, "h");
    writeViewed(stale, ["a.ts"]);
    vi.setSystemTime(150 * DAY_MS);
    writeViewed(recent, ["a.ts"]);
    localStorage.setItem(`${KEY_PREFIX}corrupt`, "{");
    localStorage.setItem("omnigent:unrelated", "keep");
    vi.setSystemTime(191 * DAY_MS);
    writeViewed(key, ["a.ts"]);

    expect(storedKeys().sort()).toEqual([key, recent].sort());
    expect(localStorage.getItem("omnigent:unrelated")).toBe("keep");
  });

  it("keeps only the 500 most recently written entries", () => {
    vi.useFakeTimers({ now: DAY_MS });
    const keys = Array.from({ length: 501 }, (_, index) =>
      viewedStorageKey("https://a", "alice", { ...PR, number: index + 1 }, "h"),
    );
    for (const entryKey of keys.slice(0, 500)) {
      writeViewed(entryKey, ["a.ts"]);
      vi.advanceTimersByTime(1);
    }
    // Rewriting the oldest entry makes the second-oldest the one to evict.
    writeViewed(keys[0], ["b.ts"]);
    vi.advanceTimersByTime(1);
    writeViewed(keys[500], ["a.ts"]);

    expect(localStorage.getItem(keys[0])).not.toBeNull();
    expect(localStorage.getItem(keys[1])).toBeNull();
    expect(storedKeys()).toHaveLength(500);
  });

  it("keeps values in memory when storage throws, until a write succeeds", () => {
    const writes = failStorageWrites();
    expect(writeViewed(key, ["a.ts"])).toEqual({ persisted: false });
    expect(readViewed(key)).toEqual(new Set(["a.ts"]));

    const reads = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    expect(readViewed(key)).toEqual(new Set(["a.ts"]));
    expect(readViewed(viewedStorageKey("https://a", "alice", PR, "head2"))).toEqual(new Set());

    writes.mockRestore();
    reads.mockRestore();
    expect(writeViewed(key, ["a.ts", "b.ts"])).toEqual({ persisted: true });
    expect(JSON.parse(localStorage.getItem(key)!).paths).toEqual(["a.ts", "b.ts"]);
  });
});

describe("useViewedFiles", () => {
  const key = viewedStorageKey(SERVER, "alice", PR, "head1");

  it("is unavailable until server, user, PR and head are known", () => {
    expect(renderHook(() => useViewedFiles(null, "head1")).result.current.available).toBe(false);
    expect(renderHook(() => useViewedFiles(PR, null)).result.current.available).toBe(false);
    expect(renderHook(() => useViewedFiles(PR, "")).result.current.available).toBe(false);
    identity.user = null;
    expect(renderHook(() => useViewedFiles(PR, "head1")).result.current.available).toBe(false);
    identity.user = "alice";
    identity.server = null;
    const { result } = renderHook(() => useViewedFiles(PR, "head1"));
    expect(result.current.available).toBe(false);
    act(() => result.current.toggle("a.ts"));
    expect(result.current.viewed.size).toBe(0);
    expect(storedKeys()).toEqual([]);
  });

  it("persists toggles and bulk changes across remounts and isolates users", () => {
    const first = renderHook(() => useViewedFiles(PR, "head1"));
    expect(first.result.current.available).toBe(true);
    act(() => first.result.current.toggle("a.ts"));
    act(() => first.result.current.setMany(["b.ts", "c.ts", "d.ts"], true));
    act(() => first.result.current.toggle("c.ts"));
    act(() => first.result.current.setMany(["d.ts", "missing.ts"], false));
    expect(first.result.current.viewed).toEqual(new Set(["a.ts", "b.ts"]));
    first.unmount();

    expect(renderHook(() => useViewedFiles(PR, "head1")).result.current.viewed).toEqual(
      new Set(["a.ts", "b.ts"]),
    );
    identity.user = "bob";
    expect(renderHook(() => useViewedFiles(PR, "head1")).result.current.viewed.size).toBe(0);
  });

  it("keeps simultaneous consumers in one tab in sync", () => {
    const first = renderHook(() => useViewedFiles(PR, "head1"));
    const second = renderHook(() => useViewedFiles(PR, "head1"));
    act(() => first.result.current.toggle("a.ts"));
    expect(second.result.current.viewed).toEqual(new Set(["a.ts"]));
    act(() => second.result.current.toggle("a.ts"));
    expect(first.result.current.viewed.size).toBe(0);
  });

  it("starts a new head empty, keeps each head's marks, and notices the reset", () => {
    const { result, rerender } = renderHook(({ head }) => useViewedFiles(PR, head), {
      initialProps: { head: "head1" },
    });
    act(() => result.current.toggle("a.ts"));
    expect(result.current.resetNotice).toBe(false);

    rerender({ head: "head2" });
    expect(result.current.viewed.size).toBe(0);
    expect(result.current.resetNotice).toBe(true);
    expect(renderHook(() => useViewedFiles(PR, "head2")).result.current.resetNotice).toBe(true);

    // A tab still on the older head writes its own key and leaves the newer head alone.
    act(() => result.current.toggle("b.ts"));
    const stale = renderHook(() => useViewedFiles(PR, "head1"));
    act(() => stale.result.current.toggle("old.ts"));
    expect(result.current.viewed).toEqual(new Set(["b.ts"]));
    expect(stale.result.current.viewed).toEqual(new Set(["a.ts", "old.ts"]));
  });

  it("bases the reset notice on whether marks changed on a head", () => {
    const { result, rerender } = renderHook(({ head }) => useViewedFiles(PR, head), {
      initialProps: { head: "head1" },
    });
    // Unmarking nothing on a fresh head records nothing, so the next head has no notice.
    act(() => result.current.setMany(["a.ts"], false));
    rerender({ head: "head2" });
    expect(result.current.resetNotice).toBe(false);
    expect(storedKeys()).toEqual([]);

    act(() => result.current.toggle("b.ts"));
    rerender({ head: "head3" });
    expect(result.current.resetNotice).toBe(true);
    // Clearing the only mark on this head must not bring the notice back.
    act(() => result.current.toggle("c.ts"));
    act(() => result.current.toggle("c.ts"));
    expect(result.current.viewed.size).toBe(0);
    expect(result.current.resetNotice).toBe(false);
  });

  it("reports unsaved marks from the store, across remounts and identity changes", () => {
    failStorageWrites();
    const first = renderHook(({ pr }) => useViewedFiles(pr, "head1"), {
      initialProps: { pr: PR },
    });
    act(() => first.result.current.toggle("a.ts"));
    expect(first.result.current.persisted).toBe(false);
    expect(first.result.current.viewed).toEqual(new Set(["a.ts"]));
    first.rerender({ pr: { ...PR, number: 8 } });
    expect(first.result.current.persisted).toBe(true);
    first.unmount();

    const remounted = renderHook(() => useViewedFiles(PR, "head1"));
    expect(remounted.result.current.persisted).toBe(false);
    expect(remounted.result.current.viewed).toEqual(new Set(["a.ts"]));
  });

  it("follows writes and clears from another tab, even over unsaved marks", () => {
    const { result } = renderHook(() => useViewedFiles(PR, "head1"));
    localStorage.setItem(key, stored(["remote.ts"]));
    storageEvent(key);
    expect(result.current.viewed).toEqual(new Set(["remote.ts"]));

    const writes = failStorageWrites();
    act(() => result.current.toggle("local.ts"));
    expect(result.current.viewed).toEqual(new Set(["remote.ts", "local.ts"]));
    writes.mockRestore();
    localStorage.setItem(key, stored(["remote.ts"]));
    storageEvent(key);
    expect(result.current.viewed).toEqual(new Set(["remote.ts"]));
    expect(result.current.persisted).toBe(true);

    failStorageWrites();
    act(() => result.current.toggle("local.ts"));
    vi.restoreAllMocks();
    localStorage.clear();
    storageEvent(null);
    expect(result.current.viewed.size).toBe(0);
    act(() => result.current.toggle("next.ts"));
    expect(readViewed(key)).toEqual(new Set(["next.ts"]));
  });

  it("ignores sessionStorage events", () => {
    const { result } = renderHook(() => useViewedFiles(PR, "head1"));
    act(() => result.current.toggle("saved.ts"));
    failStorageWrites();
    act(() => result.current.toggle("unsaved.ts"));

    storageEvent(null, sessionStorage);
    storageEvent(key, sessionStorage);
    expect(result.current.viewed).toEqual(new Set(["saved.ts", "unsaved.ts"]));
    expect(result.current.persisted).toBe(false);
  });
});
