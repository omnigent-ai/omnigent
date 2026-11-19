import { afterEach, describe, expect, it, vi } from "vitest";
import { createScopedReviewMemory } from "./scopedReviewMemory";

const STORAGE_KEY = "test.scopedReviewMemory";
const memory = createScopedReviewMemory({
  storageKey: STORAGE_KEY,
  changeEvent: "test:scoped-review-memory-change",
  maxEntries: 3,
});

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  memory.resetUnsaved();
});

describe("createScopedReviewMemory", () => {
  it("keeps each review's paths and values apart", () => {
    memory.write("changes:one", "a.ts", "abc");
    memory.write("changes:one", "b.ts", "");
    memory.write("pull-request:two", "a.ts", "xyz");
    expect([...memory.read("changes:one")]).toEqual([
      ["a.ts", "abc"],
      ["b.ts", ""],
    ]);
    expect([...memory.read("pull-request:two")]).toEqual([["a.ts", "xyz"]]);
  });

  it("stores an empty value as the bare path, so earlier choices still read", () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(["changes:one\u0000README.md"]));
    expect([...memory.read("changes:one")]).toEqual([["README.md", ""]]);
    memory.write("changes:one", "docs/a.md", "");
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY)!)).toEqual([
      "changes:one\u0000README.md",
      "changes:one\u0000docs/a.md",
    ]);
  });

  it("replaces a path's value, forgets it, and keeps only the newest entries", () => {
    memory.write("scope", "a.ts", "1");
    memory.write("scope", "a.ts", "2");
    expect(memory.read("scope").get("a.ts")).toBe("2");
    memory.write("scope", "a.ts", null);
    expect(memory.read("scope").has("a.ts")).toBe(false);
    for (const path of ["b.ts", "c.ts", "d.ts", "e.ts"]) memory.write("scope", path, "");
    expect([...memory.read("scope").keys()]).toEqual(["c.ts", "d.ts", "e.ts"]);
  });

  it("returns the same map until a choice changes", () => {
    memory.write("scope", "a.ts", "1");
    const first = memory.read("scope");
    expect(memory.read("scope")).toBe(first);
    memory.write("scope", "b.ts", "1");
    expect(memory.read("scope")).not.toBe(first);
  });

  it("reads corrupt storage as remembering nothing", () => {
    window.localStorage.setItem(STORAGE_KEY, "{not json");
    expect(memory.read("scope").size).toBe(0);
  });

  it("keeps choices for this session when storage refuses them", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(memory.write("scope", "a.ts", "1")).toBe(false);
    expect(memory.read("scope").get("a.ts")).toBe("1");
  });

  it("tells subscribers about changes from this window and others", () => {
    const listener = vi.fn();
    const unsubscribe = memory.subscribe(listener);
    memory.write("scope", "a.ts", "1");
    window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
    window.dispatchEvent(new StorageEvent("storage", { key: "unrelated" }));
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
    memory.write("scope", "b.ts", "1");
    expect(listener).toHaveBeenCalledTimes(2);
  });
});
