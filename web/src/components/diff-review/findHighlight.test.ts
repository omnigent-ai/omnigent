import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type FakeHighlight, installFakeHighlights } from "@/test/fakeHighlights";
import {
  clearOwnedFindHighlights,
  DIFF_FIND_HIGHLIGHTS,
  paintOwnedFindHighlights,
  PULL_REQUEST_FIND_HIGHLIGHTS,
  searchMatchRange,
} from "./findHighlight";

let highlights: Map<string, FakeHighlight>;
// The registry is page-wide, as the highlights are: each test clears the owners it made.
const made: object[] = [];
const owner = () => {
  const created = {};
  made.push(created);
  return created;
};
beforeEach(() => {
  highlights = installFakeHighlights();
});
afterEach(() => {
  for (const created of made.splice(0)) clearOwnedFindHighlights(created);
  vi.unstubAllGlobals();
});

const range = () => document.createRange();
const painted = (name: string) => [...(highlights.get(name)?.ranges ?? [])];

describe("find highlight ownership", () => {
  it("keeps another review's ranges when one paints, clears or repaints", () => {
    const first = owner();
    const second = owner();
    const one = range();
    const two = range();
    paintOwnedFindHighlights(first, DIFF_FIND_HIGHLIGHTS, [one], one);
    paintOwnedFindHighlights(second, DIFF_FIND_HIGHLIGHTS, [two], two);
    expect(painted(DIFF_FIND_HIGHLIGHTS.current)).toEqual([one, two]);

    // A repaint replaces the review's own range, never adds to it.
    const moved = range();
    paintOwnedFindHighlights(first, DIFF_FIND_HIGHLIGHTS, [moved], moved);
    expect(painted(DIFF_FIND_HIGHLIGHTS.current)).toEqual([two, moved]);

    clearOwnedFindHighlights(first);
    expect(painted(DIFF_FIND_HIGHLIGHTS.current)).toEqual([two]);
  });

  it("deletes a name once its last owner clears, so nothing reads as painted", () => {
    const review = owner();
    const current = range();
    const other = range();
    paintOwnedFindHighlights(review, DIFF_FIND_HIGHLIGHTS, [current, other], current);
    expect(painted(DIFF_FIND_HIGHLIGHTS.all)).toEqual([other]);
    clearOwnedFindHighlights(review);
    expect(highlights.has(DIFF_FIND_HIGHLIGHTS.current)).toBe(false);
    expect(highlights.has(DIFF_FIND_HIGHLIGHTS.all)).toBe(false);
    // Clearing again, or an owner that never painted, changes nothing.
    clearOwnedFindHighlights(review);
    clearOwnedFindHighlights(owner());
    expect(highlights.size).toBe(0);
  });

  it("keeps a pull request's names apart from the session review's", () => {
    const session = owner();
    const pullRequest = owner();
    const one = range();
    const two = range();
    paintOwnedFindHighlights(session, DIFF_FIND_HIGHLIGHTS, [one], one);
    paintOwnedFindHighlights(pullRequest, PULL_REQUEST_FIND_HIGHLIGHTS, [two], two);
    expect(painted(DIFF_FIND_HIGHLIGHTS.current)).toEqual([one]);
    expect(painted(PULL_REQUEST_FIND_HIGHLIGHTS.current)).toEqual([two]);
    clearOwnedFindHighlights(pullRequest);
    expect(highlights.has(PULL_REQUEST_FIND_HIGHLIGHTS.current)).toBe(false);
    expect(painted(DIFF_FIND_HIGHLIGHTS.current)).toEqual([one]);
  });

  it("moves an owner's ranges when it paints under other names", () => {
    const review = owner();
    const one = range();
    paintOwnedFindHighlights(review, DIFF_FIND_HIGHLIGHTS, [one], one);
    paintOwnedFindHighlights(review, PULL_REQUEST_FIND_HIGHLIGHTS, [one], one);
    expect(highlights.has(DIFF_FIND_HIGHLIGHTS.current)).toBe(false);
    expect(painted(PULL_REQUEST_FIND_HIGHLIGHTS.current)).toEqual([one]);
  });
});

describe("searchMatchRange", () => {
  it("ranges a renamed file's old name in its header, apart from its new one", () => {
    const item = document.createElement("div");
    // As the diff header renders a rename: the old name, then the new one.
    item.attachShadow({ mode: "open" }).innerHTML =
      "<div data-prev-name><bdi>src/old.ts</bdi></div><div data-title><bdi>src/new.ts</bdi></div>";
    const match = { id: "diff:src/new.ts", text: "src/old.ts", lineNumber: 1, from: 4, to: 10 };
    expect(searchMatchRange(item, { ...match, target: "previousPath" })?.toString()).toBe("old.ts");
    expect(
      searchMatchRange(item, { ...match, text: "src/new.ts", target: "currentPath" })?.toString(),
    ).toBe("new.ts");
  });
});
