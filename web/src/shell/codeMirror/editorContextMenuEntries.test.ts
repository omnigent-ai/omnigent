import { EditorSelection, EditorState } from "@codemirror/state";
import { describe, expect, it, vi } from "vitest";
import {
  type EditorMenuContext,
  type EditorMenuEntry,
  editorMenuEntries,
  pasteWithKeysDescription,
  selectionSearchQuery,
} from "./editorContextMenuEntries";

const READ_ONLY: EditorMenuContext = {
  editable: false,
  hasSelection: false,
  hasSelectedText: false,
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
    const entries = editorMenuEntries({
      ...READ_ONLY,
      editable: true,
      hasSelection: true,
      hasSelectedText: true,
    });
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

  it("offers Cut and Copy for text in any range, though the main range is a cursor", () => {
    const context = { ...READ_ONLY, editable: true, canComment: true, hasSelectedText: true };
    const entries = editorMenuEntries(context);
    expect(item(entries, "cut").enabled).toBe(true);
    expect(item(entries, "copy").enabled).toBe(true);
    // A comment is on the main range, so it still needs text there.
    expect(rows(entries)).not.toContain("Add comment");
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
    expect(paste).toMatchObject({ enabled: false, description: "Press ⌘V to paste here." });
  });

  it("names Ctrl in the clipboard keys and the Paste hint off macOS", () => {
    const paste = item(
      editorMenuEntries({ ...READ_ONLY, editable: true, canReadClipboard: false, mac: false }),
      "paste",
    );
    expect(paste).toMatchObject({
      keys: ["Ctrl", "V"],
      description: "Press Ctrl+V to paste here.",
    });
    expect(pasteWithKeysDescription(false)).toBe("Press Ctrl+V to paste here.");
    expect(item(editorMenuEntries({ ...READ_ONLY, mac: false }), "copy").keys).toEqual([
      "Ctrl",
      "C",
    ]);
  });
});

describe("selectionSearchQuery", () => {
  const selected = (doc: string, from: number, to: number) =>
    EditorState.create({ doc, selection: EditorSelection.single(from, to) });

  it("takes the selection's first non-blank line, trimmed and clipped to the selection", () => {
    const doc = "alpha beta\n   \n  needle here  \nsecond";
    expect(selectionSearchQuery(selected(doc, 6, doc.length))).toBe("beta");
    expect(selectionSearchQuery(selected(doc, 10, doc.length))).toBe("needle here");
    expect(selectionSearchQuery(selected(doc, 13, 23))).toBe("needle");
  });

  it("makes no query from an empty or blank selection", () => {
    expect(selectionSearchQuery(selected("alpha", 2, 2))).toBeNull();
    expect(selectionSearchQuery(selected("a\n  \n\t\nb", 1, 7))).toBeNull();
  });

  it("reads only up to the first non-blank line of a select-all", () => {
    const state = selected(
      Array.from({ length: 5000 }, (_, index) => `line ${index}`).join("\n"),
      0,
      0,
    );
    const all = state.update({ selection: EditorSelection.single(0, state.doc.length) }).state;
    const sliceDoc = vi.spyOn(all, "sliceDoc");
    const line = vi.spyOn(all.doc, "line");
    expect(selectionSearchQuery(all)).toBe("line 0");
    expect(sliceDoc).not.toHaveBeenCalled();
    expect(line).toHaveBeenCalledTimes(1);
  });
});
