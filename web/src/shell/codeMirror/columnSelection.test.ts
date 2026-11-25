import { EditorSelection, EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { EDITOR_COMMAND_RUNS } from "./codeMirrorEditorCommands";
import { columnBoxSelection, columnSelection } from "./columnSelection";
import type { FileEditorCommandId } from "@/components/fileEditorCommands";

const views: EditorView[] = [];
const originalClientRects = Range.prototype.getClientRects;
const originalBoundingRect = Range.prototype.getBoundingClientRect;

// jsdom has no layout; CodeMirror measures ranges while scrolling the cursor into view.
beforeAll(() => {
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => new DOMRect();
});

afterAll(() => {
  Range.prototype.getClientRects = originalClientRects;
  Range.prototype.getBoundingClientRect = originalBoundingRect;
});

afterEach(() => {
  for (const created of views.splice(0)) created.destroy();
});

function editorView(doc: string, cursor: number, readOnly = false): EditorView {
  const created = new EditorView({
    parent: document.body,
    state: EditorState.create({
      doc,
      selection: EditorSelection.single(cursor),
      extensions: [
        EditorState.allowMultipleSelections.of(true),
        EditorState.readOnly.of(readOnly),
        columnSelection,
      ],
    }),
  });
  views.push(created);
  return created;
}

function selectedTexts(target: EditorView | EditorState): string[] {
  const state = target instanceof EditorView ? target.state : target;
  return state.selection.ranges.map((range) => state.sliceDoc(range.from, range.to));
}

function run(target: EditorView, id: FileEditorCommandId, times = 1): void {
  for (let step = 0; step < times; step++) expect(EDITOR_COMMAND_RUNS[id](target)).toBe(true);
}

describe("columnBoxSelection", () => {
  const state = EditorState.create({
    doc: "alpha one\nbeta\ngamma three\n",
    extensions: EditorState.allowMultipleSelections.of(true),
  });

  it("selects the same columns on every line of the box", () => {
    const selection = columnBoxSelection(state, {
      anchor: { line: 1, column: 1 },
      head: { line: 3, column: 3 },
    });
    expect(selectedTexts(state.update({ selection }).state)).toEqual(["lp", "et", "am"]);
  });

  it("puts a cursor at the end of a line that stops before the box", () => {
    const selection = columnBoxSelection(state, {
      anchor: { line: 1, column: 6 },
      head: { line: 3, column: 9 },
    });
    const next = state.update({ selection }).state;
    expect(selectedTexts(next)).toEqual(["one", "", "thr"]);
    expect(next.selection.ranges[1].head).toBe(state.doc.line(2).to);
  });

  it("keeps the drag's direction, with the main range on the head's line", () => {
    const selection = columnBoxSelection(state, {
      anchor: { line: 3, column: 4 },
      head: { line: 1, column: 1 },
    });
    expect(selection.main.head).toBe(1);
    expect(selection.main.anchor).toBe(4);
    expect(selection.ranges.every((range) => range.head < range.anchor)).toBe(true);
  });

  it("counts tabs as visual columns", () => {
    const tabbed = EditorState.create({
      doc: "\tab\n    cd",
      extensions: EditorState.allowMultipleSelections.of(true),
    });
    const selection = columnBoxSelection(tabbed, {
      anchor: { line: 1, column: 4 },
      head: { line: 2, column: 6 },
    });
    expect(selectedTexts(tabbed.update({ selection }).state)).toEqual(["ab", "cd"]);
  });
});

describe("pressing with ⌥", () => {
  // jsdom has no layout: each line is 10px tall and each character 10px wide.
  const doc = "alpha one\nbeta two\ngamma three\n";
  function laidOut(view: EditorView): void {
    vi.spyOn(view, "posAtCoords").mockImplementation(((coords: { x: number; y: number }) => {
      const line = view.state.doc.line(Math.floor(coords.y / 10) + 1);
      return Math.min(line.from + Math.floor(coords.x / 10), line.to);
    }) as EditorView["posAtCoords"]);
    vi.spyOn(view, "coordsAtPos").mockImplementation((position: number) => {
      const line = view.state.doc.lineAt(position);
      const left = (position - line.from) * 10;
      const top = (line.number - 1) * 10;
      return { left, right: left, top, bottom: top + 10 };
    });
  }
  function press(view: EditorView, init: MouseEventInit) {
    const event = new MouseEvent("mousedown", { button: 0, altKey: true, ...init });
    const styles = view.state.facet(EditorView.mouseSelectionStyle);
    const style = styles.map((make) => make(view, event)).find((made) => made !== null);
    if (!style) throw new Error("no mouse selection style took the press");
    return style;
  }
  const at = (clientX: number, clientY: number) =>
    new MouseEvent("mousemove", { clientX, clientY, altKey: true });

  it("adds one cursor for a plain ⌥-click that doesn't move", () => {
    const view = editorView(doc, 0);
    laidOut(view);
    const style = press(view, { clientX: 32, clientY: 15 });
    // Under the drag threshold: a single added cursor, not a box.
    const selection = style.get(at(34, 16), false, true);
    expect(selection.ranges.map((range) => [range.from, range.to])).toEqual([
      [0, 0],
      [13, 13],
    ]);
  });

  it("draws a box from the main cursor to a ⇧⌥-click, without a drag", () => {
    const view = editorView(doc, 1);
    laidOut(view);
    const style = press(view, { clientX: 32, clientY: 25, shiftKey: true });
    const selection = style.get(at(32, 25), false, false);
    view.dispatch({ selection });
    expect(selectedTexts(view)).toEqual(["lp", "et", "am"]);
  });

  it("lets a Column select command grow the box a drag drew, from all of its lines", () => {
    const view = editorView("alpha one\nbeta two\ngamma three\ndelta four", 0);
    laidOut(view);
    const style = press(view, { clientX: 2, clientY: 5 });
    view.dispatch({ selection: style.get(at(32, 25), false, false), userEvent: "select.pointer" });
    expect(selectedTexts(view)).toEqual(["alp", "bet", "gam"]);

    run(view, "file.columnSelectDown");
    expect(selectedTexts(view)).toEqual(["alp", "bet", "gam", "del"]);
    run(view, "file.columnSelectRight");
    expect(selectedTexts(view)).toEqual(["alph", "beta", "gamm", "delt"]);
  });
});

describe("Column select commands", () => {
  it("grows a box down and right from the cursor, and shrinks it back", () => {
    const editor = editorView("one two\nthree four\nfive six", 1);
    run(editor, "file.columnSelectDown", 2);
    expect(editor.state.selection.ranges).toHaveLength(3);
    run(editor, "file.columnSelectRight", 2);
    expect(selectedTexts(editor)).toEqual(["ne", "hr", "iv"]);
    run(editor, "file.columnSelectUp");
    run(editor, "file.columnSelectLeft");
    expect(selectedTexts(editor)).toEqual(["n", "h"]);
  });

  it("stops at the first and last lines", () => {
    const editor = editorView("ab\ncd", 0);
    run(editor, "file.columnSelectUp");
    expect(editor.state.selection.ranges).toHaveLength(1);
    run(editor, "file.columnSelectDown", 3);
    expect(editor.state.selection.ranges).toHaveLength(2);
  });

  it("doesn't grow right past the box's widest line", () => {
    const editor = editorView("abc\nde", 0);
    run(editor, "file.columnSelectDown");
    run(editor, "file.columnSelectRight", 6);
    expect(selectedTexts(editor)).toEqual(["abc", "de"]);
  });

  it("starts a new box after the selection changes another way", () => {
    const editor = editorView("abcd\nefgh\nijkl", 0);
    run(editor, "file.columnSelectDown");
    editor.dispatch({ selection: EditorSelection.single(7) });
    run(editor, "file.columnSelectDown");
    expect(selectedTexts(editor)).toEqual(["", ""]);
    expect(editor.state.selection.ranges.map((range) => range.head)).toEqual([7, 12]);
  });

  it("works in a read-only file", () => {
    const editor = editorView("ab\ncd", 0, true);
    run(editor, "file.columnSelectDown");
    run(editor, "file.columnSelectRight");
    expect(selectedTexts(editor)).toEqual(["a", "c"]);
  });
});
