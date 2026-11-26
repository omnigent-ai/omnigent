import { Compartment, EditorSelection, EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { useFileFinderStore } from "@/store/fileFinder";
import { STICKY_LINE_CLASS } from "./codeMirrorStickyScroll";
import {
  COPY_FAILED_MESSAGE,
  CUT_FAILED_MESSAGE,
  EditorContextMenu,
  isInsideSelection,
  PASTE_FAILED_MESSAGE,
  placeCursorForMenu,
} from "./EditorContextMenu";
import type { CodeMirrorSelectionActions } from "./useCodeMirrorCommentLayer";

const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));
vi.mock("sonner", () => ({ toast: { error: toastError } }));

const views: EditorView[] = [];
const readOnlyCompartment = new Compartment();
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
  cleanup();
  toastError.mockClear();
  for (const view of views.splice(0)) view.destroy();
  useFileFinderStore.setState({
    availableSessionId: null,
    textSearchAvailable: false,
    request: null,
  });
  vi.unstubAllGlobals();
});

const NO_SELECTION_ACTIONS: CodeMirrorSelectionActions = {
  canComment: false,
  canAttachToAgent: false,
  onAddComment: () => {},
  onAttachToAgent: () => {},
};

function editorWithMenu(
  doc: string,
  selection: EditorSelection,
  {
    editable = false,
    selectionActions = NO_SELECTION_ACTIONS,
  }: { editable?: boolean; selectionActions?: CodeMirrorSelectionActions } = {},
) {
  const editorView = new EditorView({
    parent: document.body,
    state: EditorState.create({
      doc,
      selection,
      extensions: [
        EditorState.allowMultipleSelections.of(true),
        readOnlyCompartment.of(EditorState.readOnly.of(!editable)),
      ],
    }),
  });
  views.push(editorView);
  const handlers = { onFind: vi.fn(), onFindAndReplace: vi.fn(), onGoToLine: vi.fn() };
  render(
    <EditorContextMenu
      view={editorView}
      conversationId="session-1"
      editable={editable}
      selectionActions={selectionActions}
      {...handlers}
    />,
  );
  return { view: editorView, handlers };
}

/** A right-click; without layout it lands at the document's start. */
function rightClick(view: EditorView): MouseEvent {
  const event = new MouseEvent("contextmenu", {
    bubbles: true,
    cancelable: true,
    button: 2,
    clientX: 10,
    clientY: 10,
  });
  act(() => {
    view.contentDOM.dispatchEvent(event);
  });
  return event;
}

/** Shift+F10 in the editor, the keyboard's way to open the menu. */
function pressShiftF10(view: EditorView): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    key: "F10",
    shiftKey: true,
    bubbles: true,
    cancelable: true,
  });
  act(() => {
    view.contentDOM.dispatchEvent(event);
  });
  return event;
}

const menuItems = () => screen.queryAllByRole("menuitem").map((item) => item.textContent);

describe("EditorContextMenu", () => {
  it("replaces the browser's menu with the editor's commands", () => {
    const { view } = editorWithMenu("alpha beta", EditorSelection.single(0));
    const event = rightClick(view);
    expect(event.defaultPrevented).toBe(true);
    expect(screen.getByTestId("file-editor-context-menu")).toBeTruthy();
    expect(screen.getByRole("menuitem", { name: /^Find in file/ })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: /^Cut/ })).toBeNull();
  });

  it("runs a shared editor command on the editor", async () => {
    const { view } = editorWithMenu("alpha beta alpha", EditorSelection.single(1));
    rightClick(view);
    await userEvent.click(screen.getByRole("menuitem", { name: /Select all occurrences/ }));
    expect(view.state.selection.ranges.map((range) => [range.from, range.to])).toEqual([
      [0, 5],
      [11, 16],
    ]);
    expect(screen.queryByTestId("file-editor-context-menu")).toBeNull();
  });

  it.each([
    ["Find in file", /^Find in file/, "onFind"],
    ["Find and replace", /^Find and replace in file/, "onFindAndReplace"],
    ["Go to line", /^Go to line/, "onGoToLine"],
  ] as const)("hands %s to the viewer", async (_label, name, handler) => {
    const { view, handlers } = editorWithMenu("a", EditorSelection.single(0), { editable: true });
    rightClick(view);
    await userEvent.click(screen.getByRole("menuitem", { name }));
    expect(handlers[handler]).toHaveBeenCalledWith(view);
  });

  it("searches the workspace for the selection's first line, as ⌃⌘O does", async () => {
    useFileFinderStore.setState({ availableSessionId: "session-1", textSearchAvailable: true });
    const { view } = editorWithMenu("  needle here\nmore", EditorSelection.single(0, 18));
    rightClick(view);
    await userEvent.click(screen.getByRole("menuitem", { name: /Search Text in Workspace/ }));
    expect(useFileFinderStore.getState().request).toMatchObject({
      sessionId: "session-1",
      kind: "text",
      query: "needle here",
    });
  });

  it("offers no workspace search where the finder doesn't serve the session", () => {
    const { view } = editorWithMenu("needle", EditorSelection.single(0, 6));
    rightClick(view);
    expect(menuItems().some((label) => label?.startsWith("Search Text"))).toBe(false);
  });

  it("offers no workspace search where the finder finds files but can't search text", () => {
    useFileFinderStore.setState({ availableSessionId: "session-1", textSearchAvailable: false });
    const { view } = editorWithMenu("needle", EditorSelection.single(0, 6));
    rightClick(view);
    expect(menuItems().some((label) => label?.startsWith("Search Text"))).toBe(false);
  });

  it("offers the comment actions for a selection", async () => {
    const onAddComment = vi.fn();
    const { view } = editorWithMenu("needle", EditorSelection.single(0, 6), {
      selectionActions: { ...NO_SELECTION_ACTIONS, canComment: true, onAddComment },
    });
    rightClick(view);
    await userEvent.click(screen.getByRole("menuitem", { name: "Add comment" }));
    expect(onAddComment).toHaveBeenCalled();
  });

  it("pastes the clipboard's text over the selection in an editable file", async () => {
    vi.stubGlobal("navigator", {
      ...navigator,
      clipboard: { readText: () => Promise.resolve("pasted") },
    });
    const { view } = editorWithMenu("old text", EditorSelection.single(0, 3), { editable: true });
    rightClick(view);
    await userEvent.click(screen.getByRole("menuitem", { name: /^Paste/ }));
    await vi.waitFor(() => expect(view.state.doc.toString()).toBe("pasted text"));
  });

  it("leaves the text and says so when the clipboard can't be read", async () => {
    vi.stubGlobal("navigator", {
      ...navigator,
      clipboard: { readText: () => Promise.reject(new Error("denied")) },
    });
    const { view } = editorWithMenu("old text", EditorSelection.single(0, 3), { editable: true });
    rightClick(view);
    await userEvent.click(screen.getByRole("menuitem", { name: /^Paste/ }));
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith(PASTE_FAILED_MESSAGE));
    expect(view.state.doc.toString()).toBe("old text");
  });

  it.each([
    ["Cut", CUT_FAILED_MESSAGE],
    ["Copy", COPY_FAILED_MESSAGE],
  ])("names %s in the message when the browser refuses it", async (label, message) => {
    const original = document.execCommand;
    const execCommand = vi.fn(() => false);
    document.execCommand = execCommand;
    try {
      const { view } = editorWithMenu("old text", EditorSelection.single(0, 3), {
        editable: true,
      });
      rightClick(view);
      await userEvent.click(screen.getByRole("menuitem", { name: new RegExp(`^${label}`) }));
      expect(execCommand).toHaveBeenCalledWith(label.toLowerCase());
      expect(toastError).toHaveBeenCalledWith(message);
    } finally {
      document.execCommand = original;
    }
  });

  // A column over "alpha\nb" from column 2 to 4: "ph" on the first line, and a cursor after
  // the short second line, which is the main range.
  const COLUMN_WITH_EMPTY_MAIN = EditorSelection.create(
    [EditorSelection.range(2, 4), EditorSelection.cursor(7)],
    1,
  );

  /** A right-click on "ph": without layout, the click's position is given. */
  function rightClickOnPh(view: EditorView): Event {
    vi.spyOn(view, "posAtCoords").mockReturnValue(3);
    return rightClick(view);
  }

  it.each([
    ["a right-click on its text", rightClickOnPh],
    ["Shift+F10", pressShiftF10],
  ])(
    "offers Cut and Copy from %s for a column whose main range is a cursor",
    async (_opener, open) => {
      const original = document.execCommand;
      const execCommand = vi.fn(() => true);
      document.execCommand = execCommand;
      try {
        const { view } = editorWithMenu("alpha\nb", COLUMN_WITH_EMPTY_MAIN, { editable: true });
        // The editor's own menu replaces the browser's, from either opener.
        expect(open(view).defaultPrevented).toBe(true);
        expect(view.state.selection.ranges.map((range) => [range.from, range.to])).toEqual([
          [2, 4],
          [7, 7],
        ]);
        const cut = screen.getByRole("menuitem", { name: /^Cut/ });
        expect(cut).not.toHaveAttribute("aria-disabled", "true");
        await userEvent.click(screen.getByRole("menuitem", { name: /^Copy/ }));
        expect(execCommand).toHaveBeenCalledWith("copy");
      } finally {
        document.execCommand = original;
      }
    },
  );

  function delayedClipboard() {
    let resolve: (text: string) => void = () => {};
    const pending = new Promise<string>((done) => {
      resolve = done;
    });
    vi.stubGlobal("navigator", { ...navigator, clipboard: { readText: () => pending } });
    return (text: string) => resolve(text);
  }

  it("drops a paste whose clipboard read finishes after the file turned read-only", async () => {
    const finishRead = delayedClipboard();
    const { view } = editorWithMenu("old text", EditorSelection.single(0, 3), { editable: true });
    rightClick(view);
    await userEvent.click(screen.getByRole("menuitem", { name: /^Paste/ }));
    view.dispatch({ effects: readOnlyCompartment.reconfigure(EditorState.readOnly.of(true)) });
    expect(view.state.readOnly).toBe(true);
    await act(async () => finishRead("pasted"));
    expect(view.state.doc.toString()).toBe("old text");
  });

  it("drops a paste whose clipboard read finishes after the editor closed", async () => {
    const finishRead = delayedClipboard();
    const { view } = editorWithMenu("old text", EditorSelection.single(0, 3), { editable: true });
    rightClick(view);
    await userEvent.click(screen.getByRole("menuitem", { name: /^Paste/ }));
    view.destroy();
    const dispatch = vi.spyOn(view, "dispatch");
    await act(async () => finishRead("pasted"));
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("puts the cursor on a pinned sticky-scroll line, not the hidden text under it", () => {
    const { view } = editorWithMenu(
      ["class Box:", "    def size(self):", "        return 1", "    pass"].join("\n"),
      EditorSelection.single(0),
    );
    // A pinned row as the sticky scroll draws it, over the text near the top of the view.
    const row = document.createElement("div");
    row.className = STICKY_LINE_CLASS;
    row.dataset.line = "2";
    row.dataset.depth = "1";
    view.scrollDOM.prepend(row);
    const event = new MouseEvent("contextmenu", {
      bubbles: true,
      cancelable: true,
      button: 2,
      clientX: 10,
      clientY: 10,
    });
    act(() => {
      row.dispatchEvent(event);
    });
    expect(event.defaultPrevented).toBe(true);
    expect(view.state.selection.main.head).toBe(view.state.doc.line(2).from);
    expect(screen.getByTestId("file-editor-context-menu")).toBeTruthy();
  });

  it("leaves a right-click outside the text to the page", () => {
    const { view } = editorWithMenu("alpha", EditorSelection.single(0));
    const outside = document.createElement("input");
    view.dom.appendChild(outside);
    const event = new MouseEvent("contextmenu", { bubbles: true, cancelable: true, button: 2 });
    outside.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(screen.queryByTestId("file-editor-context-menu")).toBeNull();
  });
});

describe("placeCursorForMenu", () => {
  function view(selection: EditorSelection): EditorView {
    const created = new EditorView({
      state: EditorState.create({
        doc: "one two three",
        selection,
        extensions: EditorState.allowMultipleSelections.of(true),
      }),
    });
    views.push(created);
    return created;
  }

  it("keeps a selection the click landed on", () => {
    const editor = view(EditorSelection.single(4, 7));
    placeCursorForMenu(editor, 5);
    expect(editor.state.selection.main).toMatchObject({ from: 4, to: 7 });
  });

  it("moves the cursor to a click outside the selection", () => {
    const editor = view(
      EditorSelection.create([EditorSelection.range(0, 3), EditorSelection.cursor(9)]),
    );
    placeCursorForMenu(editor, 11);
    expect(editor.state.selection.ranges).toHaveLength(1);
    expect(editor.state.selection.main.head).toBe(11);
  });

  it("does nothing where the click has no text position", () => {
    const editor = view(EditorSelection.single(2));
    placeCursorForMenu(editor, null);
    expect(editor.state.selection.main.head).toBe(2);
  });

  it("counts a selection's ends as inside it, and a bare cursor as no selection", () => {
    const selection = EditorSelection.create([
      EditorSelection.range(4, 7),
      EditorSelection.cursor(1),
    ]);
    expect(isInsideSelection(selection, 4)).toBe(true);
    expect(isInsideSelection(selection, 7)).toBe(true);
    expect(isInsideSelection(selection, 1)).toBe(false);
  });
});
