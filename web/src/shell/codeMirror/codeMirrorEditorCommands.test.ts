import { foldedRanges } from "@codemirror/language";
import { EditorSelection, EditorState, type Extension } from "@codemirror/state";
import { EditorView, keymap, runScopeHandlers } from "@codemirror/view";
import { codeMirrorKeys } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { editability } from "./codeMirrorEditing";
import { FILE_EDITOR_COMMANDS, fileEditorCommandLabel } from "@/components/fileEditorCommands";
import {
  baseEditorKeymap,
  cursorHistory,
  EDITOR_COMMAND_RUNS,
  editorCommandKeymap,
} from "./codeMirrorEditorCommands";
import { indentationFolding } from "./codeMirrorFolding";
import { detectedIndentation } from "./codeMirrorIndentation";

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
  for (const view of views.splice(0)) view.destroy();
});

/** An editor over `doc`, where `|` marks the cursor and `[` `]` a selection. */
function editor(
  marked: string,
  { path = "example.ts", editable = true }: { path?: string; editable?: boolean } = {},
): EditorView {
  const anchor = marked.indexOf("[");
  const cursor = marked.indexOf("|");
  const doc = marked.replace(/[|[\]]/g, "");
  let selection = EditorSelection.single(0);
  if (cursor >= 0) selection = EditorSelection.single(cursor);
  if (anchor >= 0) selection = EditorSelection.single(anchor, marked.indexOf("]") - 1);
  const extensions: Extension[] = [
    editability(editable, () => ({ onChange: () => {}, onSave: () => {}, onBlur: () => {} }), path),
    detectedIndentation(doc),
    indentationFolding(),
    EditorState.allowMultipleSelections.of(true),
    keymap.of(baseEditorKeymap),
    cursorHistory,
  ];
  const view = new EditorView({
    parent: document.body,
    state: EditorState.create({ doc, selection, extensions }),
  });
  views.push(view);
  return view;
}

function run(view: EditorView, id: string): boolean {
  const command = FILE_EDITOR_COMMANDS.find((candidate) => candidate.id === id);
  if (!command) throw new Error(`no command ${id}`);
  return EDITOR_COMMAND_RUNS[command.id](view);
}

function press(view: EditorView, key: string, init: KeyboardEventInit = {}): boolean {
  return runScopeHandlers(view, new KeyboardEvent("keydown", { key, ...init }), "editor");
}

describe("editor commands", () => {
  it("gives every command a unique id, a label, and keys that don't collide", () => {
    const ids = FILE_EDITOR_COMMANDS.map((command) => command.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect([...ids].sort()).toEqual(Object.keys(EDITOR_COMMAND_RUNS).sort());
    const keys = FILE_EDITOR_COMMANDS.flatMap((command) => codeMirrorKeys(command.id));
    expect(new Set(keys).size).toBe(keys.length);
    for (const command of FILE_EDITOR_COMMANDS)
      expect(fileEditorCommandLabel(command)).toBeTruthy();
  });

  it("toggles line comments with the language's tokens", () => {
    const view = editor("const a = 1;|\nconst b = 2;");
    run(view, "file.toggleLineComment");
    expect(view.state.doc.line(1).text).toBe("// const a = 1;");
    run(view, "file.toggleLineComment");
    expect(view.state.doc.line(1).text).toBe("const a = 1;");

    const python = editor("x = 1|", { path: "script.py" });
    run(python, "file.toggleLineComment");
    expect(python.state.doc.toString()).toBe("# x = 1");
  });

  it("wraps a selection in a block comment and leaves a language without comments alone", () => {
    const view = editor("[a + b]");
    run(view, "file.toggleBlockComment");
    expect(view.state.doc.toString()).toBe("/* a + b */");

    const text = editor("plain|", { path: "notes.txt" });
    run(text, "file.toggleLineComment");
    expect(text.state.doc.toString()).toBe("plain");
  });

  it("indents and outdents by the file's own unit", () => {
    const view = editor("if (a) {\n  b();|\n}");
    run(view, "file.indentLines");
    expect(view.state.doc.line(2).text).toBe("    b();");
    run(view, "file.outdentLines");
    run(view, "file.outdentLines");
    expect(view.state.doc.line(2).text).toBe("b();");
  });

  it("moves, copies and deletes lines", () => {
    const view = editor("one\ntwo|\nthree");
    run(view, "file.moveLinesUp");
    expect(view.state.doc.toString()).toBe("two\none\nthree");
    run(view, "file.moveLinesDown");
    expect(view.state.doc.toString()).toBe("one\ntwo\nthree");
    run(view, "file.copyLinesDown");
    expect(view.state.doc.toString()).toBe("one\ntwo\ntwo\nthree");
    run(view, "file.deleteLines");
    expect(view.state.doc.toString()).toBe("one\ntwo\nthree");
  });

  it("inserts an indented line above or below", () => {
    const view = editor("  first|\n  second");
    run(view, "file.insertLineAbove");
    expect(view.state.doc.toString()).toBe("  \n  first\n  second");
    expect(view.state.selection.main.head).toBe(2);

    const below = editor("  fir|st\n  second");
    run(below, "file.insertLineBelow");
    expect(below.state.doc.toString()).toBe("  first\n  \n  second");
  });

  it("trims trailing whitespace", () => {
    const view = editor("a  \nb\t|");
    run(view, "file.trimTrailingWhitespace");
    expect(view.state.doc.toString()).toBe("a\nb");
  });

  // Adding a cursor above or below needs line geometry; the browser e2e covers it.
  it("adds occurrences to the selection, and undoes a cursor change", () => {
    const words = editor("[foo] bar foo foo");
    run(words, "file.selectNextOccurrence");
    expect(words.state.selection.ranges).toHaveLength(2);
    run(words, "file.undoCursor");
    expect(words.state.selection.ranges).toHaveLength(1);

    const all = editor("[foo] bar foo foo");
    run(all, "file.selectAllOccurrences");
    expect(all.state.selection.ranges).toHaveLength(3);
  });

  it("undoes only cursor changes, never an edit, and leaves Undo to edits", () => {
    const view = editor("abc|");
    view.dispatch(view.state.replaceSelection("x"));
    expect(run(view, "file.undoCursor")).toBe(false);
    expect(run(view, "file.undoCursor")).toBe(false);
    expect(view.state.doc.toString()).toBe("abcx");

    view.dispatch({ selection: EditorSelection.single(1) });
    view.dispatch({ selection: EditorSelection.single(2) });
    expect(run(view, "file.undoCursor")).toBe(true);
    expect(view.state.selection.main.head).toBe(1);
    expect(run(view, "file.undoCursor")).toBe(true);
    expect(view.state.selection.main.head).toBe(4);
    expect(run(view, "file.undoCursor")).toBe(false);
    expect(view.state.doc.toString()).toBe("abcx");

    expect(press(view, "z", { ctrlKey: true })).toBe(true);
    expect(view.state.doc.toString()).toBe("abc");
  });

  it("selects all occurrences from a bare caret and from several selections", () => {
    const caret = editor("f|oo bar foo foo");
    expect(run(caret, "file.selectAllOccurrences")).toBe(true);
    expect(caret.state.selection.ranges).toHaveLength(3);

    const several = editor("[foo] bar foo foo");
    run(several, "file.selectNextOccurrence");
    expect(run(several, "file.selectAllOccurrences")).toBe(true);
    expect(several.state.selection.ranges).toHaveLength(3);

    const blank = editor("foo  |  bar");
    expect(run(blank, "file.selectAllOccurrences")).toBe(false);
  });

  it("expands the selection by a line on each repeat and stops at the end", () => {
    const view = editor("one\ntw|o\nthree");
    const selected = () =>
      view.state.sliceDoc(view.state.selection.main.from, view.state.selection.main.to);
    run(view, "file.expandLineSelection");
    expect(selected()).toBe("two\n");
    run(view, "file.expandLineSelection");
    expect(selected()).toBe("two\nthree");
    run(view, "file.expandLineSelection");
    expect(selected()).toBe("two\nthree");
  });

  it("jumps to the matching bracket", () => {
    const brackets = editor("f|(a, b)");
    run(brackets, "file.goToBracket");
    expect(brackets.state.selection.main.head).toBe(7);
  });

  it("folds and unfolds indented blocks", () => {
    const view = editor("class A {|\n  one() {}\n  two() {}\n}");
    run(view, "file.fold");
    expect(foldedRanges(view.state).size).toBe(1);
    run(view, "file.unfold");
    expect(foldedRanges(view.state).size).toBe(0);
    run(view, "file.foldAll");
    expect(foldedRanges(view.state).size).toBe(1);
    run(view, "file.unfoldAll");
    expect(foldedRanges(view.state).size).toBe(0);
  });

  it("refuses edits in a read-only file but still navigates and folds", () => {
    const view = editor("one\ntwo|\nthree", { editable: false });
    run(view, "file.moveLinesUp");
    run(view, "file.deleteLines");
    expect(view.state.doc.toString()).toBe("one\ntwo\nthree");
    expect(editorCommandKeymap(false).map((binding) => binding.key)).not.toContain("Shift-Mod-k");
    expect(editorCommandKeymap(false).map((binding) => binding.key)).toContain("Mod-d");
  });
});

describe("editorCommandKeymap", () => {
  it("binds the registry's chords in CodeMirror's key names", () => {
    const keys = editorCommandKeymap(true).flatMap((binding) => (binding.key ? [binding.key] : []));
    expect(keys).toEqual(
      expect.arrayContaining(["Mod-/", "Alt-Shift-a", "Mod-]", "Mod-Shift-k", "Ctrl-Shift-["]),
    );
    expect(keys).toEqual(FILE_EDITOR_COMMANDS.flatMap((command) => codeMirrorKeys(command.id)));
  });
});

describe("editor keys", () => {
  it("keeps the indentation on Enter", () => {
    const view = editor("  indented|");
    expect(press(view, "Enter")).toBe(true);
    expect(view.state.doc.toString()).toBe("  indented\n  ");
  });

  it("closes brackets and quotes in code but not in prose", () => {
    // closeBrackets reacts to typed input, so drive its input handler directly.
    const typed = editor("|");
    const handled = typed.state
      .facet(EditorView.inputHandler)
      .some((handler) => handler(typed, 0, 0, "(", () => typed.state.update({})));
    expect(handled).toBe(true);
    expect(typed.state.doc.toString()).toBe("()");

    const prose = editor("|", { path: "README.md" });
    const proseHandled = prose.state
      .facet(EditorView.inputHandler)
      .some((handler) => handler(prose, 0, 0, "(", () => prose.state.update({})));
    expect(proseHandled).toBe(false);
  });

  it("binds the Monaco-style keys", () => {
    const view = editor("one|\ntwo");
    expect(press(view, "ArrowDown", { altKey: true })).toBe(true);
    expect(view.state.doc.toString()).toBe("two\none");
  });
});

/** A keydown fired at the editor as the browser would, then seen by the window's listeners. */
function keydown(view: EditorView, init: KeyboardEventInit & { keyCode?: number }): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  // CodeMirror reads the legacy keyCode to match Shift with a punctuation key.
  if (init.keyCode !== undefined) Object.defineProperty(event, "keyCode", { value: init.keyCode });
  view.contentDOM.dispatchEvent(event);
  return event;
}

// App hotkeys on the window yield to a keydown the focused editor already handled.
describe("key routing between the editor and app hotkeys", () => {
  const outdent = { key: "[", code: "BracketLeft", ctrlKey: true };
  const comment = { key: "/", code: "Slash", ctrlKey: true };
  const sidebar = { key: "[", code: "BracketLeft", ctrlKey: true, altKey: true };

  it("claims the session-step and shortcuts chords in an editable file, even as a no-op", () => {
    const view = editor("x|");
    expect(keydown(view, outdent).defaultPrevented).toBe(true);
    expect(view.state.doc.toString()).toBe("x");
    expect(keydown(view, comment).defaultPrevented).toBe(true);
    expect(view.state.doc.toString()).toBe("// x");
  });

  it("leaves those chords to the app in a read-only file", () => {
    const view = editor("  x|", { editable: false });
    expect(keydown(view, outdent).defaultPrevented).toBe(false);
    expect(keydown(view, comment).defaultPrevented).toBe(false);
    expect(view.state.doc.toString()).toBe("  x");
  });

  it("never claims the sidebar toggles, which fold in other editors", () => {
    for (const editable of [true, false]) {
      const view = editor("class A {|\n  one() {}\n}", { editable });
      expect(keydown(view, sidebar).defaultPrevented).toBe(false);
      expect(foldedRanges(view.state).size).toBe(0);
    }
  });

  it("folds with Control+Shift+[ in an editable or read-only file", () => {
    for (const editable of [true, false]) {
      const view = editor("class A {|\n  one() {}\n}", { editable });
      const fold = { key: "{", code: "BracketLeft", ctrlKey: true, shiftKey: true, keyCode: 219 };
      expect(keydown(view, fold).defaultPrevented).toBe(true);
      expect(foldedRanges(view.state).size).toBe(1);
    }
  });

  it("undoes a toggled comment in one step", () => {
    const view = editor("x|");
    keydown(view, comment);
    keydown(view, { key: "z", code: "KeyZ", ctrlKey: true });
    expect(view.state.doc.toString()).toBe("x");
  });

  describe("on macOS, where ⌥ changes the typed character", () => {
    const platform = Object.getOwnPropertyDescriptor(Navigator.prototype, "platform");
    beforeAll(() => {
      Object.defineProperty(navigator, "platform", { configurable: true, value: "MacIntel" });
    });
    afterAll(() => {
      delete (navigator as unknown as Record<string, unknown>).platform;
      if (platform) Object.defineProperty(Navigator.prototype, "platform", platform);
    });

    const blockComment = { key: "Å", code: "KeyA", altKey: true, shiftKey: true };

    it("runs ⇧⌥A by its physical key", () => {
      const view = editor("[a + b]");
      expect(keydown(view, blockComment).defaultPrevented).toBe(true);
      expect(view.state.doc.toString()).toBe("/* a + b */");
    });

    it("leaves AltGr and composition input alone", () => {
      const view = editor("[a + b]");
      const altGraph = new KeyboardEvent("keydown", {
        ...blockComment,
        bubbles: true,
        cancelable: true,
      });
      altGraph.getModifierState = (key: string) => key === "AltGraph";
      view.contentDOM.dispatchEvent(altGraph);
      expect(keydown(view, { ...blockComment, isComposing: true }).defaultPrevented).toBe(false);
      expect(view.state.doc.toString()).toBe("a + b");
    });
  });

  describe("⌘U through the editor's whole keymap", () => {
    const cursorUndo = { key: "u", code: "KeyU", ctrlKey: true };

    it("never undoes an edit, right after it or once cursor history runs out", () => {
      const view = editor("abc|");
      view.dispatch(view.state.replaceSelection("x"));
      expect(keydown(view, cursorUndo).defaultPrevented).toBe(true);
      expect(view.state.doc.toString()).toBe("abcx");

      view.dispatch({ selection: EditorSelection.single(1) });
      keydown(view, cursorUndo);
      expect(view.state.selection.main.head).toBe(4);
      keydown(view, cursorUndo);
      keydown(view, cursorUndo);
      expect(view.state.doc.toString()).toBe("abcx");
    });

    it("leaves Undo and Redo to edits", () => {
      const view = editor("abc|");
      view.dispatch(view.state.replaceSelection("x"));
      keydown(view, { key: "z", code: "KeyZ", ctrlKey: true });
      expect(view.state.doc.toString()).toBe("abc");
      keydown(view, { key: "y", code: "KeyY", ctrlKey: true });
      expect(view.state.doc.toString()).toBe("abcx");
    });
  });
});
