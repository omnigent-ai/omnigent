// Tests for the auto-save status pill in MarkdownEditorToolbar.
//
// The pill replaces the old explicit Save button. It reflects the live
// persistence state and stays clickable only when there's an actionable
// write (retry a failed save, or flush unsaved edits). ⌘S always flushes.
//
// @tiptap/react is mocked so the toolbar renders without a real editor;
// only useEditorState (formatting badges) and the editor.getMarkdown()
// call on save are exercised.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
      editor={editorStub}
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
  vi.unstubAllGlobals();
  cleanup();
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
