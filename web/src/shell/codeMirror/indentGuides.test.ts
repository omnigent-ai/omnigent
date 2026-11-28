import { indentUnit } from "@codemirror/language";
import { EditorSelection, EditorState, Text } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, describe, expect, it, vi } from "vitest";
import { detectedIndentation } from "./codeMirrorIndentation";
import {
  activeGuide,
  guideCount,
  indentGuideDecorations,
  indentGuides,
  keepsIndentation,
  lineIndents,
} from "./indentGuides";

const views: EditorView[] = [];

afterEach(() => {
  for (const created of views.splice(0)) created.destroy();
});

const SOURCE = [
  "class Shop:", // 1
  "    def total(self):", // 2
  "        sum = 0", // 3
  "", // 4
  "        for item in self.items:", // 5
  "            sum += item", // 6
  "        return sum", // 7
  "", // 8
  "    def empty(self):", // 9
  "        return False", // 10
  "", // 11
  "print(Shop())", // 12
].join("\n");

function stateAt(doc: string, line: number, column = 0): EditorState {
  const probe = EditorState.create({ doc });
  const from = probe.doc.line(line).from + column;
  return EditorState.create({
    doc,
    selection: EditorSelection.single(from),
    extensions: detectedIndentation(doc),
  });
}

describe("lineIndents", () => {
  const state = stateAt(SOURCE, 1);

  it("counts leading whitespace in columns", () => {
    expect(lineIndents(state, 1, 3)).toEqual([0, 4, 8]);
  });

  it("carries the block's indentation across a blank line inside it", () => {
    expect(lineIndents(state, 4, 4)).toEqual([8]);
  });

  it("drops a blank line to the shallower side where a block ends", () => {
    expect(lineIndents(state, 8, 8)).toEqual([4]);
    expect(lineIndents(state, 11, 11)).toEqual([0]);
  });

  it("counts a tab to the next tab stop", () => {
    const tabbed = EditorState.create({
      doc: "a\n\tb\n  \tc",
      extensions: [EditorState.tabSize.of(4), indentUnit.of("\t")],
    });
    expect(lineIndents(tabbed, 1, 3)).toEqual([0, 4, 4]);
  });
});

describe("guideCount", () => {
  it("shows one guide per started level", () => {
    expect(guideCount(0, 4)).toBe(0);
    expect(guideCount(4, 4)).toBe(1);
    expect(guideCount(8, 4)).toBe(2);
    expect(guideCount(6, 4)).toBe(2);
  });
});

describe("activeGuide", () => {
  it("takes the block the cursor's line sits in", () => {
    expect(activeGuide(stateAt(SOURCE, 3, 8), 4)).toEqual({
      firstLine: 3,
      lastLine: 7,
      column: 4,
    });
  });

  it("takes the block a line opens when the cursor is on its header", () => {
    expect(activeGuide(stateAt(SOURCE, 5, 8), 4)).toEqual({
      firstLine: 6,
      lastLine: 6,
      column: 8,
    });
    expect(activeGuide(stateAt(SOURCE, 1), 4)).toEqual({ firstLine: 2, lastLine: 10, column: 0 });
  });

  it("has none at the top level", () => {
    expect(activeGuide(stateAt(SOURCE, 12), 4)).toBeNull();
  });

  it("looks past blank lines for the body a header opens", () => {
    expect(activeGuide(stateAt("if a:\n\n  b\nc", 1), 2)).toEqual({
      firstLine: 3,
      lastLine: 3,
      column: 0,
    });
    const nested = "class X:\n  def f():\n\n    body\n  def g():\n    body";
    expect(activeGuide(stateAt(nested, 2), 2)).toEqual({ firstLine: 4, lastLine: 4, column: 2 });
  });

  it("takes the block around a blank line inside it", () => {
    expect(activeGuide(stateAt(SOURCE, 4), 4)).toEqual({ firstLine: 3, lastLine: 7, column: 4 });
  });

  it("follows the block no further than the lines on screen", () => {
    const state = stateAt(SOURCE, 3, 8);
    expect(activeGuide(state, 4, { first: 3, last: 5 })).toEqual({
      firstLine: 3,
      lastLine: 5,
      column: 4,
    });
  });
});

describe("indentGuideDecorations", () => {
  function hugeState(cursorLine: number): EditorState {
    const doc = Text.of(Array.from({ length: 240_000 }, (_, index) => `entry${index} = ${index}`));
    return EditorState.create({
      doc,
      selection: EditorSelection.single(doc.line(cursorLine).from),
    });
  }

  function linesRead(run: () => void): number {
    const line = vi.spyOn(Text.prototype, "line");
    try {
      run();
      return line.mock.calls.length;
    } finally {
      line.mockRestore();
    }
  }

  it("reads only the lines on screen and their neighbours in a huge flat file", () => {
    const state = hugeState(120_010);
    const reads = linesRead(() =>
      indentGuideDecorations(state, [{ first: 120_000, last: 120_020 }]),
    );
    expect(reads).toBeLessThan(60);
  });

  it("reads only up to the screen when following a big block", () => {
    const body = Array.from({ length: 50_000 }, (_, index) => `    line${index}`);
    const doc = Text.of(["def big():", ...body, "done()"]);
    const state = EditorState.create({
      doc,
      selection: EditorSelection.single(doc.line(25_000).from),
      extensions: detectedIndentation(doc.toString()),
    });
    const reads = linesRead(() => indentGuideDecorations(state, [{ first: 24_990, last: 25_010 }]));
    expect(reads).toBeLessThan(80);
  });
});

describe("keepsIndentation", () => {
  const before = Text.of(["def f():", "    return 1", ""]);

  function keeps(from: number, to: number, insert: string): boolean {
    const state = EditorState.create({ doc: before });
    const transaction = state.update({ changes: { from, to, insert } });
    return keepsIndentation(transaction.changes, before, transaction.state.doc);
  }

  it("holds for typing after a line's indentation", () => {
    expect(keeps(20, 20, "0")).toBe(true);
    expect(keeps(4, 5, "")).toBe(true);
  });

  it("fails for edits that can move guides", () => {
    expect(keeps(9, 9, "  ")).toBe(false); // into the indentation
    expect(keeps(8, 8, "\n")).toBe(false); // a new line
    expect(keeps(22, 22, "x")).toBe(false); // a blank line gets code
    expect(keeps(13, 21, "")).toBe(false); // a line's code removed
  });
});

describe("indentGuides", () => {
  function render(doc: string, line: number): EditorView {
    const created = new EditorView({
      parent: document.body,
      state: EditorState.create({
        doc,
        selection: EditorSelection.single(EditorState.create({ doc }).doc.line(line).from),
        extensions: [detectedIndentation(doc), indentGuides],
      }),
    });
    views.push(created);
    return created;
  }

  function guides(view: EditorView) {
    return [...view.dom.querySelectorAll<HTMLElement>(".cm-line")].map((line) => ({
      count: line.style.getPropertyValue("--cm-indent-guides"),
      active: line.style.getPropertyValue("--cm-indent-guide-active"),
    }));
  }

  it("draws guides at the file's indent unit, with the cursor's block active", () => {
    const view = render("if a:\n  b\n  if c:\n    d\ne", 4);
    expect(guides(view)).toEqual([
      { count: "", active: "" },
      { count: "1", active: "" },
      { count: "1", active: "" },
      { count: "2", active: "2ch" },
      { count: "", active: "" },
    ]);
    expect(
      view.dom
        .querySelector<HTMLElement>(".cm-indent-guides")
        ?.style.getPropertyValue("--cm-indent-unit"),
    ).toBe("2ch");
  });

  it("moves the active guide with the cursor", () => {
    const view = render("if a:\n  b\n  if c:\n    d\ne", 4);
    view.dispatch({ selection: EditorSelection.single(view.state.doc.line(2).from) });
    expect(guides(view).map((line) => line.active)).toEqual(["", "0ch", "0ch", "0ch", ""]);
  });

  it("keeps its decorations when the cursor moves along a line or typing leaves indents alone", () => {
    const view = render("if a:\n  b\n  if c:\n    d\ne", 4);
    const plugin = view.plugin(indentGuides);
    const drawn = plugin?.decorations;
    view.dispatch({ selection: EditorSelection.single(view.state.doc.line(4).to) });
    expect(plugin?.decorations).toBe(drawn);
    // Typing reads no more lines than the same edit in an editor without guides.
    const plain = new EditorView({
      parent: document.body,
      state: EditorState.create({ doc: view.state.doc }),
    });
    views.push(plain);
    const linesReadBy = (target: EditorView) => {
      const reads = vi.spyOn(Text.prototype, "line");
      target.dispatch({ changes: { from: target.state.doc.line(4).to, insert: "x" } });
      const count = reads.mock.calls.length;
      reads.mockRestore();
      return count;
    };
    expect(linesReadBy(view)).toBe(linesReadBy(plain));
    expect(guides(view)[3]).toEqual({ count: "2", active: "2ch" });
  });

  it("follows edits", () => {
    const view = render("a\nb", 1);
    view.dispatch({ changes: { from: view.state.doc.line(2).from, insert: "    " } });
    expect(guides(view)[1].count).toBe("1");
  });
});
