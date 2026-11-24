import type { FileTreeScrollOffset } from "./types";

interface FileTreeScrollTargetInput {
  currentScrollTop: number;
  index: number;
  itemHeight: number;
  offset: FileTreeScrollOffset;
  totalHeight: number;
  viewportHeight: number;
}

/**
 * The `scrollTop` that brings row `index` into view, or `null` when no scroll
 * is needed. `nearest` scrolls the minimum distance; `top` and `center` align
 * the row, clamped to the scrollable range.
 */
export function computeScrollTarget({
  currentScrollTop,
  index,
  itemHeight,
  offset,
  totalHeight,
  viewportHeight,
}: FileTreeScrollTargetInput): number | null {
  if (index < 0) return null;

  const itemTop = index * itemHeight;
  const itemBottom = itemTop + itemHeight;
  let nextScrollTop: number;
  if (offset === "nearest") {
    if (itemTop < currentScrollTop) nextScrollTop = itemTop;
    else if (itemBottom > currentScrollTop + viewportHeight)
      nextScrollTop = itemBottom - viewportHeight;
    else return null;
  } else {
    const viewportOffset = offset === "center" ? Math.max(0, (viewportHeight - itemHeight) / 2) : 0;
    nextScrollTop = itemTop - viewportOffset;
  }

  const clamped = Math.max(0, Math.min(nextScrollTop, Math.max(0, totalHeight - viewportHeight)));
  return clamped === currentScrollTop ? null : clamped;
}

interface FileTreeWindow {
  /** Inclusive row range to render, including overscan. Empty when `end < start`. */
  start: number;
  end: number;
}

export function computeWindow({
  itemCount,
  itemHeight,
  overscan,
  scrollTop,
  viewportHeight,
}: {
  itemCount: number;
  itemHeight: number;
  overscan: number;
  scrollTop: number;
  viewportHeight: number;
}): FileTreeWindow {
  if (itemCount <= 0) return { start: 0, end: -1 };
  const firstVisible = Math.floor(scrollTop / itemHeight);
  const lastVisible = Math.ceil((scrollTop + viewportHeight) / itemHeight) - 1;
  return {
    start: Math.max(0, Math.min(itemCount - 1, firstVisible - overscan)),
    end: Math.max(0, Math.min(itemCount - 1, lastVisible + overscan)),
  };
}
