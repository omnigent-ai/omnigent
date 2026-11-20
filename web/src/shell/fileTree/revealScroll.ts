// Brings a revealed row into the Files tree's view: a row already on screen stays
// where it is, and one out of view, or not drawn yet, is scrolled to the middle.

import type { FileTreeModel } from "@/components/file-tree/vendored";

/** Where a row or the tree's viewport sits on screen, top to bottom. */
interface VerticalBox {
  top: number;
  bottom: number;
}

/** The row that draws `path` in the tree, if it's drawn. */
function drawnRow(host: HTMLElement, path: string): HTMLElement | null {
  return host.querySelector<HTMLElement>(`[role="treeitem"][data-item-path="${CSS.escape(path)}"]`);
}

/** The part of the tree rows show through. */
function treeViewport(host: HTMLElement): VerticalBox {
  const box = host.getBoundingClientRect();
  return { top: box.top, bottom: box.bottom };
}

/** Whether `path`'s row is drawn and wholly inside the tree's view. */
export function rowInView(host: HTMLElement, path: string): boolean {
  const row = drawnRow(host, path);
  if (!row) return false;
  const rect = row.getBoundingClientRect();
  if (rect.height === 0) return false;
  const view = treeViewport(host);
  // A pixel of slack absorbs fractional row positions under page zoom.
  return rect.top >= view.top - 1 && rect.bottom <= view.bottom + 1;
}

/** Scrolls `path`'s row to the middle of the tree, unless it's already in view. */
export function scrollRowIntoView(
  model: Pick<FileTreeModel, "scrollToPath">,
  host: HTMLElement | null,
  path: string,
): void {
  if (host && rowInView(host, path)) return;
  model.scrollToPath(path, { offset: "center", focus: false });
}
