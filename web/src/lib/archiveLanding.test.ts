import { describe, expect, it } from "vitest";

import { archiveLanding, type ArchiveLandingInputs } from "./archiveLanding";

function inputs(overrides: Partial<ArchiveLandingInputs> = {}): ArchiveLandingInputs {
  const live = new Set(["a", "b", "c", "child", "grandchild"]);
  const parents = new Map([
    ["child", "b"],
    ["grandchild", "child"],
  ]);
  return {
    history: ["b", "a"],
    sidebarEntryIds: ["c", "a", "b"],
    isLive: (id) => live.has(id),
    parentOf: (id) => parents.get(id) ?? null,
    listState: "loaded",
    ...overrides,
  };
}

const leaving = (...ids: string[]) => new Set(ids);

describe("archiveLanding", () => {
  it("opens the most recent live visit other than the archived session", () => {
    expect(archiveLanding(leaving("b"), inputs())).toEqual({
      kind: "entry",
      entryId: "a",
      landed: "recent",
    });
  });

  it("falls back to sidebar order, the ⌃Tab order, when no visit is left", () => {
    expect(archiveLanding(leaving("b"), inputs({ history: ["b"] }))).toEqual({
      kind: "entry",
      entryId: "c",
      landed: "sidebar_order",
    });
  });

  it("skips the archived session's whole subtree, however deep", () => {
    const landing = archiveLanding(leaving("b"), inputs({ history: ["grandchild", "child", "a"] }));
    expect(landing).toMatchObject({ entryId: "a", landed: "recent" });
  });

  it("falls back to sidebar order when a bulk archive covers every recent visit", () => {
    expect(archiveLanding(leaving("a", "b"), inputs())).toMatchObject({
      entryId: "c",
      landed: "sidebar_order",
    });
  });

  it("goes to New session only when the loaded list has nothing left", () => {
    expect(archiveLanding(leaving("a", "b", "c"), inputs())).toEqual({
      kind: "new_session",
    });
  });

  it("waits while the list loads, unless a live visit is already known", () => {
    expect(archiveLanding(leaving("b"), inputs({ history: ["b"], listState: "loading" }))).toEqual({
      kind: "pending",
    });
    expect(archiveLanding(leaving("b"), inputs({ listState: "loading" }))).toMatchObject({
      entryId: "a",
      landed: "recent",
    });
  });

  it("doesn't treat a list that failed to load as empty", () => {
    expect(archiveLanding(leaving("b"), inputs({ history: ["b"], listState: "failed" }))).toEqual({
      kind: "list_failed",
    });
  });
});
