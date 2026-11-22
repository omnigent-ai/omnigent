// How far a dragged strip tab may travel: from the first tab's start to the last tab's end,
// in the row's own coordinates. A tab held past either end stops there, so it can't widen the
// row and keep the strip scrolling after its last tab.

/** A box's horizontal extent. */
export interface HorizontalExtent {
  left: number;
  right: number;
}

/** The dragged tab's allowed offset from its own place: back to `min`, forward to `max`. */
export interface StripDragBounds {
  min: number;
  max: number;
}

/** The bounds for dragging `entry` along a row that runs from `first`'s start to `last`'s end. */
export function stripDragBounds(
  entry: HorizontalExtent,
  first: HorizontalExtent,
  last: HorizontalExtent,
): StripDragBounds {
  return {
    min: Math.min(0, first.left - entry.left),
    max: Math.max(0, last.right - entry.right),
  };
}

/**
 * The pointer's offset `x` along the strip, held within `bounds`. The strip scrolled
 * `scrolled` px since the drag began, and the tab moves that much more with the row.
 */
export function clampAlongStrip(x: number, scrolled: number, bounds: StripDragBounds): number {
  return Math.min(Math.max(x + scrolled, bounds.min), bounds.max) - scrolled;
}
