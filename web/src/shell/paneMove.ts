// Placing a pane on another pane's side: moving a split's pane there, as a drag of its strip row
// or ⌘K's Move Pane… does, or inserting a new one, as a dropped sidebar tab does. Every preview
// and every commit reads the same placed layout.

import { MoveIcon } from "lucide-react";

import {
  moveTerminalPane,
  splitTerminalLayout,
  terminalLayoutGeometry,
  terminalLayoutKeys,
  type TerminalLayout,
  type TerminalPaneSide,
  type TerminalRect,
} from "@/lib/paneSplitLayout";
import type { CommandAvailability, PaletteCommand } from "@/store/paletteCommands";

import { paneCommandLabel } from "./paneCommandTable";
import type { PaneSizing } from "./paneSizing";
import { zoomedPaneKey } from "./terminalPaneZoom";
import type { SplitTarget } from "./terminalSplitTarget";

export const PANE_SIDES: readonly TerminalPaneSide[] = ["left", "right", "top", "bottom"];

/** ⌘K's label for moving the pane you're in, from the panes' shared command table. */
export const MOVE_PANE_LABEL = paneCommandLabel("terminal.movePane");

/** Said when no spot keeps every pane big enough. */
export const NO_ROOM_TO_MOVE_REASON = "Not enough room to move this pane.";

export interface PaneMoveDestination {
  targetKey: string;
  side: TerminalPaneSide;
  /** The split once the pane is there. */
  layout: TerminalLayout;
}

/**
 * A pane placed on `side` of `targetKey`'s: one already in the split moving there, or a new one
 * inserted there, as a sidebar tab dropped on the panes is.
 */
export interface PanePlacement {
  kind: "move" | "insert";
  key: string;
  targetKey: string;
  side: TerminalPaneSide;
}

/** Placing a pane on `side` of another: the split it makes and which half it takes. */
export const splitOfSide = (side: TerminalPaneSide) => ({
  axis: side === "left" || side === "right" ? ("columns" as const) : ("rows" as const),
  placement: side === "left" || side === "top" ? ("before" as const) : ("after" as const),
});

/**
 * The layout `placement` makes of `layout`: what its drop, ⌘K or menu commits. A move leaves its
 * place as a close would; an insert keeps the other panes' proportions, as a split does. Returns
 * `layout` itself when nothing would change.
 */
export function placedLayout(layout: TerminalLayout, placement: PanePlacement): TerminalLayout {
  const { kind, key, targetKey, side } = placement;
  if (kind === "move") return moveTerminalPane(layout, key, targetKey, side);
  const keys = terminalLayoutKeys(layout);
  if (keys.includes(key) || !keys.includes(targetKey)) return layout;
  const { axis, placement: where } = splitOfSide(side);
  return splitTerminalLayout(layout, targetKey, key, axis, where);
}

/**
 * Where `placement`'s pane would sit, in percent of the view, read from the layout it makes, so
 * a preview always shows where the pane lands. Null when nothing would change.
 */
export function placedPaneArea(
  layout: TerminalLayout,
  placement: PanePlacement,
): TerminalRect | null {
  const placed = placedLayout(layout, placement);
  if (placed === layout) return null;
  const pane = terminalLayoutGeometry(placed).panes.find(({ key }) => key === placement.key);
  return pane ? { left: pane.left, top: pane.top, width: pane.width, height: pane.height } : null;
}

/**
 * Where `key`'s pane can go in `layout`: each side of every other pane that moves it, in layout
 * order, and that keeps every pane to `sizing`'s minimum, when there's a rule to ask.
 */
export function paneMoveDestinations(
  layout: TerminalLayout,
  key: string,
  sizing: PaneSizing | null,
): PaneMoveDestination[] {
  const destinations: PaneMoveDestination[] = [];
  for (const targetKey of terminalLayoutKeys(layout)) {
    if (targetKey === key) continue;
    for (const side of PANE_SIDES) {
      const moved = placedLayout(layout, { kind: "move", key, targetKey, side });
      if (moved === layout) continue;
      if (sizing && !sizing.keeps(layout, moved)) continue;
      destinations.push({ targetKey, side, layout: moved });
    }
  }
  return destinations;
}

/**
 * The split whose panes can move now, by a drag or ⌘K: two or more panes shown, none of them
 * maximized. Null otherwise, when neither offers a move.
 */
export function paneMoveLayout(
  layout: TerminalLayout | null | undefined,
  zoomedKey: string | null,
): TerminalLayout | null {
  if (!layout || typeof layout === "string" || zoomedKey !== null) return null;
  return layout;
}

/** Whether `targetKey`'s `side` is among `destinations`. */
export function isPaneMoveDestination(
  destinations: readonly PaneMoveDestination[],
  targetKey: string,
  side: TerminalPaneSide,
): boolean {
  return destinations.some(
    (destination) => destination.targetKey === targetKey && destination.side === side,
  );
}

const SIDE_LABELS: Record<TerminalPaneSide, string> = {
  left: "Left of",
  right: "Right of",
  top: "Above",
  bottom: "Below",
};

/**
 * Moves pane `key` of a session's split to `targetKey`'s `side`, if that spot is still among the
 * split's {@link paneMoveDestinations} as it is now and no pane is maximized, and makes it the
 * focused pane.
 */
export function moveSplitPane(
  target: SplitTarget,
  { key, targetKey, side }: { key: string; targetKey: string; side: TerminalPaneSide },
  sizing: PaneSizing | null,
): void {
  if (zoomedPaneKey(target) !== null) return;
  target.update((current) => {
    if (!current) return current;
    const destination = paneMoveDestinations(current.layout, key, sizing).find(
      (candidate) => candidate.targetKey === targetKey && candidate.side === side,
    );
    return destination ? { layout: destination.layout, focusedKey: key } : current;
  });
}

/** A spot a dragged pane would take: a side of the pane under the pointer. */
export interface PaneDropSpot {
  targetKey: string;
  side: TerminalPaneSide;
}

/**
 * How much nearer, as a share of the pane's size, another side must be before the spot shown
 * gives way to it, so the preview doesn't flicker while the pointer runs along a diagonal.
 */
export const PANE_DROP_HYSTERESIS = 0.08;

/**
 * The pane under `point` (percent of the view) in `layout`, and the side whose zone holds the
 * point: the pane's diagonals cut it into four, so below its middle is Bottom in a tall pane as
 * in a wide one. `shown`, the spot offered so far, stays until another side is clearly nearer.
 * Null outside every pane.
 */
export function paneDropSpot(
  layout: TerminalLayout,
  point: { x: number; y: number },
  shown: PaneDropSpot | null = null,
): PaneDropSpot | null {
  const pane = terminalLayoutGeometry(layout).panes.find(
    (candidate) =>
      point.x >= candidate.left &&
      point.x <= candidate.left + candidate.width &&
      point.y >= candidate.top &&
      point.y <= candidate.top + candidate.height,
  );
  if (!pane) return null;
  // Each side's distance as a share of the pane's width or height, so the nearest side is the
  // zone the diagonals put the point in, whatever the pane's shape.
  const across = (offset: number, size: number) => (size > 0 ? offset / size : 0);
  const distances: Record<TerminalPaneSide, number> = {
    left: across(point.x - pane.left, pane.width),
    right: across(pane.left + pane.width - point.x, pane.width),
    top: across(point.y - pane.top, pane.height),
    bottom: across(pane.top + pane.height - point.y, pane.height),
  };
  const nearest = PANE_SIDES.reduce((best, candidate) =>
    distances[candidate] < distances[best] ? candidate : best,
  );
  const side =
    shown &&
    shown.targetKey === pane.key &&
    distances[shown.side] - distances[nearest] < PANE_DROP_HYSTERESIS
      ? shown.side
      : nearest;
  return { targetKey: pane.key, side };
}

/** A spot's name on Move Pane…'s page, such as "Below zsh". */
export function paneMoveDestinationLabel(side: TerminalPaneSide, targetName: string): string {
  return `${SIDE_LABELS[side]} ${targetName}`;
}

/** What a split's host tells ⌘K's Move Pane… about the pane you're in, read when it's asked. */
export interface PaneMoveSource {
  /** The split, its maximized pane if any, and the pane to move; null with nothing to move. */
  read: () => { layout: TerminalLayout; zoomedKey: string | null; key: string } | null;
  /** The size rule, measured now; null until the panes have a size. */
  sizing: () => PaneSizing | null;
  /** The name a pane's row shows. */
  nameOf: (key: string) => string;
  /** Moves the pane there; called only once that spot is chosen. */
  move: (destination: { key: string; targetKey: string; side: TerminalPaneSide }) => void;
}

/**
 * ⌘K's Move Pane… for the pane `source` reads: its options are every spot
 * {@link paneMoveDestinations} lists, read again as one is chosen, so a pane closed or a split
 * changed meanwhile never moves anything somewhere else.
 */
export function movePaneCommand(
  id: string,
  source: PaneMoveSource,
  availability: CommandAvailability,
): PaletteCommand {
  return {
    id,
    label: MOVE_PANE_LABEL,
    icon: MoveIcon,
    keywords: ["terminal", "pane", "move", "rearrange", "split", "drag"],
    availability,
    optionsEmptyMessage: NO_ROOM_TO_MOVE_REASON,
    options: () => paneMoveOptions(source),
    run: () => {},
  };
}

/** One spot Move Pane offers, as ⌘K's options page and a pane toolbar's submenu list it. */
export interface PaneMoveOption {
  id: string;
  /** Such as "Right of zsh". */
  label: string;
  keywords: string[];
  run: () => void;
}

/** Every spot `source`'s pane can move to now, in {@link paneMoveDestinations}' order. */
export function paneMoveOptions(source: PaneMoveSource): PaneMoveOption[] {
  const current = source.read();
  const layout = current && paneMoveLayout(current.layout, current.zoomedKey);
  if (!current || !layout) return [];
  return paneMoveDestinations(layout, current.key, source.sizing()).map(({ targetKey, side }) => {
    return {
      id: `${current.key}>${targetKey}:${side}`,
      label: paneMoveDestinationLabel(side, source.nameOf(targetKey)),
      keywords: [source.nameOf(targetKey)],
      run: () => source.move({ key: current.key, targetKey, side }),
    };
  });
}

/** The element a split's panes are laid out in, by its `SplitPanesLayout` conversation id. */
export function splitPanesRoot(conversationId: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-split-panes="${CSS.escape(conversationId)}"]`);
}

/**
 * Move Pane…'s availability: listed where {@link paneMoveLayout} lets panes move, greyed with why
 * when `sizing` (measured now) refuses every spot.
 */
export function movePaneAvailability({
  layout: shown,
  zoomedKey,
  key,
  sizing,
}: {
  layout: TerminalLayout | null | undefined;
  zoomedKey: string | null;
  key: string | null;
  sizing: PaneSizing | null;
}): CommandAvailability | null {
  const layout = paneMoveLayout(shown, zoomedKey);
  if (layout === null || key === null) return null;
  if (!terminalLayoutKeys(layout).includes(key)) return null;
  return paneMoveDestinations(layout, key, sizing).length > 0
    ? { available: true }
    : { available: false, reason: NO_ROOM_TO_MOVE_REASON };
}

/** A place off the split a dragged pane may be dropped on. */
export interface PaneDragOutside {
  /** The pointer is at `point`, off every pane spot, or null once it's on one or the drag ends. */
  over: (key: string, point: { x: number; y: number } | null) => void;
  /** Dropped at `point` off every pane spot: true when it went there. */
  drop: (key: string, point: { x: number; y: number }) => boolean;
}
