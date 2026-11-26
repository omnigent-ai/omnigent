// Rebuilds a file tree model from a fresh set of paths while keeping the reader's
// place: their open folders, selection, focus and the row at the top of the view.
// A re-sort and a folder that adds hundreds of entries at once both rebuild this way.

import type { FileTreeModel } from "@/components/file-tree/vendored";

/** Whether keyboard focus is on the tree in `host` or one of its rows. */
export const hasFocusWithin = (host: HTMLElement | null): boolean =>
  host !== null && host.contains(document.activeElement);

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
 * Makes `model` hold exactly `paths`, keeping every folder it has open, even one a
 * folder chain shows under its deepest folder's row, then restores the selection,
 * focus (when the tree in `host` had it) and the top visible row.
 */
export function rebuildTreeInPlace(
  model: FileTreeModel,
  paths: Iterable<string>,
  { host }: { host: HTMLElement | null },
): void {
  const anchor = host ? topVisibleRow(host) : null;
  const selected = model.getSelectedPaths();
  const focused = model.getFocusedPath();
  const hadFocus = hasFocusWithin(host);
  model.resetPaths([...paths]);
  for (const path of selected) model.getItem(path)?.select();
  if (focused && hadFocus) model.focusPath(focused);
  if (anchor) model.scrollToPath(anchor, { offset: "top", focus: false });
}
