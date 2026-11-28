// How tall a kind of review's toolbar drew at each width, so a placeholder in its frame leaves
// the same room and the review's content doesn't move when it fills in. Kept for this window.

import { useLayoutEffect, useRef, type ReactNode } from "react";

/** The heights one kind of review's toolbar drew, by the review's width. */
export interface ReviewToolbarHeights {
  /** The review, `width` wide, drew its toolbar `height` tall. */
  remember: (width: number, height: number) => void;
  /** The height it drew at this width, or null when it hasn't drawn at it. */
  at: (width: number | null) => number | null;
}

/** Enough widths for the panes and windows one session moves a review between. */
const REMEMBERED_WIDTHS = 8;

export function createReviewToolbarHeights(): ReviewToolbarHeights {
  const byWidth = new Map<number, number>();
  return {
    remember(width, height) {
      const key = Math.round(width);
      if (key <= 0 || height <= 0) return;
      // Last drawn last, so the oldest width goes first.
      byWidth.delete(key);
      byWidth.set(key, Math.round(height));
      const oldest = byWidth.keys().next();
      if (byWidth.size > REMEMBERED_WIDTHS && !oldest.done) byWidth.delete(oldest.value);
    },
    at(width) {
      if (width === null || width <= 0) return null;
      return byWidth.get(Math.round(width)) ?? null;
    },
  };
}

/** A pull request's Changes, whose placeholder the Pull Requests page shows while it reads. */
export const PULL_REQUEST_REVIEW_TOOLBAR_HEIGHTS = createReviewToolbarHeights();

/**
 * The review's toolbar rows, recorded into `heights` as they wrap: the review's full width,
 * so its width is the review's.
 */
export function RecordedReviewToolbar({
  heights,
  children,
}: {
  heights: ReviewToolbarHeights;
  children: ReactNode;
}) {
  const element = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const toolbar = element.current;
    if (!toolbar || typeof ResizeObserver === "undefined") return;
    const record = () => {
      const { width, height } = toolbar.getBoundingClientRect();
      heights.remember(width, height);
    };
    record();
    const observer = new ResizeObserver(record);
    observer.observe(toolbar);
    return () => observer.disconnect();
  }, [heights]);
  return (
    <div ref={element} data-recorded-review-toolbar className="shrink-0">
      {children}
    </div>
  );
}
