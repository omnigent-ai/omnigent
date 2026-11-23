// How wide the diff review needs to be for its side tree and its split layout.

import { useLayoutEffect, useState } from "react";

/** Below this width the tree opens as a popover instead of a side column. */
export const WIDE_REVIEW_MIN_WIDTH = 720;

/** The side tree's width until the reader drags it, in pixels. */
export const REVIEW_TREE_DEFAULT_WIDTH = 240;

/** Whether a review this wide shows its tree as a side column; an unmeasured one does. */
export function reviewShowsTreeColumn(width: number | null): boolean {
  return width === null || width === 0 || width >= WIDE_REVIEW_MIN_WIDTH;
}

/**
 * Below this width a split diff leaves conversations and code too narrow to read,
 * so the review shows unified and hides the layout choice; the saved choice is kept.
 */
export const SPLIT_REVIEW_MIN_WIDTH = WIDE_REVIEW_MIN_WIDTH;

/** Below this width the toolbar's view buttons no longer fit beside the counts and a source's controls. */
export const COMPACT_TOOLBAR_MAX_WIDTH = 440;

/** Whether a review this wide needs the compact toolbar; an unmeasured one doesn't. */
export function reviewToolbarIsCompact(width: number | null): boolean {
  return width !== null && width > 0 && width < COMPACT_TOOLBAR_MAX_WIDTH;
}

/** Whether a review this wide can show split; an unmeasured width (pre-layout, tests) can. */
export function reviewCanSplit(width: number | null): boolean {
  return width === null || width === 0 || width >= SPLIT_REVIEW_MIN_WIDTH;
}

/** The element's rendered width, kept current as it resizes; null until measured. */
export function useElementWidth(element: HTMLElement | null): number | null {
  const [width, setWidth] = useState<number | null>(null);
  useLayoutEffect(() => {
    if (!element || typeof ResizeObserver === "undefined") return;
    const measure = () => setWidth(element.getBoundingClientRect().width);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);
  return width;
}
