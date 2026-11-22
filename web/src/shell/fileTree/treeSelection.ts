// One selected row at a time in a file tree, which on its own selects
// ranges and toggles rows with modified clicks and keys. The panel opens the row
// a person activates, never one that stayed selected while another was dropped.

import { isMacPlatform } from "@/lib/hotkeys";

/** What the policy needs from the tree: `FileTreeModel` satisfies it. */
export interface SelectableTree {
  getSelectedPaths: () => readonly string[];
  getFocusedPath: () => string | null;
  getItem: (path: string) => { select: () => void; deselect: () => void } | null;
  focusPath: (path: string) => void;
  focusNextItem: () => void;
  focusPreviousItem: () => void;
}

/** How the tree host handles one piece of row input before the tree sees it. */
export type RowInputAction =
  | { kind: "pass" }
  | { kind: "block" }
  | { kind: "move"; direction: "next" | "previous" }
  | { kind: "select"; path: string }
  | { kind: "open-in-new-tab"; path: string };

export interface RowKeyInput {
  key: string;
  shiftKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
}

/**
 * A key on a focused row. Shift+Up/Down would extend a range, so they move like
 * plain arrows; select-all and toggle-select (Ctrl/Meta+A, Ctrl/Meta+Space) do nothing.
 */
export function rowKeyAction(input: RowKeyInput): RowInputAction {
  const command = input.ctrlKey || input.metaKey;
  if (input.shiftKey && !command && !input.altKey) {
    if (input.key === "ArrowDown") return { kind: "move", direction: "next" };
    if (input.key === "ArrowUp") return { kind: "move", direction: "previous" };
  }
  if (command && (input.key === "a" || input.key === "A" || input.key === " ")) {
    return { kind: "block" };
  }
  return { kind: "pass" };
}

/**
 * A click on a row. ⌘-click (Ctrl-click off macOS) on a file opens it in a new tab;
 * any other modified click, which would range-select or toggle, selects that row alone.
 */
export function rowClickAction(
  input: { shiftKey: boolean; ctrlKey: boolean; metaKey: boolean },
  path: string | null,
  mac: boolean = isMacPlatform(),
): RowInputAction {
  if (!path) return { kind: "pass" };
  const command = mac ? input.metaKey : input.ctrlKey;
  if (command && !input.shiftKey && !path.endsWith("/")) return { kind: "open-in-new-tab", path };
  if (input.shiftKey || input.ctrlKey || input.metaKey) return { kind: "select", path };
  return { kind: "pass" };
}

/**
 * Keeps a tree to one selected row. Which file opens is decided by the input
 * that chose it, never by a selection change, so trimming a selection,
 * revealing a row or restoring one after a re-sort opens nothing.
 */
export class SingleSelection {
  private readonly tree: SelectableTree;
  private applying = 0;

  constructor(tree: SelectableTree) {
    this.tree = tree;
  }

  /** Makes `path` the focused and only selected row. */
  selectOnly(path: string): void {
    this.applying++;
    try {
      this.tree.focusPath(path);
      this.keepOnly(path);
    } finally {
      this.applying--;
    }
  }

  /** Makes `path` the only selected row, leaving focus where it is. */
  selectWithoutFocus(path: string): void {
    this.applying++;
    try {
      this.keepOnly(path);
    } finally {
      this.applying--;
    }
  }

  private keepOnly(path: string): void {
    for (const selected of this.tree.getSelectedPaths()) {
      if (selected !== path) this.tree.getItem(selected)?.deselect();
    }
    if (!this.tree.getSelectedPaths().includes(path)) this.tree.getItem(path)?.select();
  }

  /**
   * Runs one row input action. `consumed` means the tree must not also handle the
   * event; `activate` is the row a person chose, to open once the update is done,
   * in a new tab when `newTab` is set.
   */
  apply(action: RowInputAction): { consumed: boolean; activate: string | null; newTab: boolean } {
    switch (action.kind) {
      case "pass":
        return { consumed: false, activate: null, newTab: false };
      case "block":
        return { consumed: true, activate: null, newTab: false };
      case "move":
        if (action.direction === "next") this.tree.focusNextItem();
        else this.tree.focusPreviousItem();
        return { consumed: true, activate: null, newTab: false };
      case "select":
        this.selectOnly(action.path);
        return { consumed: true, activate: action.path, newTab: false };
      case "open-in-new-tab":
        this.selectOnly(action.path);
        return { consumed: true, activate: action.path, newTab: true };
    }
  }

  /** After the tree reports a selection change: trims anything beyond one row to the focused row. */
  normalize(): void {
    if (this.applying > 0) return;
    const selected = this.tree.getSelectedPaths();
    if (selected.length <= 1) return;
    const focused = this.tree.getFocusedPath();
    this.selectOnly(focused && selected.includes(focused) ? focused : selected.at(-1)!);
  }
}

/** Whether a key opens the focused row, as Enter and Space do on a list of files. */
export function opensRow(input: RowKeyInput): boolean {
  if (input.shiftKey || input.ctrlKey || input.metaKey || input.altKey) return false;
  return input.key === "Enter" || input.key === " ";
}
