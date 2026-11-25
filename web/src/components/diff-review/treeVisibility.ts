// Whether the diff review shows its changed-files tree, remembered per device.

import { readSidebarVisible, writeSidebarVisible } from "@/lib/sidebarVisibility";

const TREE_VISIBLE_STORAGE_KEY = "omnigent:diff-review-tree-visible";

export function readDiffReviewTreeVisible(fallback: boolean): boolean {
  return readSidebarVisible(TREE_VISIBLE_STORAGE_KEY, fallback);
}

export function writeDiffReviewTreeVisible(visible: boolean): void {
  writeSidebarVisible(TREE_VISIBLE_STORAGE_KEY, visible);
}
