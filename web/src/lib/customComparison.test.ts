import { afterEach, describe, expect, it, vi } from "vitest";

import {
  clearCustomComparison,
  commitRevisions,
  shortCommit,
  shownCommit,
  MAXIMUM_REVISION_BYTES,
  readCustomComparison,
  REMEMBERED_SESSIONS,
  subscribeCustomComparisons,
  typedRevisions,
  writeCustomComparison,
} from "./customComparison";

const WS = { hostId: "host_1", root: "/repo" };

afterEach(() => {
  window.localStorage.clear();
  vi.useRealTimers();
});

describe("a session's typed comparison", () => {
  it("is saved per session, and cleared back to the saved base", () => {
    const heard = vi.fn();
    const unsubscribe = subscribeCustomComparisons(heard);
    writeCustomComparison("conv_1", WS, { base: "v1", head: "" });
    expect(readCustomComparison("conv_1", WS)).toEqual({ base: "v1", head: "" });
    expect(readCustomComparison("conv_2", WS)).toBeNull();
    clearCustomComparison("conv_1", WS);
    expect(readCustomComparison("conv_1", WS)).toBeNull();
    expect(heard).toHaveBeenCalledTimes(2);
    unsubscribe();
  });

  it("belongs to the session's workspace, so another root or host doesn't see it", () => {
    writeCustomComparison("conv_1", WS, { base: "v1", head: "" });
    expect(readCustomComparison("conv_1", { hostId: "host_1", root: "/other" })).toBeNull();
    expect(readCustomComparison("conv_1", { hostId: "host_2", root: "/repo" })).toBeNull();
    expect(readCustomComparison("conv_1", WS)).toEqual({ base: "v1", head: "" });
  });

  it("forgets the least recently typed sessions first", () => {
    vi.useFakeTimers();
    for (let index = 0; index <= REMEMBERED_SESSIONS; index += 1) {
      vi.setSystemTime(index);
      writeCustomComparison(`conv_${index}`, WS, { base: "main", head: "" });
    }
    expect(readCustomComparison("conv_0", WS)).toBeNull();
    expect(readCustomComparison(`conv_${REMEMBERED_SESSIONS}`, WS)).not.toBeNull();
  });

  it("ignores storage it can't read", () => {
    window.localStorage.setItem("omnigent.changes.custom-comparisons", "{not json");
    expect(readCustomComparison("conv_1", WS)).toBeNull();
    window.localStorage.setItem(
      "omnigent.changes.custom-comparisons",
      JSON.stringify({
        [JSON.stringify(["conv_1", "host_1", "/repo"])]: { base: "", head: "", savedAt: 1 },
        [JSON.stringify(["conv_2", "host_1", "/repo"])]: "v1",
      }),
    );
    expect([readCustomComparison("conv_1", WS), readCustomComparison("conv_2", WS)]).toEqual([
      null,
      null,
    ]);
  });
});

describe("typedRevisions", () => {
  it("trims both fields, and reads Working Copy or nothing as the working copy", () => {
    expect(typedRevisions("  v1 ", " Working Copy ")).toEqual({ spec: { base: "v1", head: "" } });
    expect(typedRevisions("v1", "")).toEqual({ spec: { base: "v1", head: "" } });
    expect(typedRevisions("main", "feature~2")).toEqual({
      spec: { base: "main", head: "feature~2" },
    });
  });

  it.each([
    ["", "Working Copy", "base", "Enter a branch, tag or commit."],
    ["--output=x", "", "base", "Base can't start with “-”."],
    ["v1", "-x", "head", "Head can't start with “-”."],
    ["v1\u0000", "", "base", "Base can't contain control characters."],
    ["v1", "a\nb", "head", "Head can't contain control characters."],
    ["é".repeat(MAXIMUM_REVISION_BYTES / 2 + 1), "", "base", "Base is too long."],
  ])("refuses %j … %j in its field", (base, head, field, message) => {
    expect(typedRevisions(base, head)).toEqual({ field, message });
  });
});

describe("one commit as a comparison", () => {
  const oid = "0123456789abcdef0123456789abcdef01234567";

  it("compares a commit's first parent with it, and recognizes that comparison", () => {
    expect(commitRevisions(oid)).toEqual({ base: `${oid}~1`, head: oid });
    expect(shownCommit(commitRevisions(oid))).toBe(oid);
    expect(shortCommit(oid)).toBe("0123456");
  });

  it("compares a root commit with the empty tree of its hash format, and recognizes that", () => {
    const sha256 = "a".repeat(64);
    expect(commitRevisions(oid, { root: true })).toEqual({
      base: "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
      head: oid,
    });
    expect(commitRevisions(sha256, { root: true })).toEqual({
      base: "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321",
      head: sha256,
    });
    expect(shownCommit(commitRevisions(oid, { root: true }))).toBe(oid);
    expect(shownCommit(commitRevisions(sha256, { root: true }))).toBe(sha256);
  });

  it("doesn't take other typed comparisons for a commit", () => {
    expect(shownCommit(null)).toBeNull();
    expect(shownCommit({ base: "HEAD~1", head: "HEAD" })).toBeNull();
    expect(shownCommit({ base: "main", head: oid })).toBeNull();
    expect(shownCommit({ base: "abc~1", head: "abc" })).toBeNull();
  });
});
