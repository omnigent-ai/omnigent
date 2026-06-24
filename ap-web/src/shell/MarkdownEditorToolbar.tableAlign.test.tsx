// Tests for the TableAlignControls component in MarkdownEditorToolbar.
//
// Covers three behaviours:
//   1. Visibility — buttons appear only when the cursor is inside a table cell.
//   2. Active state — the button matching the column's current alignment is
//      highlighted; no button is highlighted when alignment is unset.
//   3. Dispatch — clicking a button calls setColumnAlign, which iterates over
//      all cells in the current column via prosemirror-tables helpers and
//      dispatches a single transaction that sets the align attr on each cell.
//
// @tiptap/react's useEditorState is a vi.fn() so individual tests can control
// what TableAlignControls sees without touching the toolbar's own state.
// @tiptap/pm/tables is fully mocked so setColumnAlign runs without a real
// ProseMirror document.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tiptap/react", () => ({
  useEditorState: vi.fn(),
}));
vi.mock("@tiptap/markdown", () => ({}));
vi.mock("@tiptap/pm/tables", () => ({
  isInTable: vi.fn(),
  cellAround: vi.fn(),
  colCount: vi.fn(),
  findTable: vi.fn(),
  TableMap: { get: vi.fn() },
}));

import { useEditorState } from "@tiptap/react";
import { TableMap, cellAround, colCount, findTable, isInTable } from "@tiptap/pm/tables";
import type { Editor } from "@tiptap/react";
import { ToolbarPlugin } from "./MarkdownEditorToolbar";
import i18n from "@/i18n";

const t = i18n.getFixedT(null, "common");

// Combined state object for both ToolbarPlugin's and TableAlignControls'
// useEditorState calls — the mock returns the same object for every call.
const TOOLBAR_DEFAULTS = {
  canUndo: false,
  canRedo: false,
  isParagraph: false,
  isH1: false,
  isH2: false,
  isH3: false,
  isBlockquote: false,
  isBold: false,
  isItalic: false,
  isStrike: false,
  isCode: false,
};

type AlignState = { inTable: boolean; align: string | null };

function mockEditorState(align: AlignState) {
  vi.mocked(useEditorState).mockReturnValue({ ...TOOLBAR_DEFAULTS, ...align });
}

// Minimal editor that satisfies ToolbarPlugin without a live ProseMirror view.
// Extended in dispatch tests to include view.state / view.dispatch.
const baseEditor = { getMarkdown: () => "" } as unknown as Editor;

function renderToolbar(editor: Editor = baseEditor) {
  render(
    <ToolbarPlugin
      editor={editor}
      onSave={vi.fn()}
      isSaving={false}
      isDirty={false}
      saveError={false}
      saveDisabled={false}
      hasExternalUpdate={false}
    />,
  );
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

// ---------------------------------------------------------------------------
// Visibility
// ---------------------------------------------------------------------------

describe("TableAlignControls visibility", () => {
  it("hides alignment buttons when the cursor is outside a table", () => {
    mockEditorState({ inTable: false, align: null });
    renderToolbar();
    expect(screen.queryByTitle(t("alignColumnLeft"))).toBeNull();
    expect(screen.queryByTitle(t("alignColumnCenter"))).toBeNull();
    expect(screen.queryByTitle(t("alignColumnRight"))).toBeNull();
  });

  it("shows all three alignment buttons when the cursor is inside a table", () => {
    mockEditorState({ inTable: true, align: null });
    renderToolbar();
    expect(screen.getByTitle(t("alignColumnLeft"))).toBeDefined();
    expect(screen.getByTitle(t("alignColumnCenter"))).toBeDefined();
    expect(screen.getByTitle(t("alignColumnRight"))).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// Active state
// ---------------------------------------------------------------------------

describe("TableAlignControls active state", () => {
  it("marks only the left button active for left-aligned columns", () => {
    mockEditorState({ inTable: true, align: "left" });
    renderToolbar();
    expect(screen.getByTitle(t("alignColumnLeft")).className).toContain("bg-accent");
    expect(screen.getByTitle(t("alignColumnCenter")).className).not.toContain("bg-accent");
    expect(screen.getByTitle(t("alignColumnRight")).className).not.toContain("bg-accent");
  });

  it("marks only the center button active for center-aligned columns", () => {
    mockEditorState({ inTable: true, align: "center" });
    renderToolbar();
    expect(screen.getByTitle(t("alignColumnCenter")).className).toContain("bg-accent");
    expect(screen.getByTitle(t("alignColumnLeft")).className).not.toContain("bg-accent");
    expect(screen.getByTitle(t("alignColumnRight")).className).not.toContain("bg-accent");
  });

  it("marks only the right button active for right-aligned columns", () => {
    mockEditorState({ inTable: true, align: "right" });
    renderToolbar();
    expect(screen.getByTitle(t("alignColumnRight")).className).toContain("bg-accent");
    expect(screen.getByTitle(t("alignColumnLeft")).className).not.toContain("bg-accent");
    expect(screen.getByTitle(t("alignColumnCenter")).className).not.toContain("bg-accent");
  });

  it("marks no button active when the column has no alignment set", () => {
    mockEditorState({ inTable: true, align: null });
    renderToolbar();
    expect(screen.getByTitle(t("alignColumnLeft")).className).not.toContain("bg-accent");
    expect(screen.getByTitle(t("alignColumnCenter")).className).not.toContain("bg-accent");
    expect(screen.getByTitle(t("alignColumnRight")).className).not.toContain("bg-accent");
  });
});

// ---------------------------------------------------------------------------
// Dispatch — setColumnAlign
// ---------------------------------------------------------------------------

describe("setColumnAlign dispatch", () => {
  // Build an editor whose view.state and view.dispatch are wired so
  // setColumnAlign can execute with the mocked prosemirror-tables helpers.
  function makeEditorWithView() {
    const mockSetNodeMarkup = vi.fn().mockReturnThis();
    const mockDispatch = vi.fn();
    const tr = { setNodeMarkup: mockSetNodeMarkup };
    const editor = {
      getMarkdown: () => "",
      view: {
        state: { selection: { $head: {}, $from: {} }, tr },
        dispatch: mockDispatch,
        focus: vi.fn(),
      },
    } as unknown as Editor;
    return { editor, mockSetNodeMarkup, mockDispatch };
  }

  beforeEach(() => {
    mockEditorState({ inTable: true, align: null });
    // Wire prosemirror-tables helpers for a 2-row table at column 2.
    // cellsInRect returns positions [4, 9]; with tableResult.start=1 the
    // absolute positions become 5 and 10.
    vi.mocked(isInTable).mockReturnValue(true);
    vi.mocked(cellAround).mockReturnValue({} as ReturnType<typeof cellAround>);
    vi.mocked(colCount).mockReturnValue(2);
    vi.mocked(findTable).mockReturnValue({
      node: { nodeAt: vi.fn().mockReturnValue({ attrs: { align: null } }) },
      start: 1,
      pos: 0,
      depth: 1,
    } as unknown as ReturnType<typeof findTable>);
    vi.mocked(TableMap.get).mockReturnValue({
      height: 2,
      cellsInRect: vi.fn().mockReturnValue([4, 9]),
    } as unknown as ReturnType<typeof TableMap.get>);
  });

  it("clicking 'center' dispatches one transaction updating both column cells", () => {
    const { editor, mockSetNodeMarkup, mockDispatch } = makeEditorWithView();
    renderToolbar(editor);

    fireEvent.click(screen.getByTitle(t("alignColumnCenter")));

    // One dispatch call carrying the built transaction.
    expect(mockDispatch).toHaveBeenCalledOnce();
    // Both cells at absolute positions 5 and 10 receive align="center".
    expect(mockSetNodeMarkup).toHaveBeenCalledWith(5, null, { align: "center" });
    expect(mockSetNodeMarkup).toHaveBeenCalledWith(10, null, { align: "center" });
  });

  it("clicking 'right' dispatches with align='right' for all column cells", () => {
    const { editor, mockSetNodeMarkup, mockDispatch } = makeEditorWithView();
    renderToolbar(editor);

    fireEvent.click(screen.getByTitle(t("alignColumnRight")));

    expect(mockDispatch).toHaveBeenCalledOnce();
    expect(mockSetNodeMarkup).toHaveBeenCalledWith(5, null, { align: "right" });
    expect(mockSetNodeMarkup).toHaveBeenCalledWith(10, null, { align: "right" });
  });

  it("clicking 'left' dispatches with align='left' for all column cells", () => {
    const { editor, mockSetNodeMarkup, mockDispatch } = makeEditorWithView();
    renderToolbar(editor);

    fireEvent.click(screen.getByTitle(t("alignColumnLeft")));

    expect(mockDispatch).toHaveBeenCalledOnce();
    expect(mockSetNodeMarkup).toHaveBeenCalledWith(5, null, { align: "left" });
    expect(mockSetNodeMarkup).toHaveBeenCalledWith(10, null, { align: "left" });
  });

  it("skips cells that already have the target alignment", () => {
    // Override nodeAt so both cells already carry align="center".
    vi.mocked(findTable).mockReturnValue({
      node: { nodeAt: vi.fn().mockReturnValue({ attrs: { align: "center" } }) },
      start: 1,
      pos: 0,
      depth: 1,
    } as unknown as ReturnType<typeof findTable>);

    const { editor, mockSetNodeMarkup, mockDispatch } = makeEditorWithView();
    renderToolbar(editor);

    fireEvent.click(screen.getByTitle(t("alignColumnCenter")));

    // No cells changed — neither setNodeMarkup nor dispatch should be called,
    // avoiding a no-op history entry in the editor.
    expect(mockSetNodeMarkup).not.toHaveBeenCalled();
    expect(mockDispatch).not.toHaveBeenCalled();
  });

  it("does not dispatch when isInTable returns false", () => {
    vi.mocked(isInTable).mockReturnValue(false);
    const { editor, mockDispatch } = makeEditorWithView();
    renderToolbar(editor);

    fireEvent.click(screen.getByTitle(t("alignColumnLeft")));

    expect(mockDispatch).not.toHaveBeenCalled();
  });

  it("does not dispatch when there is no cell at the cursor position", () => {
    vi.mocked(cellAround).mockReturnValue(null);
    const { editor, mockDispatch } = makeEditorWithView();
    renderToolbar(editor);

    fireEvent.click(screen.getByTitle(t("alignColumnCenter")));

    expect(mockDispatch).not.toHaveBeenCalled();
  });
});
