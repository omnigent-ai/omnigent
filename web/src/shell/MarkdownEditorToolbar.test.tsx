// Tests for the auto-save status pill in MarkdownEditorToolbar.
//
// The pill replaces the old explicit Save button. It reflects the live
// persistence state and stays clickable only when there's an actionable
// write (retry a failed save, or flush unsaved edits). ⌘S always flushes.
//
// @tiptap/react is mocked so the toolbar renders without a real editor;
// only useEditorState (formatting badges) and the editor.getMarkdown()
// call on save are exercised.

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@tiptap/react", () => ({
  // Return every badge flag false; the toolbar's formatting buttons are
  // irrelevant to these status tests.
  useEditorState: () => ({
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
  }),
}));
// Side-effect import in the component; nothing needed at runtime.
vi.mock("@tiptap/markdown", () => ({}));

import { ToolbarPlugin } from "./MarkdownEditorToolbar";
import type { Editor } from "@tiptap/react";

const MARKDOWN = "# saved doc";
const editorStub = { getMarkdown: () => MARKDOWN } as unknown as Editor;

function renderToolbar(
  overrides: Partial<{
    editor: Editor;
    onSave: (md: string) => void;
    isSaving: boolean;
    isDirty: boolean;
    saveError: boolean;
    saveDisabled: boolean;
    hasExternalUpdate: boolean;
  }> = {},
) {
  const onSave = overrides.onSave ?? vi.fn();
  render(
    <ToolbarPlugin
      editor={overrides.editor ?? editorStub}
      onSave={onSave}
      isSaving={overrides.isSaving ?? false}
      isDirty={overrides.isDirty ?? false}
      saveError={overrides.saveError ?? false}
      saveDisabled={overrides.saveDisabled ?? false}
      hasExternalUpdate={overrides.hasExternalUpdate ?? false}
    />,
  );
  return { onSave };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  cleanup();
});

// ── Responsive overflow ─────────────────────────────────────────────────────────────────
// jsdom has no layout: getBoundingClientRect is stubbed so the row reports the
// given width, each clone item 28px (divider 9px) and the status pill 70px.

function installToolbarWidths(rowWidth: number | (() => number)): {
  observers: ResizeObserverCallback[];
} {
  const observers: ResizeObserverCallback[] = [];
  class StubResizeObserver {
    constructor(callback: ResizeObserverCallback) {
      observers.push(callback);
    }
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", StubResizeObserver);
  const currentRowWidth = typeof rowWidth === "function" ? rowWidth : () => rowWidth;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
    this: HTMLElement,
  ) {
    let width = 0;
    if (this.getAttribute("role") === "toolbar") width = currentRowWidth();
    else if (this.dataset.measure === "divider") width = 9;
    else if (this.dataset.measure !== undefined) width = 28;
    else if (this.dataset.slot === "save-status") width = 70;
    return {
      width,
      height: 0,
      top: 0,
      left: 0,
      right: width,
      bottom: 0,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect;
  });
  return { observers };
}

/** A chainable editor stub that records every command name it receives. */
function chainRecordingEditor(): { editor: Editor; calls: string[] } {
  const calls: string[] = [];
  const chain: Record<string, (...args: unknown[]) => unknown> = new Proxy(
    {},
    {
      get:
        (_target, prop: string) =>
        (..._args: unknown[]) => {
          calls.push(prop);
          return prop === "run" ? true : chain;
        },
    },
  );
  const editor = {
    getMarkdown: () => MARKDOWN,
    chain: () => chain,
    commands: {
      focus: () => {
        calls.push("commands.focus");
        return true;
      },
    },
    isFocused: false,
  } as unknown as Editor;
  return { editor, calls };
}

const INLINE_MARKS = ["Bold (⌘B)", "Italic (⌘I)", "Strikethrough", "Inline code"];
const LOW_PRIORITY = ["Undo (⌘Z)", "Heading 1", "Quote", "Bullet list", "Insert table", "Copy"];

describe("MarkdownEditorToolbar overflow", () => {
  it("keeps every button inline when the row is wide enough", () => {
    installToolbarWidths(1000);
    renderToolbar();
    const row = within(screen.getByRole("toolbar", { name: "Formatting" }));
    for (const name of [...INLINE_MARKS, ...LOW_PRIORITY, "All changes saved"]) {
      expect(row.getByRole("button", { name })).toBeInTheDocument();
    }
    expect(row.queryByRole("button", { name: "More formatting" })).toBeNull();
  });

  it("folds low-priority tools into a ⋯ menu and keeps the inline marks and status pill", () => {
    // Four marks (112) + ⋯ (28) + status (70) + 2px slack fit exactly; undo
    // and its divider would not, so everything below the marks folds.
    installToolbarWidths(212);
    renderToolbar();
    const row = within(screen.getByRole("toolbar", { name: "Formatting" }));
    for (const name of [...INLINE_MARKS, "More formatting", "All changes saved"]) {
      expect(row.getByRole("button", { name })).toBeInTheDocument();
    }
    for (const name of LOW_PRIORITY) {
      expect(row.queryByRole("button", { name })).toBeNull();
    }
  });

  it("keeps Bold inline longest and still shows the status pill in a very narrow row", () => {
    // Bold (28) + ⋯ (28) + status (70) = 126; a second mark would overflow.
    installToolbarWidths(130);
    renderToolbar();
    const row = within(screen.getByRole("toolbar", { name: "Formatting" }));
    expect(row.getByRole("button", { name: "Bold (⌘B)" })).toBeInTheDocument();
    expect(row.getByRole("button", { name: "All changes saved" })).toBeInTheDocument();
    expect(row.queryByRole("button", { name: "Italic (⌘I)" })).toBeNull();
  });

  it("runs a folded command from the ⋯ menu against the editor", () => {
    installToolbarWidths(210);
    const { editor, calls } = chainRecordingEditor();
    renderToolbar({ editor });
    fireEvent.click(screen.getByRole("button", { name: "More formatting" }));
    const folded = screen.getByRole("button", { name: "Bullet list" });
    expect(screen.getByRole("toolbar", { name: "Formatting" })).not.toContainElement(folded);
    fireEvent.click(folded);
    // The menu closes once the command has run and hands focus back to the editor.
    expect(calls).toEqual(["focus", "toggleBulletList", "run", "commands.focus"]);
    expect(screen.queryByRole("button", { name: "Bullet list" })).toBeNull();
  });

  it("refolds as the measured row width changes after mount", () => {
    let rowWidth = 1000;
    const { observers } = installToolbarWidths(() => rowWidth);
    renderToolbar();
    const row = () => within(screen.getByRole("toolbar", { name: "Formatting" }));
    expect(row().getByRole("button", { name: "Undo (⌘Z)" })).toBeInTheDocument();
    expect(row().queryByRole("button", { name: "More formatting" })).toBeNull();

    rowWidth = 212;
    act(() => observers.forEach((cb) => cb([], {} as ResizeObserver)));
    expect(row().queryByRole("button", { name: "Undo (⌘Z)" })).toBeNull();
    expect(row().getByRole("button", { name: "More formatting" })).toBeInTheDocument();

    rowWidth = 1000;
    act(() => observers.forEach((cb) => cb([], {} as ResizeObserver)));
    expect(row().getByRole("button", { name: "Undo (⌘Z)" })).toBeInTheDocument();
    expect(row().queryByRole("button", { name: "More formatting" })).toBeNull();
  });

  it("closes the ⋯ menu after a folded Copy and hands focus back to the editor", () => {
    installToolbarWidths(212);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    try {
      const { editor, calls } = chainRecordingEditor();
      renderToolbar({ editor });
      fireEvent.click(screen.getByRole("button", { name: "More formatting" }));
      fireEvent.click(screen.getByRole("button", { name: "Copy" }));
      expect(writeText).toHaveBeenCalledWith(MARKDOWN);
      // Copy has no editor chain of its own; closing the menu refocuses the editor.
      expect(calls).toEqual(["commands.focus"]);
      expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
      expect(screen.getByRole("button", { name: "More formatting" })).toBeInTheDocument();
    } finally {
      Reflect.deleteProperty(navigator, "clipboard");
    }
  });
});

describe("MarkdownEditorToolbar auto-save status", () => {
  it("shows 'Saved' when clean and does not trigger a save on click", () => {
    const { onSave } = renderToolbar({ isDirty: false });
    const btn = screen.getByText("Saved");
    fireEvent.click(btn);
    // Clean state is informational only — clicking must not write.
    expect(onSave).not.toHaveBeenCalled();
  });

  it("shows 'Unsaved' while dirty (debounce pending, no write yet) and flushes on click", () => {
    // isDirty true, isSaving false → debounce window, no network I/O yet, so
    // the pill reads "Unsaved", not "Saving…". Clicking is a manual flush.
    const { onSave } = renderToolbar({ isDirty: true, isSaving: false });
    expect(screen.queryByText("Saving…")).toBeNull();
    fireEvent.click(screen.getByText("Unsaved"));
    expect(onSave).toHaveBeenCalledWith(MARKDOWN);
  });

  it("shows 'Saving…' only once a write is in flight", () => {
    renderToolbar({ isSaving: true, isDirty: true });
    expect(screen.getByText("Saving…")).toBeInTheDocument();
    expect(screen.queryByText("Unsaved")).toBeNull();
  });

  it("shows 'Retry' on error and re-attempts the save on click", () => {
    // A failed save leaves the editor dirty, so retry is actionable.
    const { onSave } = renderToolbar({ saveError: true, isDirty: true });
    fireEvent.click(screen.getByText("Retry"));
    expect(onSave).toHaveBeenCalledWith(MARKDOWN);
  });

  it("does not show a clickable 'Retry' for a stale error with nothing to save", () => {
    // saveError but !isDirty (e.g. after Load latest cleared dirty): there is
    // nothing to retry, so the pill reads "Saved" and clicking is a no-op.
    const { onSave } = renderToolbar({ saveError: true, isDirty: false });
    expect(screen.queryByText("Retry")).toBeNull();
    expect(screen.getByText("Saved")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Saved"));
    expect(onSave).not.toHaveBeenCalled();
  });

  it("shows 'Offline' when the runner is down and does not write on click", () => {
    const { onSave } = renderToolbar({ saveDisabled: true, isDirty: true });
    fireEvent.click(screen.getByText("Offline"));
    // Offline can't persist; the pill is disabled so the click is a no-op.
    expect(onSave).not.toHaveBeenCalled();
  });

  it("shows 'Offline' (not a clickable 'Retry') when a save errored and then went offline", () => {
    // saveError + saveDisabled: offline takes precedence so we don't surface a
    // "Retry" that would silently no-op (handleSave bails while offline).
    const { onSave } = renderToolbar({ saveError: true, saveDisabled: true, isDirty: true });
    expect(screen.getByText("Offline")).toBeInTheDocument();
    expect(screen.queryByText("Retry")).toBeNull();
    fireEvent.click(screen.getByText("Offline"));
    expect(onSave).not.toHaveBeenCalled();
  });

  it("does not save (pill click or ⌘S) while an external-edit conflict is unresolved", () => {
    // hasExternalUpdate=true: the user must resolve via Keep mine / Load latest
    // first, so the pill is non-clickable and ⌘S is a no-op (no clobbering).
    const { onSave } = renderToolbar({ isDirty: true, hasExternalUpdate: true });
    fireEvent.click(screen.getByText("Unsaved"));
    fireEvent.keyDown(window, { key: "s", metaKey: true });
    expect(onSave).not.toHaveBeenCalled();
  });

  it("flushes on ⌘S when there are unsaved edits", () => {
    const { onSave } = renderToolbar({ isDirty: true });
    fireEvent.keyDown(window, { key: "s", metaKey: true });
    expect(onSave).toHaveBeenCalledWith(MARKDOWN);
  });

  it("does not flush on ⌘S when offline", () => {
    const { onSave } = renderToolbar({ isDirty: true, saveDisabled: true });
    fireEvent.keyDown(window, { key: "s", metaKey: true });
    // handleSave short-circuits when saveDisabled — no write attempted.
    expect(onSave).not.toHaveBeenCalled();
  });
});
