// Swapping a split's pane with the pane beside it, as ⌘K's and the pane toolbar's Swap with Pane
// commands do: the two trade places and sizes, the dividers stay, and the moved pane keeps focus.

import {
  neighborTerminalPane,
  swapTerminalPanes,
  type TerminalLayout,
  type TerminalPaneDirection,
} from "@/lib/paneSplitLayout";

import { paneCommandLabel, type PaneCommandId } from "./paneCommandTable";
import { zoomedPaneKey } from "./terminalPaneZoom";
import type { SplitTarget } from "./terminalSplitTarget";

export const PANE_SWAP_DIRECTIONS: readonly TerminalPaneDirection[] = [
  "left",
  "right",
  "up",
  "down",
];

/** Each direction's command, as the shortcut registry and the panes' shared table name it. */
export const PANE_SWAP_IDS = {
  left: "terminal.swapLeft",
  right: "terminal.swapRight",
  up: "terminal.swapUp",
  down: "terminal.swapDown",
} as const satisfies Record<TerminalPaneDirection, PaneCommandId>;

/** The label every entry point shows for swapping toward `direction`. */
export function paneSwapLabel(direction: TerminalPaneDirection): string {
  return paneCommandLabel(PANE_SWAP_IDS[direction]);
}

/**
 * The pane `key` would swap with toward `direction`, or null: at that edge, in a lone pane, or
 * while a pane is maximized and its neighbours are hidden. `aspect` is the view's width over height.
 */
export function paneSwapNeighbor(
  layout: TerminalLayout | null | undefined,
  zoomedKey: string | null,
  key: string,
  direction: TerminalPaneDirection,
  aspect: number,
): string | null {
  if (!layout || typeof layout === "string" || zoomedKey !== null) return null;
  return neighborTerminalPane(layout, key, direction, aspect);
}

/** The directions `key` can swap toward now, in {@link PANE_SWAP_DIRECTIONS}' order. */
export function paneSwapDirections(
  layout: TerminalLayout | null | undefined,
  zoomedKey: string | null,
  key: string,
  aspect: number,
): TerminalPaneDirection[] {
  return PANE_SWAP_DIRECTIONS.filter(
    (direction) => paneSwapNeighbor(layout, zoomedKey, key, direction, aspect) !== null,
  );
}

/**
 * Swaps pane `key` of a session's split with its neighbour toward `direction`, as the split is
 * now, and keeps `key` focused. Returns that neighbour, or null when nothing swapped.
 */
export function swapSplitPane(
  target: SplitTarget,
  key: string,
  direction: TerminalPaneDirection,
  aspect: number,
): string | null {
  const zoomedKey = zoomedPaneKey(target);
  let swappedWith: string | null = null;
  target.update((current) => {
    if (!current) return current;
    const neighbor = paneSwapNeighbor(current.layout, zoomedKey, key, direction, aspect);
    if (neighbor === null) return current;
    swappedWith = neighbor;
    return { layout: swapTerminalPanes(current.layout, key, neighbor), focusedKey: key };
  });
  return swappedWith;
}
