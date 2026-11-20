// Reads and addresses text in the diff's rendered rows. Each file renders in
// a shadow root; a row is `[data-line]` with its line's visible text. The
// current ("after") side is addition and context rows, outside the deletions
// column in split layout.

import type { LinePosition } from "./textAnchors";

/** Whether `row` shows a line of the current version. */
export function isCurrentSideRow(row: Element): boolean {
  const type = row.getAttribute("data-line-type") ?? "";
  if (type === "change-addition") return true;
  return type.startsWith("context") && row.closest("[data-deletions]") === null;
}

/** The rendered row showing line `line` of the current version, if rendered. */
export function currentSideRow(root: ParentNode, line: number): HTMLElement | undefined {
  return [...root.querySelectorAll<HTMLElement>(`[data-line="${line}"]`)].find(
    (row) => row.closest("[data-content]") !== null && isCurrentSideRow(row),
  );
}

/** The rendered row showing line `line` of one side's version, if rendered. */
export function sideRow(
  root: ParentNode,
  line: number,
  side: "additions" | "deletions",
): HTMLElement | undefined {
  if (side === "additions") return currentSideRow(root, line);
  // An unchanged row in the unified layout carries its old line number as data-alt-line.
  const rows = root.querySelectorAll<HTMLElement>(
    `[data-content] [data-line="${line}"], [data-content] [data-alt-line="${line}"]`,
  );
  return [...rows].find((row) => {
    const type = row.getAttribute("data-line-type") ?? "";
    if (type === "change-deletion") return row.getAttribute("data-line") === String(line);
    if (!type.startsWith("context")) return false;
    return row.closest("[data-deletions]") !== null
      ? row.getAttribute("data-line") === String(line)
      : row.getAttribute("data-alt-line") === String(line);
  });
}

/** The current-version line and column at a DOM point, or undefined off that side. */
export function currentSidePosition(node: Node, offset: number): LinePosition | undefined {
  const element = node instanceof Element ? node : node.parentElement;
  const row = element?.closest("[data-line]");
  if (!row || row.closest("[data-content]") === null || !isCurrentSideRow(row)) return undefined;
  const line = Number(row.getAttribute("data-line"));
  if (!Number.isSafeInteger(line) || line < 1) return undefined;
  const before = row.ownerDocument.createRange();
  before.setStart(row, 0);
  before.setEnd(node, offset);
  return { line, column: before.toString().length };
}

function textPosition(nodes: Text[], offset: number): { node: Text; offset: number } | undefined {
  let remaining = offset;
  for (const node of nodes) {
    if (remaining <= node.length) return { node, offset: remaining };
    remaining -= node.length;
  }
  return undefined;
}

/**
 * Where `node` at `offset` is in `element`'s text, counted the way `textRange` counts: the
 * lengths of `element`'s text nodes before it, plus `offset`. Undefined when `node` isn't one of
 * `element`'s text nodes.
 */
export function textOffsetWithin(element: Element, node: Node, offset: number): number | undefined {
  if (!(node instanceof Text) || !element.contains(node) || offset < 0 || offset > node.length) {
    return undefined;
  }
  const walker = element.ownerDocument.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  let before = 0;
  for (let current = walker.nextNode(); current; current = walker.nextNode()) {
    if (current === node) return before + offset;
    if (current instanceof Text) before += current.length;
  }
  return undefined;
}

/** The DOM range of columns `[from, to)` in `element`'s text. */
export function textRange(element: Element, from: number, to: number): Range | undefined {
  if (from < 0 || to <= from) return undefined;
  const walker = element.ownerDocument.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  let length = 0;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!(node instanceof Text)) continue;
    nodes.push(node);
    length += node.length;
  }
  if (to > length) return undefined;
  const start = textPosition(nodes, from);
  const end = textPosition(nodes, to);
  if (!start || !end) return undefined;
  const range = element.ownerDocument.createRange();
  range.setStart(start.node, start.offset);
  range.setEnd(end.node, end.offset);
  return range;
}

/** The text selection in one rendered file, as current-version positions. */
export interface RenderedSelection {
  itemId: string;
  start: LinePosition;
  end: LinePosition;
  collapsed: boolean;
  /** Where the selection starts on screen, for placing actions beside it. */
  anchorRect: DOMRect | undefined;
}

/**
 * The document's selection when it lies within one rendered file and both
 * ends are on the current version's side. `roots` maps item ids to the
 * shadow roots their rows render in.
 */
export function readRenderedSelection(
  roots: ReadonlyMap<string, ShadowRoot>,
): RenderedSelection | undefined {
  const selection = document.getSelection();
  if (!selection || selection.rangeCount === 0) return undefined;
  const range: AbstractRange | undefined =
    typeof selection.getComposedRanges === "function"
      ? selection.getComposedRanges({ shadowRoots: [...roots.values()] })[0]
      : selection.getRangeAt(0);
  if (!range) return undefined;
  for (const [itemId, root] of roots) {
    if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) continue;
    const start = currentSidePosition(range.startContainer, range.startOffset);
    const end = currentSidePosition(range.endContainer, range.endOffset);
    if (!start || !end) return undefined;
    const startRange = document.createRange();
    startRange.setStart(range.startContainer, range.startOffset);
    return {
      itemId,
      start,
      end,
      collapsed: range.collapsed,
      anchorRect: startRange.getClientRects?.()[0] ?? startRange.getBoundingClientRect?.(),
    };
  }
  return undefined;
}

/** The topmost current-version row still showing below `viewportTop`, if rendered. */
export function topCurrentSideRow(
  root: ParentNode,
  viewportTop: number,
): { line: number; top: number } | undefined {
  for (const row of root.querySelectorAll<HTMLElement>("[data-content] [data-line]")) {
    if (!isCurrentSideRow(row)) continue;
    const bounds = row.getBoundingClientRect();
    if (bounds.bottom <= viewportTop) continue;
    const line = Number(row.getAttribute("data-line"));
    if (Number.isSafeInteger(line) && line > 0) return { line, top: bounds.top };
  }
  return undefined;
}

function scrollsHorizontally(element: Element): boolean {
  const { overflowX } = getComputedStyle(element);
  return (
    (overflowX === "auto" || overflowX === "scroll") && element.scrollWidth > element.clientWidth
  );
}

/** The nearest ancestor of `element` that scrolls sideways, across shadow roots. */
function horizontalScroller(element: Element): Element | undefined {
  let current: Element | null = element.parentElement;
  while (current) {
    if (scrollsHorizontally(current)) return current;
    const parent: Element | null =
      current.parentElement ??
      (current.parentNode instanceof ShadowRoot ? current.parentNode.host : null);
    current = parent;
  }
  return undefined;
}

/** Scrolls `row` sideways so 0-based `column` is in view, centering it when it was off screen. */
export function revealColumn(row: Element, column: number): void {
  const length = row.textContent?.length ?? 0;
  if (length === 0) return;
  const from = Math.max(0, Math.min(column, length - 1));
  const bounds = textRange(row, from, from + 1)?.getBoundingClientRect();
  const scroller = bounds && horizontalScroller(row);
  if (!bounds || !scroller) return;
  const viewport = scroller.getBoundingClientRect();
  if (bounds.left >= viewport.left && bounds.right <= viewport.right) return;
  scroller.scrollLeft += bounds.left - (viewport.left + viewport.width / 2);
}

/**
 * Whether `node` sits inside `container`, counting nodes inside the shadow roots
 * of its descendants, as a selection in a rendered row does.
 */
export function containsAcrossShadowRoots(container: Node, node: Node | null): boolean {
  let current: Node | null = node;
  while (current) {
    if (container.contains(current)) return true;
    const root = current.getRootNode();
    current = root instanceof ShadowRoot ? root.host : null;
  }
  return false;
}
