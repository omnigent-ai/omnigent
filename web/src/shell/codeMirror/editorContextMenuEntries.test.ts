import { describe, expect, it } from "vitest";
import {
  type EditorMenuContext,
  type EditorMenuEntry,
  editorMenuEntries,
  PASTE_WITH_KEYS_DESCRIPTION,
} from "./editorContextMenuEntries";

const READ_ONLY: EditorMenuContext = {
  editable: false,
  hasSelection: false,
  canSearchWorkspace: false,
  canComment: false,
  canAttachToAgent: false,
  canReadClipboard: true,
  mac: true,
};

function rows(entries: EditorMenuEntry[]): string[] {
  return entries.map((entry) => (entry.kind === "separator" ? "---" : entry.label));
}

function item(entries: EditorMenuEntry[], action: string) {
  const found = entries.find((entry) => entry.kind === "item" && entry.action === action);
  if (!found || found.kind !== "item") throw new Error(`no ${action} row`);
  return found;
}

describe("editorMenuEntries", () => {
  it("offers reading commands only in a read-only file", () => {
    expect(rows(editorMenuEntries(READ_ONLY))).toEqual([
      "Copy",
      "---",
      "Find in file",
      "Go to line…",
      "---",
      "Select all occurrences",
      "Fold all",
      "Unfold all",
    ]);
    expect(item(editorMenuEntries(READ_ONLY), "copy").enabled).toBe(false);
  });

  it("adds the editing commands, with their shortcuts, in an editable file", () => {
    const entries = editorMenuEntries({ ...READ_ONLY, editable: true, hasSelection: true });
    expect(rows(entries)).toEqual([
      "Cut",
      "Copy",
      "Paste",
      "---",
      "Find in file",
      "Find and replace in file",
      "Go to line…",
      "---",
      "Select all occurrences",
      "Toggle line comment",
      "Fold all",
      "Unfold all",
    ]);
    expect(item(entries, "cut")).toMatchObject({ enabled: true, keys: ["⌘", "X"] });
    expect(item(entries, "file.toggleLineComment").shortcut).toBe("file.toggleLineComment");
    expect(item(entries, "find-and-replace").shortcut).toBe("file.findAndReplace");
  });

  it("leads with Search Text in Workspace and its shortcut for a selection", () => {
    const entries = editorMenuEntries({
      ...READ_ONLY,
      hasSelection: true,
      canSearchWorkspace: true,
    });
    expect(entries[0]).toMatchObject({
      action: "search-workspace",
      label: "Search Text in Workspace…",
      shortcut: "palette.text",
    });
    expect(entries[1]).toEqual({ kind: "separator", key: "separator-1" });
  });

  it("offers the comment actions for a selection that can take them", () => {
    const context = { ...READ_ONLY, hasSelection: true, canComment: true };
    expect(rows(editorMenuEntries(context)).slice(0, 2)).toEqual(["Add comment", "---"]);
    expect(rows(editorMenuEntries({ ...context, canAttachToAgent: true })).slice(0, 2)).toEqual([
      "Add comment",
      "Attach to agent",
    ]);
    expect(rows(editorMenuEntries({ ...context, hasSelection: false }))[0]).toBe("Copy");
  });

  it("points Paste to the keyboard where the page can't read the clipboard", () => {
    const paste = item(
      editorMenuEntries({ ...READ_ONLY, editable: true, canReadClipboard: false }),
      "paste",
    );
    expect(paste).toMatchObject({ enabled: false, description: PASTE_WITH_KEYS_DESCRIPTION });
  });

  it("shows Ctrl for the clipboard keys off macOS", () => {
    expect(item(editorMenuEntries({ ...READ_ONLY, mac: false }), "copy").keys).toEqual([
      "Ctrl",
      "C",
    ]);
  });
});
