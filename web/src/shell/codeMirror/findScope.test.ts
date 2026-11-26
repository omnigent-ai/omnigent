import { history } from "@codemirror/commands";
import { SearchQuery, openSearchPanel, search, setSearchQuery } from "@codemirror/search";
import { EditorSelection, EditorState, Text } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_FIND_OPTIONS } from "@/components/FindBar";
import { applyFindQuery, FindMatchIndex, findStatusLabel, stepToMatch } from "./codeMirrorFind";
import { FindReplacer } from "./codeMirrorReplace";
import { type FindScheduler, inlineMatchSource } from "./findMatchSource";
import {
  canToggleFindInSelection,
  findScope,
  findScopeCandidate,
  findScopeOf,
  toggleFindInSelection,
} from "./findScope";
import { MatchCollector } from "./findMatchCollector";
import { createFindWorkerHandler, type FindWorkerBatch } from "./findMatchWorkerProtocol";

const views: EditorView[] = [];

afterEach(() => {
  for (const view of views.splice(0)) view.destroy();
});

const DOC = ["alpha one", "alpha two", "alpha three", "alpha four"].join("\n");
const LINE_2 = { from: 10, to: 19 };
const LINE_3 = { from: 20, to: 31 };

/** Runs every slice at once; each clock read passes the slice budget. */
const immediate: FindScheduler = {
  schedule: (task) => {
    task();
    return () => {};
  },
  now: (() => {
    let time = 0;
    return () => (time += 100);
  })(),
};

function editor(selection: EditorSelection = EditorSelection.single(0)) {
  const view = new EditorView({
    parent: document.body,
    state: EditorState.create({
      doc: DOC,
      selection,
      extensions: [EditorState.allowMultipleSelections.of(true), search(), history(), findScope],
    }),
  });
  views.push(view);
  return view;
}

function query(view: EditorView, text: string, regexp = false, replace = "") {
  view.dispatch({ effects: setSearchQuery.of(new SearchQuery({ search: text, regexp, replace })) });
}

function indexFor(view: EditorView) {
  const index = new FindMatchIndex(() => {}, inlineMatchSource(immediate));
  index.sync(view.state);
  return index;
}

/** Selects lines 2 and 3 as the reader would, then turns Find in selection on. */
function scopedToLines2And3(view: EditorView) {
  view.dispatch({ selection: { anchor: LINE_2.from, head: LINE_3.to }, userEvent: "select" });
  expect(toggleFindInSelection(view, true)).toBe(true);
}

describe("findScope", () => {
  it("takes the reader's selection, not a match find selected", () => {
    const view = editor();
    view.dispatch({ selection: { anchor: LINE_2.from, head: LINE_2.to }, userEvent: "select" });
    view.dispatch({ selection: { anchor: 0, head: 5 }, userEvent: "select.search" });
    expect(findScopeCandidate(view.state)).toEqual([LINE_2]);
    // A query's jump to the top keeps it; a click that collapses the selection lets it go.
    expect(applyFindQuery(view, "three", DEFAULT_FIND_OPTIONS)).toBe(true);
    expect(view.state.selection.main.head).toBe(0);
    expect(findScopeCandidate(view.state)).toEqual([LINE_2]);
    view.dispatch({ selection: { anchor: 3 }, userEvent: "select.pointer" });
    expect(findScopeCandidate(view.state)).toBeNull();
    expect(toggleFindInSelection(view, true)).toBe(false);
  });

  it("lets go of the reader's selection when the editor moves the cursor away itself", () => {
    const view = editor();
    view.dispatch({ selection: { anchor: LINE_2.from, head: LINE_2.to }, userEvent: "select" });
    // Such as jumping to a comment, which names no user event.
    view.dispatch({ selection: { anchor: LINE_3.from } });
    expect(findScopeCandidate(view.state)).toBeNull();
  });

  it("keeps every range of a multiple selection", () => {
    const view = editor(
      EditorSelection.create([
        EditorSelection.range(LINE_2.from, LINE_2.to),
        EditorSelection.range(LINE_3.from, LINE_3.to),
      ]),
    );
    toggleFindInSelection(view, true);
    expect(findScopeOf(view.state)).toEqual([LINE_2, LINE_3]);
  });

  it("follows edits, tints the scope, and turns off", () => {
    const view = editor();
    scopedToLines2And3(view);
    expect(view.dom.querySelectorAll(".cm-find-scope").length).toBeGreaterThan(0);
    view.dispatch({ changes: { from: 0, insert: ">> " } });
    expect(findScopeOf(view.state)).toEqual([{ from: LINE_2.from + 3, to: LINE_3.to + 3 }]);
    toggleFindInSelection(view, false);
    expect(findScopeOf(view.state)).toBeNull();
    expect(view.dom.querySelectorAll(".cm-find-scope")).toHaveLength(0);
  });
});

describe("find in selection", () => {
  it("counts and steps through only the scope's matches", () => {
    const view = editor();
    query(view, "alpha");
    scopedToLines2And3(view);
    const index = indexFor(view);
    view.dispatch({ selection: { anchor: 0 } });
    expect(stepToMatch(view, index, 1)).toBe(true);
    expect(view.state.selection.main.from).toBe(LINE_2.from);
    expect(stepToMatch(view, index, 1)).toBe(true);
    expect(view.state.selection.main.from).toBe(LINE_3.from);
    expect(stepToMatch(view, index, 1)).toBe(true);
    expect(view.state.selection.main.from).toBe(LINE_2.from);
    const status = index.status(view.state);
    expect(status).toMatchObject({ matchCount: 2, inSelection: true });
    expect(findStatusLabel(status)).toBe("1 of 2");
  });

  it("leaves find's match highlights outside the scope wrapped, so they can be hidden", () => {
    const view = editor();
    openSearchPanel(view);
    query(view, "alpha");
    scopedToLines2And3(view);
    const outside = view.dom.querySelectorAll(".cm-find-outside-scope .cm-searchMatch");
    const all = view.dom.querySelectorAll(".cm-searchMatch");
    expect(all).toHaveLength(4);
    expect([...outside].map((match) => match.closest(".cm-line")?.textContent)).toEqual([
      "alpha one",
      "alpha four",
    ]);
  });

  it("re-indexes the whole file once it turns off", () => {
    const view = editor();
    query(view, "alpha");
    scopedToLines2And3(view);
    const index = indexFor(view);
    toggleFindInSelection(view, false);
    expect(index.belongsTo(view.state)).toBe(false);
    index.sync(view.state);
    expect(index.status(view.state)).toMatchObject({ matchCount: 4, inSelection: false });
  });

  it("reads a match's line around the scope, as for ^ and lookbehind", () => {
    const view = editor();
    query(view, "(?<=alpha )t\\w+", true);
    view.dispatch({ selection: { anchor: LINE_2.from + 6, head: LINE_3.to }, userEvent: "select" });
    toggleFindInSelection(view, true);
    const index = indexFor(view);
    expect(index.matches()).toEqual({ froms: [16, 26], tos: [19, 31] });
  });

  it("replaces all only inside the scope, with or without groups", () => {
    for (const [find, replace, regexp] of [
      ["alpha", "omega", false],
      ["(alpha) (\\w+)", "$2 $1", true],
    ] as const) {
      const view = editor();
      query(view, find, regexp, replace);
      scopedToLines2And3(view);
      const index = indexFor(view);
      new FindReplacer(index, immediate).replaceAll(view);
      expect(view.state.doc.toString().split("\n")).toEqual(
        regexp
          ? ["alpha one", "two alpha", "three alpha", "alpha four"]
          : ["alpha one", "omega two", "omega three", "alpha four"],
      );
    }
  });
});

describe("find in selection edge cases", () => {
  function longLineEditor(text: string) {
    const view = new EditorView({
      parent: document.body,
      state: EditorState.create({ doc: text, extensions: [search(), history(), findScope] }),
    });
    views.push(view);
    return view;
  }

  it("leaves out a match that runs past the scope, in the worker and in Replace all", () => {
    const text = `${"x".repeat(6000)} HIT${"x".repeat(20_000)}`;
    const replies: FindWorkerBatch[] = [];
    const handle = createFindWorkerHandler(
      (batch) => replies.push(batch),
      (resume) => resume(),
    );
    handle({ type: "document", documentId: 1, text });
    handle({
      type: "search",
      jobId: 1,
      documentId: 1,
      spec: {
        search: "\\s(HIT)",
        caseSensitive: false,
        wholeWord: false,
        regexp: true,
        scope: [{ from: 0, to: 5500 }],
      },
    });
    expect(replies.flatMap((batch) => [...batch.froms])).toEqual([]);

    const view = longLineEditor(text);
    query(view, "\\s(HIT)", true, " $1CHANGED");
    view.dispatch({ selection: { anchor: 0, head: 5500 }, userEvent: "select" });
    toggleFindInSelection(view, true);
    const index = indexFor(view);
    expect(index.status(view.state).matchCount).toBe(0);
    new FindReplacer(index, immediate).replaceAll(view);
    expect(view.state.doc.toString()).toBe(text);
  });

  it("leaves out a match that starts in the scope and ends past it, counted and drawn", () => {
    const text = "HIT one HIT two";
    const view = longLineEditor(text);
    openSearchPanel(view);
    query(view, "HIT", false, "X");
    // The scope ends inside the second HIT, which starts at 8.
    view.dispatch({ selection: { anchor: 0, head: 9 }, userEvent: "select" });
    toggleFindInSelection(view, true);
    const index = indexFor(view);
    expect(index.status(view.state).matchCount).toBe(1);
    // Only the counted match shows: no part of the one crossing the edge is drawn.
    const shown = [...view.dom.querySelectorAll(".cm-searchMatch")].filter(
      (match) => !match.closest(".cm-find-outside-scope"),
    );
    expect(shown.map((match) => match.textContent)).toEqual(["HIT"]);
    new FindReplacer(index, immediate).replaceAll(view);
    expect(view.state.doc.toString()).toBe("X one HIT two");
  });

  it("looks for edge-crossing matches near the screen only, not through all of a long line", () => {
    const text = `HIT ${"x".repeat(200_000)} HIT`;
    const view = longLineEditor(text);
    openSearchPanel(view);
    query(view, "HIT");
    view.dispatch({ selection: { anchor: 0, head: 2 }, userEvent: "select" });
    const cursors = vi.spyOn(SearchQuery.prototype, "getCursor");
    toggleFindInSelection(view, true);
    // Only the crossing-match marks read the editor's state; find's counting reads the text.
    const windows = cursors.mock.calls
      .filter(([source]) => source instanceof EditorState)
      .map(([, from, to]) => [from, to]);
    cursors.mockRestore();
    const screenEnd = view.visibleRanges.at(-1)!.to;
    expect(screenEnd).toBeLessThan(text.length / 10);
    expect(windows.length).toBeGreaterThan(0);
    for (const [from, to] of windows) {
      expect(from).toBe(0);
      expect(to).toBeLessThanOrEqual(screenEnd + "HIT".length);
    }
    // The first HIT still crosses the scope's edge, so none of it shows.
    const shown = [...view.dom.querySelectorAll(".cm-searchMatch")].filter(
      (match) => !match.closest(".cm-find-outside-scope"),
    );
    expect(shown).toHaveLength(0);
  });

  it("keeps a scope whose text was all removed, matching nothing", () => {
    const view = editor();
    query(view, "alpha");
    view.dispatch({ selection: { anchor: 10, head: 15 }, userEvent: "select" });
    toggleFindInSelection(view, true);
    new FindReplacer(indexFor(view), immediate).replaceAll(view);
    expect(view.state.doc.toString()).toBe("alpha one\n two\nalpha three\nalpha four");
    expect(findScopeOf(view.state)).toEqual([]);
    const index = indexFor(view);
    expect(index.status(view.state)).toMatchObject({ matchCount: 0, inSelection: true });
    new FindReplacer(index, immediate).replaceAll(view);
    expect(view.state.doc.toString()).toBe("alpha one\n two\nalpha three\nalpha four");
    expect(view.dom.querySelectorAll(".cm-find-scope")).toHaveLength(0);
  });

  it.each([
    ["an anchor past the scope", "(alpha)$", 0],
    ["a lookahead past the scope", "alpha(?= two)", 1],
  ])("reads %s in the line's context, and Replace agrees with the count", (_case, find, count) => {
    const scoped = () => {
      const view = editor();
      query(view, find, true, "$&X");
      view.dispatch({ selection: { anchor: 10, head: 15 }, userEvent: "select" });
      toggleFindInSelection(view, true);
      return view;
    };
    const replaced = count > 0 ? DOC.replace("alpha two", "alphaX two") : DOC;
    const view = scoped();
    const index = indexFor(view);
    expect(index.status(view.state).matchCount).toBe(count);
    if (count > 0) {
      view.dispatch({ selection: { anchor: 10, head: 15 } });
      const resumed: (number | null)[] = [];
      new FindReplacer(index, immediate).replaceSelected(view, (resumeFrom) =>
        resumed.push(resumeFrom),
      );
      expect(resumed).toEqual([16]);
      expect(view.state.doc.toString()).toBe(replaced);
    }
    const all = scoped();
    new FindReplacer(indexFor(all), immediate).replaceAll(all);
    expect(all.state.doc.toString()).toBe(replaced);
  });

  it("is available only with a selection or while on", () => {
    const view = editor();
    expect(canToggleFindInSelection(view.state)).toBe(false);
    view.dispatch({ selection: { anchor: 10, head: 15 }, userEvent: "select" });
    expect(canToggleFindInSelection(view.state)).toBe(true);
    toggleFindInSelection(view, true);
    view.dispatch({ selection: { anchor: 0 }, userEvent: "select.pointer" });
    expect(canToggleFindInSelection(view.state)).toBe(true);
  });
});

describe("find in selection's time budget", () => {
  // Every line matches a pattern read across the whole document; the scope is only the last line.
  const LINES = 2000;
  const text = Array.from({ length: LINES }, () => "hit").join("\n");
  const lastLine = { from: text.length - 3, to: text.length };
  /** A clock that passes any budget on each read. */
  const expiring = () => {
    let time = 0;
    return () => (time += 100);
  };

  it("pauses the collector between matches the scope leaves out", () => {
    const collector = new MatchCollector(Text.of(text.split("\n")), {
      search: "(hit)\\s?",
      caseSensitive: false,
      wholeWord: false,
      regexp: true,
      scope: [lastLine],
    });
    const clock = expiring();
    const first = collector.step(8, clock);
    expect(first).toMatchObject({ froms: [], done: false });
    let steps = 1;
    let batch = first;
    while (!batch.done) {
      batch = collector.step(8, clock);
      steps += 1;
      if (batch.froms.length > 0) expect(batch.froms).toEqual([lastLine.from]);
    }
    expect(steps).toBeGreaterThanOrEqual(LINES);
  });

  it("pauses a Replace all with groups between matches the scope leaves out, and can stop", () => {
    const view = new EditorView({
      parent: document.body,
      state: EditorState.create({ doc: text, extensions: [search(), history(), findScope] }),
    });
    views.push(view);
    query(view, "(hit)\\s?", true, "$1X");
    view.dispatch({ selection: { anchor: lastLine.from, head: lastLine.to }, userEvent: "select" });
    toggleFindInSelection(view, true);
    const slices: (() => void)[] = [];
    const scheduler: FindScheduler = {
      schedule: (task) => {
        slices.push(task);
        return () => slices.splice(slices.indexOf(task), 1);
      },
      now: expiring(),
    };
    const replacer = new FindReplacer(indexFor(view), scheduler);
    replacer.replaceAll(view);
    expect(slices).toHaveLength(1);
    for (let slice = 0; slice < 10; slice += 1) slices.shift()!();
    expect(replacer.busy).toBe(true);
    replacer.cancel();
    expect(slices).toHaveLength(0);
    expect(view.state.doc.toString()).toBe(text);

    replacer.replaceAll(view);
    while (slices.length > 0) slices.shift()!();
    expect(view.state.doc.toString()).toBe(`${text.slice(0, lastLine.from)}hitX`);
  });

  it.each([
    ["turns off", (view: EditorView) => toggleFindInSelection(view, false)],
    [
      "moves to other text",
      (view: EditorView) => {
        view.dispatch({ selection: { anchor: 0, head: 10 }, userEvent: "select" });
        toggleFindInSelection(view, true);
      },
    ],
  ])("stops a pending Replace all, without an edit, when the scope %s", (_label, change) => {
    const view = new EditorView({
      parent: document.body,
      state: EditorState.create({ doc: text, extensions: [search(), history(), findScope] }),
    });
    views.push(view);
    query(view, "(hit)\\s?", true, "$1X");
    view.dispatch({ selection: { anchor: lastLine.from, head: lastLine.to }, userEvent: "select" });
    toggleFindInSelection(view, true);
    const slices: (() => void)[] = [];
    const scheduler: FindScheduler = {
      schedule: (task) => {
        slices.push(task);
        return () => slices.splice(slices.indexOf(task), 1);
      },
      now: expiring(),
    };
    const replacer = new FindReplacer(indexFor(view), scheduler);
    replacer.replaceAll(view);
    slices.shift()!();
    expect(replacer.busy).toBe(true);

    change(view);
    while (slices.length > 0) slices.shift()!();
    expect(replacer.busy).toBe(false);
    expect(view.state.doc.toString()).toBe(text);
  });
});
