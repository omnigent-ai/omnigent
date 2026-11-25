// One minimum-size policy for every way into a split (keys, ⌘K, menus, divider drags): each
// terminal keeps 20 x 6 whole cells and each pane its strip row's floor.

import type { TerminalLayout, TerminalSplitAxis } from "@/lib/paneSplitLayout";
import { TERMINAL_PANE } from "@/lib/paneSurfaces";

import {
  MINIMUM_PANE_CELLS,
  PANE_CHROME,
  PANE_ROW_PX,
  paneContentCells,
  type PaneContentMeasure,
  type PaneContentSize,
  paneSizesFit,
  paneSizesKeep,
} from "./paneStrip/paneGeometry";

export const NO_ROOM_MESSAGE = "Not enough room to split this way.";

/** A split's size rule for user actions, over its panes' area now. */
export interface PaneSizing {
  /** Whether every pane of `layout` keeps the minimum. */
  fits: (layout: TerminalLayout) => boolean;
  /** Whether `after` may replace `before`: no pane shrinks below the minimum. */
  keeps: (before: TerminalLayout, after: TerminalLayout) => boolean;
  /** The layout nearest `target` on the way from `from` that keeps the minimum; may be `from`. */
  toward: (target: TerminalLayout, from: TerminalLayout) => TerminalLayout;
  /** What a split refused for room along `axis` tells the user. */
  refusal: (axis: TerminalSplitAxis) => string;
}

/** A rule holding every pane's projected size to `minimum`; Equalize goes whole or not at all. */
function minimumSizing(
  sizesOf: (layout: TerminalLayout) => ReadonlyMap<string, PaneContentSize>,
  minimum: { width: number; height: number },
  refusal: (axis: TerminalSplitAxis) => string,
): PaneSizing {
  return {
    fits: (layout) => paneSizesFit(sizesOf(layout), minimum),
    keeps: (before, after) => paneSizesKeep(sizesOf(before), sizesOf(after), minimum),
    toward: (_target, from) => from,
    refusal,
  };
}

/** Session panes: each terminal keeps 20 x 6 whole cells as it would fit them. */
export function cellPaneSizing(measure: PaneContentMeasure): PaneSizing {
  return minimumSizing(
    (layout) => paneContentCells(layout, measure),
    MINIMUM_PANE_CELLS,
    () => NO_ROOM_MESSAGE,
  );
}

/** The room the window's controls take at the view's top-left, while its row clears them. */
const windowCornerWidth = (root: HTMLElement) =>
  Math.ceil(
    root.querySelector("[data-pane-strip-window-inset]")?.getBoundingClientRect().width ?? 0,
  );

/** The row's height for panes along the view's top edge: taller at the Mac window's top. */
const topRowHeight = (root: HTMLElement) =>
  root.querySelector("[data-pane-strip-title-bar-row]")?.getBoundingClientRect().height ||
  PANE_ROW_PX;

/** The height of `pane`'s own row, as drawn. */
const rowHeightOf = (pane: HTMLElement) =>
  pane.querySelector("[data-pane-strip]")?.getBoundingClientRect().height || PANE_ROW_PX;

/** The element the terminal draws its cells in. */
const DRAWN_GRID = ".xterm-screen";

/**
 * How the session terminals under `root` measure, from a shown pane whose terminal has fitted:
 * its cell from the drawn grid, and what its terminal loses inside the pane besides the shared
 * chrome. Null until one has, when nothing is checked.
 */
export function measurePaneContent(root: HTMLElement | null): PaneContentMeasure | null {
  if (!root) return null;
  const surfaces = root.querySelectorAll<HTMLElement>("[data-terminal-cols][data-terminal-rows]");
  for (const surface of surfaces) {
    const pane = surface.closest<HTMLElement>(TERMINAL_PANE);
    if (!pane || !root.contains(pane) || pane.hasAttribute("data-pane-hidden")) continue;
    const columns = Number(surface.dataset.terminalCols);
    const rows = Number(surface.dataset.terminalRows);
    const grid = surface.querySelector(DRAWN_GRID)?.getBoundingClientRect();
    const tile = pane.getBoundingClientRect();
    const box = surface.getBoundingClientRect();
    if (!(columns > 0 && rows > 0) || !grid || grid.width <= 0 || grid.height <= 0) continue;
    if (box.width <= 0 || box.height <= 0) continue;
    const chrome = { x: PANE_CHROME.x, y: rowHeightOf(pane) };
    const reserve = (value: string | undefined) => Math.max(0, Number(value ?? 0) || 0);
    const area = root.getBoundingClientRect();
    return {
      area: { width: area.width, height: area.height },
      inset: {
        x:
          Math.max(0, Math.round(tile.width - box.width - chrome.x)) +
          reserve(surface.dataset.terminalFitReserveWidth),
        y:
          Math.max(0, Math.round(tile.height - box.height - chrome.y)) +
          reserve(surface.dataset.terminalFitReserveHeight),
      },
      cell: { width: grid.width / columns, height: grid.height / rows },
      corner: windowCornerWidth(root),
      topRow: topRowHeight(root),
    };
  }
  return null;
}

/** The cell rule over the terminals rendered under `root`; null until one has fitted. */
export function domCellSizing(root: HTMLElement | null): PaneSizing | null {
  const measure = measurePaneContent(root);
  return measure === null ? null : cellPaneSizing(measure);
}
