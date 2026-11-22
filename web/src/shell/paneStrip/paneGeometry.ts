// Where a pane's terminal draws inside its tile, below its strip row, for both the pane layout and
// the size rule. Tiles meet at the divider, which draws its 1 px line over their edges.

import { MIN_PANE_CELLS, type TerminalLayout, terminalLayoutGeometry } from "@/lib/paneSplitLayout";

import { PANE_STRIP_MIN_WIDTH } from "./paneStripLayout";

/** The strip row's height above every pane's terminal, in CSS px (h-7). */
export const PANE_ROW_PX = 28;

/** What a pane's chrome takes from its tile, in CSS px: across, and down including the row. */
export const PANE_CHROME = Object.freeze({ x: 0, y: PANE_ROW_PX });

/** What a pane's content loses besides the shared chrome, in CSS px: padding, a scrollbar. */
export interface PaneInset {
  x: number;
  y: number;
}

/** A pane's size after a layout: its row's room, and its content in whole units. */
export interface PaneContentSize {
  /** The row's width, less the window's controls at the top-left corner. */
  rowWidth: number;
  width: number;
  height: number;
}

/**
 * Each pane's content box when `layout` fills `area` (CSS px): its tile less the chrome and
 * `inset`, floored to whole pixels as the content's renderer measures it. `corner` is the room
 * the window's controls take from the top-left pane's row; `topRow` is the row's height for the
 * panes along the view's top edge.
 */
export function paneContentBoxes(
  layout: TerminalLayout,
  area: { width: number; height: number },
  inset: PaneInset = { x: 0, y: 0 },
  corner = 0,
  topRow: number = PANE_ROW_PX,
): Map<string, PaneContentSize> {
  const chrome = PANE_CHROME;
  const boxes = new Map<string, PaneContentSize>();
  for (const pane of terminalLayoutGeometry(layout).panes) {
    const tileWidth = (pane.width / 100) * area.width;
    const tileHeight = (pane.height / 100) * area.height;
    const atCorner = pane.left === 0 && pane.top === 0;
    const rowHeight = pane.top === 0 ? topRow : chrome.y;
    boxes.set(pane.key, {
      rowWidth: tileWidth - chrome.x - (atCorner ? corner : 0),
      width: Math.max(0, Math.floor(tileWidth - chrome.x - inset.x)),
      height: Math.max(0, Math.floor(tileHeight - rowHeight - inset.y)),
    });
  }
  return boxes;
}

/** How a split's session terminals measure, as one live pane shows. */
export interface PaneContentMeasure {
  /** The panes' area, CSS px. */
  area: { width: number; height: number };
  /** The terminal's padding and the room its renderer keeps; the same for every pane of a view. */
  inset: PaneInset;
  /** One terminal cell, CSS px. */
  cell: { width: number; height: number };
  /** The room the window's controls take from the top-left pane's row, CSS px. */
  corner?: number;
  /** The row's height for panes along the view's top edge, CSS px; PANE_ROW_PX otherwise. */
  topRow?: number;
}

/** Each pane's whole cells when `layout` fills the measured area, as the renderer fits them. */
export function paneContentCells(
  layout: TerminalLayout,
  measure: PaneContentMeasure,
): Map<string, PaneContentSize> {
  const cells = new Map<string, PaneContentSize>();
  const boxes = paneContentBoxes(
    layout,
    measure.area,
    measure.inset,
    measure.corner,
    measure.topRow,
  );
  for (const [key, box] of boxes) {
    cells.set(key, {
      rowWidth: box.rowWidth,
      width: Math.floor(box.width / measure.cell.width),
      height: Math.floor(box.height / measure.cell.height),
    });
  }
  return cells;
}

/** The terminal minimum in cells: 20 columns by 6 rows. */
export const MINIMUM_PANE_CELLS = Object.freeze({
  width: MIN_PANE_CELLS.columns,
  height: MIN_PANE_CELLS.rows,
});

const meets = (size: PaneContentSize, minimum: { width: number; height: number }) =>
  size.width >= minimum.width &&
  size.height >= minimum.height &&
  size.rowWidth >= PANE_STRIP_MIN_WIDTH;

/** Whether every pane of `sizes` keeps `minimum`, and its row its floor. */
export const paneSizesFit = (
  sizes: ReadonlyMap<string, PaneContentSize>,
  minimum: { width: number; height: number },
) => [...sizes.values()].every((size) => meets(size, minimum));

/**
 * Whether going from `before` to `after` leaves no pane worse off: each ends at the minimum or
 * no smaller than it was. A pane already too small may grow, never shrink.
 */
export function paneSizesKeep(
  before: ReadonlyMap<string, PaneContentSize>,
  after: ReadonlyMap<string, PaneContentSize>,
  minimum: { width: number; height: number },
): boolean {
  for (const [key, size] of after) {
    const previous = before.get(key);
    if (!previous) continue;
    if (size.width < previous.width && size.width < minimum.width) return false;
    if (size.height < previous.height && size.height < minimum.height) return false;
    if (size.rowWidth < previous.rowWidth && size.rowWidth < PANE_STRIP_MIN_WIDTH) return false;
  }
  return true;
}
