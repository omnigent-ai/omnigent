import {
  Compartment,
  EditorState,
  type StateEffect,
  Text,
  type TransactionSpec,
} from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  pinnedLineAt,
  revealStickyLine,
  STICKY_LINE_CLASS,
  stickyScroll,
} from "./codeMirrorStickyScroll";

function docOf(lines: string[]): Text {
  return Text.of(lines);
}

const NESTED = [
  "class Outer {",
  "  method() {",
  "    if (ready) {",
  "      first();",
  "    }",
  "  }",
  "}",
];

describe("revealStickyLine", () => {
  let view: EditorView | undefined;
  afterEach(() => view?.destroy());

  it("puts the cursor on the pinned line and scrolls it to its pinned place", () => {
    view = new EditorView({
      parent: document.body,
      state: EditorState.create({ doc: docOf(NESTED) }),
    });
    // Without layout the line height is 0, so give the margin a size to check.
    vi.spyOn(view, "defaultLineHeight", "get").mockReturnValue(18);
    const dispatch = vi.spyOn(view, "dispatch");
    revealStickyLine(view, 3, 2);
    const lineStart = view.state.doc.line(3).from;
    expect(view.state.selection.main.head).toBe(lineStart);
    expect(document.activeElement).toBe(view.contentDOM);
    // It scrolls the line to the top, below the two pinned rows above it.
    const spec = dispatch.mock.calls[0]![0] as TransactionSpec;
    expect((spec.effects as StateEffect<unknown>).value).toMatchObject({
      range: { from: lineStart, to: lineStart },
      y: "start",
      yMargin: 36,
    });
  });

  it("ignores a line that's no longer in the document", () => {
    view = new EditorView({ parent: document.body, state: EditorState.create({ doc: "one" }) });
    revealStickyLine(view, 5, 0);
    expect(view.state.selection.main.head).toBe(0);
  });
});

describe("pinnedLineAt", () => {
  let view: EditorView | undefined;
  let other: EditorView | undefined;
  afterEach(() => {
    view?.destroy();
    other?.destroy();
  });

  function pinnedRow(target: EditorView, line: string, depth: string): HTMLElement {
    const row = document.createElement("div");
    row.className = STICKY_LINE_CLASS;
    row.dataset.line = line;
    row.dataset.depth = depth;
    const text = document.createElement("span");
    row.append(text);
    target.scrollDOM.prepend(row);
    return text;
  }

  it("reads the pinned line and depth from anywhere in a pinned row", () => {
    view = new EditorView({ parent: document.body, state: EditorState.create({ doc: "a\nb" }) });
    expect(pinnedLineAt(view, pinnedRow(view, "2", "1"))).toEqual({ line: 2, depth: 1 });
  });

  it("finds nothing off the pinned rows, in another editor, or on a malformed row", () => {
    view = new EditorView({ parent: document.body, state: EditorState.create({ doc: "a\nb" }) });
    other = new EditorView({ parent: document.body, state: EditorState.create({ doc: "c" }) });
    expect(pinnedLineAt(view, view.contentDOM)).toBeNull();
    expect(pinnedLineAt(view, null)).toBeNull();
    expect(pinnedLineAt(view, pinnedRow(other, "1", "0"))).toBeNull();
    expect(pinnedLineAt(view, pinnedRow(view, "two", "0"))).toBeNull();
  });
});

describe("stickyScroll", () => {
  it("keeps its pinned rows when the viewer reconfigures it with the same choice", () => {
    const lines = new Compartment();
    const view = new EditorView({
      parent: document.body,
      state: EditorState.create({ doc: "a\n  b", extensions: lines.of(stickyScroll) }),
    });
    try {
      const root = view.dom.querySelector(".cm-stickyScroll");
      expect(root).not.toBeNull();
      view.dispatch({ effects: lines.reconfigure(stickyScroll) });
      expect(view.dom.querySelector(".cm-stickyScroll")).toBe(root);
      view.dispatch({ effects: lines.reconfigure([]) });
      expect(view.dom.querySelector(".cm-stickyScroll")).toBeNull();
    } finally {
      view.destroy();
    }
  });
});
