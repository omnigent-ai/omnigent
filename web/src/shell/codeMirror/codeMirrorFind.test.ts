import { SearchQuery, getSearchQuery, search, setSearchQuery } from "@codemirror/search";
import { EditorState, Text } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_FIND_OPTIONS, type FindOptions } from "@/components/FindBar";
import {
  applyFindQuery,
  FindMatchIndex,
  findQueryFromSelection,
  findStatusLabel,
  setReplaceText,
  stepToMatch,
  type FindStatus,
} from "./codeMirrorFind";
import { type FindScheduler, inlineMatchSource, type MatchSource } from "./findMatchSource";

const views: EditorView[] = [];

function viewOf(doc: string): EditorView {
  const view = new EditorView({
    parent: document.body,
    state: EditorState.create({ doc, extensions: [search()] }),
  });
  views.push(view);
  return view;
}

afterEach(() => {
  for (const view of views.splice(0)) view.destroy();
  vi.restoreAllMocks();
});

/** Slices run only when the test drains them; each slice takes one match. */
function manualSource() {
  const queue: (() => void)[] = [];
  let time = 0;
  const scheduler: FindScheduler = {
    schedule: (task) => {
      queue.push(task);
      return () => {
        const at = queue.indexOf(task);
        if (at >= 0) queue.splice(at, 1);
      };
    },
    // Every read passes the slice budget, so a slice stops after one match.
    now: () => (time += 100),
  };
  return {
    source: inlineMatchSource(scheduler),
    pending: () => queue.length,
    runSlices: (count: number) => {
      for (let step = 0; step < count && queue.length; step += 1) queue.shift()!();
    },
    drain: () => {
      while (queue.length) queue.shift()!();
    },
  };
}

function setQuery(view: EditorView, text: string, options: FindOptions = DEFAULT_FIND_OPTIONS) {
  view.dispatch({ effects: setSearchQuery.of(new SearchQuery({ search: text, ...options })) });
}

function indexed(
  view: EditorView,
  text: string,
  options: FindOptions = DEFAULT_FIND_OPTIONS,
  source: MatchSource = manualSource().source,
) {
  setQuery(view, text, options);
  const index = new FindMatchIndex(() => {}, source);
  index.sync(view.state);
  return index;
}

function drained(view: EditorView, text: string, options: FindOptions = DEFAULT_FIND_OPTIONS) {
  const manual = manualSource();
  const index = indexed(view, text, options, manual.source);
  manual.drain();
  return index;
}

function fullCursorMatches(doc: string, spec: FindOptions & { search: string }) {
  const matches: { from: number; to: number }[] = [];
  const cursor = new SearchQuery(spec).getCursor(Text.of(doc.split("\n")));
  for (let result = cursor.next(); !result.done; result = cursor.next()) {
    matches.push(result.value);
  }
  return matches;
}

function walk(view: EditorView, index: FindMatchIndex, direction: 1 | -1, steps: number) {
  const visited: number[] = [];
  for (let step = 0; step < steps; step += 1) {
    expect(stepToMatch(view, index, direction)).toBe(true);
    visited.push(view.state.selection.main.from);
  }
  return visited;
}

describe("applyFindQuery", () => {
  it("moves to the top so the first match can be selected", () => {
    const view = viewOf("alpha beta alpha");
    view.dispatch({ selection: { anchor: 12 } });
    expect(applyFindQuery(view, "alpha", DEFAULT_FIND_OPTIONS)).toBe(true);
    expect(view.state.selection.main.from).toBe(0);
  });

  it("keeps a selection that already is a match", () => {
    const view = viewOf("alpha beta alpha");
    view.dispatch({ selection: { anchor: 11, head: 16 } });
    expect(applyFindQuery(view, "alpha", DEFAULT_FIND_OPTIONS)).toBe(false);
    expect(view.state.selection.main).toMatchObject({ from: 11, to: 16 });
  });

  it("asks for no match for an invalid pattern or an empty query", () => {
    const view = viewOf("alpha");
    expect(applyFindQuery(view, "(", { ...DEFAULT_FIND_OPTIONS, regexp: true })).toBe(false);
    expect(applyFindQuery(view, "", DEFAULT_FIND_OPTIONS)).toBe(false);
  });
});

describe("FindMatchIndex", () => {
  it("counts matches and the selected one's position", () => {
    const view = viewOf("a b a b a");
    const index = drained(view, "a");
    view.dispatch({ selection: { anchor: 4, head: 5 } });
    expect(index.status(view.state)).toMatchObject({ complete: true, matchCount: 3, ordinal: 2 });
  });

  it.each<[string, string, FindOptions & { search: string }]>([
    ["a literal", "Alpha alpha alphabet\nalpha", { ...DEFAULT_FIND_OPTIONS, search: "alpha" }],
    [
      "a case-sensitive whole word",
      "Alpha alpha alphabet",
      { ...DEFAULT_FIND_OPTIONS, search: "alpha", caseSensitive: true, wholeWord: true },
    ],
    ["line starts", "alpha\nbeta\ngamma", { ...DEFAULT_FIND_OPTIONS, search: "^", regexp: true }],
    ["line ends", "alpha\nbeta\n", { ...DEFAULT_FIND_OPTIONS, search: "$", regexp: true }],
    [
      "a multiline pattern far longer than the query",
      `START\n${"body\n".repeat(20_000)}END`,
      { ...DEFAULT_FIND_OPTIONS, search: "START[\\s\\S]*?END", regexp: true },
    ],
    [
      "a line break in a literal",
      "one\ntwo\none\ntwo",
      { ...DEFAULT_FIND_OPTIONS, search: "one\\ntwo" },
    ],
    [
      "an absent literal on one very long line",
      "x".repeat(300_000),
      { ...DEFAULT_FIND_OPTIONS, search: "not-present" },
    ],
  ])("matches the editor's full-document cursor for %s", (_label, doc, spec) => {
    const view = viewOf(doc);
    const index = drained(view, spec.search, spec);
    const expected = fullCursorMatches(doc, spec);
    expect(index.status(view.state)).toMatchObject({
      complete: true,
      matchCount: expected.length,
    });
    for (const [ordinal, match] of expected.entries()) {
      view.dispatch({ selection: { anchor: match.from, head: match.to } });
      expect(index.status(view.state).ordinal).toBe(ordinal + 1);
    }
  });

  it("reports an invalid pattern without indexing", () => {
    const view = viewOf("a");
    expect(
      drained(view, "[", { ...DEFAULT_FIND_OPTIONS, regexp: true }).status(view.state),
    ).toMatchObject({ valid: false, complete: true, matchCount: 0 });
  });

  it("re-indexes after the document changes", () => {
    const view = viewOf("a a");
    const manual = manualSource();
    const index = indexed(view, "a", DEFAULT_FIND_OPTIONS, manual.source);
    manual.drain();
    view.dispatch({ changes: { from: 3, insert: " a" } });
    expect(index.status(view.state)).toMatchObject({ complete: false });
    index.sync(view.state);
    manual.drain();
    expect(index.status(view.state).matchCount).toBe(3);
  });

  it("stops the previous query's work when the query changes", () => {
    const view = viewOf("a ".repeat(50));
    const manual = manualSource();
    const index = indexed(view, "a", DEFAULT_FIND_OPTIONS, manual.source);
    expect(manual.pending()).toBe(1);
    setQuery(view, "b");
    index.sync(view.state);
    manual.drain();
    expect(index.status(view.state)).toMatchObject({ complete: true, matchCount: 0 });
  });

  it("keeps its matches when only the replacement text changes", () => {
    const view = viewOf("a b a");
    const inline = inlineMatchSource();
    const start = vi.fn(inline.start);
    const index = indexed(view, "a", DEFAULT_FIND_OPTIONS, { start, dispose: inline.dispose });
    expect(start).toHaveBeenCalledTimes(1);
    setReplaceText(view, "z");
    index.sync(view.state);
    expect(start).toHaveBeenCalledTimes(1);
    expect(getSearchQuery(view.state)).toMatchObject({ search: "a", replace: "z" });
    expect(index.status(view.state)).toMatchObject({ replace: "z", matchCount: 2 });
    // A new query keeps the replacement the reader typed.
    applyFindQuery(view, "b", DEFAULT_FIND_OPTIONS);
    expect(getSearchQuery(view.state).replace).toBe("z");
  });

  it("indexes a large file in slices and reads status and steps without rescanning", () => {
    const lines = Array.from(
      { length: 160_000 },
      (_, row) => `export const entry${String(row).padStart(6, "0")} = { id: ${row} };`,
    );
    const view = viewOf(lines.join("\n"));
    const manual = manualSource();
    const index = indexed(view, "const", DEFAULT_FIND_OPTIONS, manual.source);
    expect(index.status(view.state)).toMatchObject({ complete: false });
    expect(findStatusLabel(index.status(view.state))).toMatch(/^(Searching…|\d+\+)$/);
    manual.drain();
    expect(index.status(view.state)).toMatchObject({ complete: true, matchCount: 160_000 });

    const getCursor = vi.spyOn(SearchQuery.prototype, "getCursor");
    const last = view.state.doc.line(160_000);
    view.dispatch({ selection: { anchor: last.from + 7, head: last.from + 12 } });
    expect(index.status(view.state).ordinal).toBe(160_000);
    stepToMatch(view, index, 1);
    expect(index.status(view.state).ordinal).toBe(1);
    stepToMatch(view, index, -1);
    expect(index.status(view.state).ordinal).toBe(160_000);
    expect(getCursor).not.toHaveBeenCalled();
  });
});

describe("stepToMatch", () => {
  it("steps forward and back, wrapping at the ends", () => {
    const view = viewOf("a b a b a");
    const index = drained(view, "a");
    view.dispatch({ selection: { anchor: 0, head: 1 } });
    expect(walk(view, index, 1, 3)).toEqual([4, 8, 0]);
    expect(walk(view, index, -1, 2)).toEqual([8, 4]);
  });

  it("moves past empty matches at line starts in both directions", () => {
    const view = viewOf("alpha\nbeta\ngamma");
    const index = drained(view, "^", { ...DEFAULT_FIND_OPTIONS, regexp: true });
    view.dispatch({ selection: { anchor: 0 } });
    expect(index.status(view.state).ordinal).toBe(1);
    expect(walk(view, index, 1, 3)).toEqual([6, 11, 0]);
    expect(walk(view, index, -1, 3)).toEqual([11, 6, 0]);
  });

  it("moves past empty matches at line ends", () => {
    const view = viewOf("ab\ncd");
    const index = drained(view, "$", { ...DEFAULT_FIND_OPTIONS, regexp: true });
    view.dispatch({ selection: { anchor: 0 } });
    expect(walk(view, index, 1, 3)).toEqual([2, 5, 2]);
  });

  it("waits for the index instead of answering Previous from an unfinished prefix", () => {
    const doc = "hit\n".repeat(50_000);
    const view = viewOf(doc);
    const manual = manualSource();
    const index = indexed(view, "hit", DEFAULT_FIND_OPTIONS, manual.source);
    manual.runSlices(10);
    const lastHit = doc.length - 4;
    view.dispatch({ selection: { anchor: lastHit, head: lastHit + 3 } });
    expect(stepToMatch(view, index, -1)).toBe(false);
    expect(view.state.selection.main.from).toBe(lastHit);
    manual.drain();
    expect(stepToMatch(view, index, -1)).toBe(true);
    expect(view.state.selection.main.from).toBe(lastHit - 4);
  });

  it("answers Next from an unfinished index when a later match is already known", () => {
    const view = viewOf("hit\n".repeat(1_000));
    const manual = manualSource();
    const index = indexed(view, "hit", DEFAULT_FIND_OPTIONS, manual.source);
    manual.runSlices(10);
    view.dispatch({ selection: { anchor: 0, head: 3 } });
    expect(stepToMatch(view, index, 1)).toBe(true);
    expect(view.state.selection.main.from).toBe(4);
    view.dispatch({ selection: { anchor: 3_990, head: 3_993 } });
    expect(stepToMatch(view, index, 1)).toBe(false);
  });

  it("wraps only once the index is complete", () => {
    const view = viewOf("hit hit");
    const manual = manualSource();
    const index = indexed(view, "hit", DEFAULT_FIND_OPTIONS, manual.source);
    view.dispatch({ selection: { anchor: 0, head: 3 } });
    expect(stepToMatch(view, index, -1)).toBe(false);
    manual.drain();
    expect(stepToMatch(view, index, -1)).toBe(true);
    expect(view.state.selection.main.from).toBe(4);
  });
});

describe("findStatusLabel", () => {
  const base: FindStatus = {
    query: "x",
    options: DEFAULT_FIND_OPTIONS,
    replace: "",
    valid: true,
    complete: true,
    matchCount: 0,
    ordinal: 0,
  };
  it.each<[string, Partial<FindStatus>, string]>([
    ["no query", { query: "" }, ""],
    ["an invalid pattern", { valid: false }, "Invalid pattern"],
    ["no matches", {}, "No results"],
    ["a selected match", { matchCount: 12, ordinal: 3 }, "3 of 12"],
    ["matches but none selected", { matchCount: 4 }, "1 of 4"],
    ["indexing without matches yet", { complete: false }, "Searching…"],
    ["indexing with a selected match", { complete: false, matchCount: 40, ordinal: 2 }, "2 of 40+"],
    ["indexing without a selection", { complete: false, matchCount: 40 }, "40+"],
  ])("labels %s", (_label, status, expected) => {
    expect(findStatusLabel({ ...base, ...status })).toBe(expected);
  });
});

it("escapes line breaks in a selection used as the query", () => {
  expect(findQueryFromSelection("one\ntwo")).toBe("one\\ntwo");
  expect(findQueryFromSelection("one\r\ntwo\rthree")).toBe("one\\ntwo\\nthree");
});
