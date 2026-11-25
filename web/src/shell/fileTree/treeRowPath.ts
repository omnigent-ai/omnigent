// The file tree row an event happened in, found along the event's path, so an event
// on a row's icon or name still names the row.

/** The row path a pointer, key or focus event happened in, or null outside a row. */
export function rowPathOf(event: Event): string | null {
  for (const node of event.composedPath()) {
    if (!(node instanceof HTMLElement)) continue;
    if (node.tagName === "BUTTON" && !node.hasAttribute("data-item-path")) return null;
    const path = node.getAttribute("data-item-path");
    if (path && node.getAttribute("role") === "treeitem") return path;
  }
  return null;
}
