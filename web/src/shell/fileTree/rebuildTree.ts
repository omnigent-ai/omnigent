// Rebuilds a file tree model from a fresh set of paths while keeping the reader's
// place: their open folders, selection, focus and the row at the top of the view.
// A re-sort and a folder that adds hundreds of entries at once both rebuild this way.

import type { FileTreeModel } from "@/components/file-tree/vendored";

/** The first row actually on screen in `host`, skipping overscan. */
export function topVisibleRow(host: HTMLElement): string | null {
  const box = host.getBoundingClientRect();
  for (const row of host.querySelectorAll<HTMLElement>('[role="treeitem"][data-item-path]')) {
    const rect = row.getBoundingClientRect();
    if (rect.height > 0 && rect.bottom > box.top + 1) return row.getAttribute("data-item-path");
  }
  return null;
}

/**
 * Makes `model` hold exactly `paths`, opening `expandedPaths`, then restores the
 * selection, focus (when the tree in `host` had it) and the top visible row.
 */
export function rebuildTreeInPlace(
  model: FileTreeModel,
  paths: Iterable<string>,
  { host, expandedPaths }: { host: HTMLElement | null; expandedPaths: Iterable<string> },
): void {
  const anchor = host ? topVisibleRow(host) : null;
  const selected = model.getSelectedPaths();
  const focused = model.getFocusedPath();
  const hadFocus = host !== null && host.contains(document.activeElement);
  model.resetPaths([...paths], { initialExpandedPaths: [...expandedPaths] });
  for (const path of selected) model.getItem(path)?.select();
  if (focused && hadFocus) model.focusPath(focused);
  if (anchor) model.scrollToPath(anchor, { offset: "top", focus: false });
}
