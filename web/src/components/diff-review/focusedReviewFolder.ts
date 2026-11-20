// The changed-files tree folder keyboard focus is on, so ⌘K offers the same actions as its
// ⋯ menu. Focus moving into the palette or a menu keeps it; focus moving anywhere else lets it go.

import { createFocusedTarget } from "@/shell/focusedTarget";
import type { DiffReviewFileAction } from "./diffReviewFileAction";

/** A focused folder row: the review it belongs to, its path, and its menu's actions. */
export interface FocusedReviewFolder {
  /** The review whose tree has the folder; its tree may be portalled out of the review. */
  owner: string;
  path: string;
  actions: readonly DiffReviewFileAction[];
}

const focusedFolder = createFocusedTarget<FocusedReviewFolder>();

/** Make `folder`, focused in the tree `host`, ⌘K's folder while focus stays in that tree. */
export const publishFocusedReviewFolder = focusedFolder.publish;

/** Let the tree `host` go as ⌘K's folder, once focus is on a file row or the tree unmounts. */
export const releaseFocusedReviewFolder = focusedFolder.release;

/** The tree whose folder ⌘K has now, if any. */
export function focusedReviewFolderTree(): HTMLElement | null {
  return focusedFolder.useStore.getState().element;
}

/** The focused folder and the tree that has it, while that tree is still on the page. */
export function useFocusedReviewFolder(): {
  folder: FocusedReviewFolder;
  tree: HTMLElement;
} | null {
  const folder = focusedFolder.useFocused();
  const tree = focusedFolder.useStore((state) => state.element);
  return folder && tree ? { folder, tree } : null;
}
