// Split tree for the panes view: leaves are pane keys, splits divide their area by ``ratio``.
// Splits and closes keep the other panes' proportions, and panes can move, swap and resize by
// key. The tree's shape and its pure helpers are the terminal view's (`terminalSplitLayout`).

import {
  replaceTerminalLayoutKey,
  terminalLayoutGeometry,
  terminalLayoutKeys,
  type TerminalDivider,
  type TerminalLayout,
  type TerminalRect,
  type TerminalSplitAxis,
  type TerminalSplitState,
} from "@/lib/terminalSplitLayout";

export { replaceTerminalLayoutKey, terminalLayoutGeometry, terminalLayoutKeys };
export type {
  TerminalDivider,
  TerminalLayout,
  TerminalRect,
  TerminalSplitAxis,
  TerminalSplitState,
};

/** Where a split puts the new pane: right of/below the target, or left of/above it. */
export type TerminalPanePlacement = "after" | "before";
/** The side of a pane another pane can be moved to. */
export type TerminalPaneSide = "left" | "right" | "top" | "bottom";
/** A direction to move focus, swap or nudge toward. */
export type TerminalPaneDirection = "left" | "right" | "up" | "down";

type LayoutSplit = Exclude<TerminalLayout, string>;

// A resize can't shrink either side of a split below 15% of it. Splits and
// closes keep proportions instead, so a long row may hold smaller panes.
export const MIN_SPLIT_RATIO = 0.15;
export const MAX_SPLIT_RATIO = 0.85;

/** ``ratio`` within the resize limits. */
export function clampSplitRatio(ratio: number): number {
  // A zero-size container yields NaN; never persist a ratio the parser would reject.
  if (!Number.isFinite(ratio)) return 0.5;
  return Math.min(MAX_SPLIT_RATIO, Math.max(MIN_SPLIT_RATIO, ratio));
}

function clampResizedRatio(ratio: number, current: number): number {
  if (!Number.isFinite(ratio)) return clampSplitRatio(ratio);
  const low = Math.min(MIN_SPLIT_RATIO, current);
  const high = Math.max(MAX_SPLIT_RATIO, current);
  return Math.min(high, Math.max(low, ratio));
}

interface Insertion {
  layout: TerminalLayout;
  inserted: boolean;
  /** How much the row or column holding the new pane grew, as a share of itself. */
  growth?: number;
}

function insertKeyPreservingProportions(
  layout: TerminalLayout,
  targetKey: string,
  newKey: string,
  axis: TerminalSplitAxis,
  placement: TerminalPanePlacement,
): Insertion {
  if (typeof layout === "string") {
    if (layout !== targetKey) return { layout, inserted: false };
    const children: [TerminalLayout, TerminalLayout] =
      placement === "after" ? [layout, newKey] : [newKey, layout];
    // The new pane starts the target's size, so the target's area doubles.
    return { layout: { axis, ratio: 0.5, children }, inserted: true, growth: 1 };
  }
  for (const index of [0, 1] as const) {
    const result = insertKeyPreservingProportions(
      layout.children[index],
      targetKey,
      newKey,
      axis,
      placement,
    );
    if (!result.inserted) continue;
    const children: [TerminalLayout, TerminalLayout] = [...layout.children];
    children[index] = result.layout;
    // A perpendicular split is outside the row or column that grew.
    if (result.growth === undefined || layout.axis !== axis) {
      return { layout: { ...layout, children }, inserted: true };
    }
    const childShare = index === 0 ? layout.ratio : 1 - layout.ratio;
    const addedShare = childShare * result.growth;
    const total = 1 + addedShare;
    const ratio = index === 0 ? (childShare * (1 + result.growth)) / total : layout.ratio / total;
    return { layout: { ...layout, ratio, children }, inserted: true, growth: addedShare };
  }
  return { layout, inserted: false };
}

/**
 * Split ``targetKey`` in two, placing ``newKey`` after (right of or below) it by
 * default. Along the target's row or column the new pane starts the target's
 * size and the whole row scales to fit, so every other pane keeps its
 * proportions; a split across the row halves only the target.
 */
export function splitTerminalLayout(
  layout: TerminalLayout,
  targetKey: string,
  newKey: string,
  axis: TerminalSplitAxis,
  placement: TerminalPanePlacement = "after",
): TerminalLayout {
  return insertKeyPreservingProportions(layout, targetKey, newKey, axis, placement).layout;
}

interface Removal {
  layout: TerminalLayout | null;
  removed: boolean;
  /** The share of its row or column the removed pane left, along that row's axis. */
  shrinkage?: { axis: TerminalSplitAxis; fraction: number };
}

function removeKeyPreservingProportions(layout: TerminalLayout, key: string): Removal {
  if (typeof layout === "string") {
    return layout === key ? { layout: null, removed: true } : { layout, removed: false };
  }
  for (const index of [0, 1] as const) {
    const child = layout.children[index];
    if (child === key) {
      const fraction = index === 0 ? layout.ratio : 1 - layout.ratio;
      return {
        layout: layout.children[1 - index],
        removed: true,
        shrinkage: { axis: layout.axis, fraction },
      };
    }
    const result = removeKeyPreservingProportions(child, key);
    if (!result.removed || result.layout === null) continue;
    const children: [TerminalLayout, TerminalLayout] = [...layout.children];
    children[index] = result.layout;
    const shrinkage = result.shrinkage;
    // A perpendicular split is outside the row or column the pane left.
    if (shrinkage === undefined || shrinkage.axis !== layout.axis) {
      return { layout: { ...layout, children }, removed: true };
    }
    const childShare = index === 0 ? layout.ratio : 1 - layout.ratio;
    const removedShare = childShare * shrinkage.fraction;
    const remaining = 1 - removedShare;
    const ratio =
      index === 0 ? (childShare * (1 - shrinkage.fraction)) / remaining : layout.ratio / remaining;
    return {
      layout: { ...layout, ratio, children },
      removed: true,
      shrinkage: { axis: layout.axis, fraction: removedShare },
    };
  }
  return { layout, removed: false };
}

/**
 * Drop ``key``'s pane. Panes in the same row or column keep their relative
 * sizes as they share the freed space. Null when nothing is left.
 */
export function removeTerminalLayoutKey(
  layout: TerminalLayout,
  key: string,
): TerminalLayout | null {
  const result = removeKeyPreservingProportions(layout, key);
  return result.removed ? result.layout : layout;
}

/** Whether ``a`` and ``b`` split the same way around the same panes, whatever their ratios. */
function sameArrangement(a: TerminalLayout, b: TerminalLayout): boolean {
  if (typeof a === "string" || typeof b === "string") return a === b;
  return (
    a.axis === b.axis &&
    sameArrangement(a.children[0], b.children[0]) &&
    sameArrangement(a.children[1], b.children[1])
  );
}

/**
 * Move ``key``'s pane to ``side`` of ``targetKey``'s: it leaves its place, as a close would,
 * and takes that half of the target. Returns ``layout`` itself when that changes nothing: the
 * same pane, a pane that isn't there, or a drop where the pane already is.
 */
export function moveTerminalPane(
  layout: TerminalLayout,
  key: string,
  targetKey: string,
  side: TerminalPaneSide,
): TerminalLayout {
  if (key === targetKey || typeof layout === "string") return layout;
  const keys = terminalLayoutKeys(layout);
  if (!keys.includes(key) || !keys.includes(targetKey)) return layout;
  const rest = removeTerminalLayoutKey(layout, key);
  if (rest === null) return layout;
  const axis: TerminalSplitAxis = side === "left" || side === "right" ? "columns" : "rows";
  const before = side === "left" || side === "top";
  const split: LayoutSplit = {
    axis,
    ratio: 0.5,
    children: before ? [key, targetKey] : [targetKey, key],
  };
  const moved = mapLayoutLeaves(rest, (leaf) => (leaf === targetKey ? split : leaf));
  return sameArrangement(layout, moved) ? layout : moved;
}

/**
 * Trade ``key``'s and ``otherKey``'s places: each takes the other's spot and size, and every
 * divider stays put. Returns ``layout`` itself for the same pane or one that isn't there.
 */
export function swapTerminalPanes(
  layout: TerminalLayout,
  key: string,
  otherKey: string,
): TerminalLayout {
  if (key === otherKey || typeof layout === "string") return layout;
  const keys = terminalLayoutKeys(layout);
  if (!keys.includes(key) || !keys.includes(otherKey)) return layout;
  return mapLayoutLeaves(layout, (leaf) => {
    if (leaf === key) return otherKey;
    if (leaf === otherKey) return key;
    return leaf;
  });
}

/** ``layout`` with each pane replaced by what ``replace`` makes of its key. */
function mapLayoutLeaves(
  layout: TerminalLayout,
  replace: (key: string) => TerminalLayout,
): TerminalLayout {
  if (typeof layout === "string") return replace(layout);
  const [first, second] = layout.children;
  return {
    ...layout,
    children: [mapLayoutLeaves(first, replace), mapLayoutLeaves(second, replace)],
  };
}

/**
 * Set the split at ``path`` to ``ratio``, within the resize limits. A split already past a limit,
 * as in a long row, may move back in but never further out, and never jumps to the limit.
 */
export function resizeTerminalSplit(
  layout: TerminalLayout,
  path: readonly number[],
  ratio: number,
): TerminalLayout {
  if (typeof layout === "string") return layout;
  if (path.length === 0) return { ...layout, ratio: clampResizedRatio(ratio, layout.ratio) };
  const [index, ...rest] = path;
  const children: [TerminalLayout, TerminalLayout] = [...layout.children];
  children[index] = resizeTerminalSplit(children[index], rest, ratio);
  return { ...layout, children };
}

/**
 * Drop panes whose terminal is gone and move a stale agent pane onto the live
 * agent terminal. ``removable`` says whether a missing key is known gone, as
 * opposed to not yet delivered. Returns ``state`` itself when nothing changes.
 */
export function reconcileTerminalSplit(
  state: TerminalSplitState,
  liveKeys: ReadonlySet<string>,
  {
    liveAgentKey,
    isAgentKey,
    removable,
  }: {
    liveAgentKey: string | null;
    isAgentKey: (key: string) => boolean;
    removable: (key: string) => boolean;
  },
): TerminalSplitState | undefined {
  let next: TerminalSplitState | undefined = state;
  for (const key of terminalLayoutKeys(state.layout)) {
    if (next === undefined) break;
    if (liveKeys.has(key)) continue;
    if (isAgentKey(key)) {
      if (liveAgentKey && !terminalLayoutKeys(next.layout).includes(liveAgentKey)) {
        next = {
          layout: replaceTerminalLayoutKey(next.layout, key, liveAgentKey),
          focusedKey: next.focusedKey === key ? liveAgentKey : next.focusedKey,
        };
      }
    } else if (removable(key)) {
      // A shell that exited closes like one the user closed.
      next = closeTerminalPane(next, key);
    }
  }
  if (next === undefined || next.layout === state.layout) return next;
  const keys = terminalLayoutKeys(next.layout);
  return keys.includes(next.focusedKey) ? next : { ...next, focusedKey: keys[0] };
}

/**
 * How deep a saved split may nest. A pane's minimum size keeps real layouts far shallower; a
 * damaged value nested deeper restores as unsplit instead of overflowing the stack.
 */
export const MAX_SAVED_LAYOUT_DEPTH = 64;

/** Validate a persisted split; anything malformed restores as unsplit. */
export function parseTerminalSplitState(value: unknown): TerminalSplitState | undefined {
  const validLayout = (node: unknown, depth = 0): node is TerminalLayout => {
    if (typeof node === "string") return node.length > 0;
    if (!node || typeof node !== "object" || depth >= MAX_SAVED_LAYOUT_DEPTH) return false;
    const { axis, ratio, children } = node as Record<string, unknown>;
    return (
      (axis === "columns" || axis === "rows") &&
      typeof ratio === "number" &&
      ratio > 0 &&
      ratio < 1 &&
      Array.isArray(children) &&
      children.length === 2 &&
      children.every((child) => validLayout(child, depth + 1))
    );
  };
  if (!value || typeof value !== "object") return undefined;
  const { layout, focusedKey } = value as Record<string, unknown>;
  if (!validLayout(layout) || typeof layout === "string" || typeof focusedKey !== "string") {
    return undefined;
  }
  const keys = terminalLayoutKeys(layout);
  if (new Set(keys).size !== keys.length || !keys.includes(focusedKey)) return undefined;
  return { layout, focusedKey };
}

/** How far one resize step moves a divider, as a share of its split. */
export const SPLIT_NUDGE_STEP = 0.03;

/**
 * ``ratio`` moved one step by ``delta`` within the resize limits, or null when
 * it can't move that way. A long row's split can sit past a limit; a step
 * never pulls it the other way.
 */
export function steppedSplitRatio(ratio: number, delta: number): number | null {
  const next = clampSplitRatio(ratio + delta);
  return Math.sign(next - ratio) === Math.sign(delta) ? next : null;
}

const GEOMETRY_EPSILON = 1e-6;

function directionAxis(direction: TerminalPaneDirection): TerminalSplitAxis {
  return direction === "left" || direction === "right" ? "columns" : "rows";
}

/** Child indexes from the root to ``key``'s leaf, or null when it isn't in the layout. */
function pathToKey(layout: TerminalLayout, key: string): number[] | null {
  if (typeof layout === "string") return layout === key ? [] : null;
  for (const index of [0, 1] as const) {
    const rest = pathToKey(layout.children[index], key);
    if (rest !== null) return [index, ...rest];
  }
  return null;
}

function nodeAt(layout: TerminalLayout, path: readonly number[]): TerminalLayout {
  return path.reduce<TerminalLayout>(
    (node, index) => (typeof node === "string" ? node : node.children[index]),
    layout,
  );
}

/** How far apart two intervals are (0 when they touch or overlap), and how much they overlap. */
function intervalRelation(start: number, end: number, otherStart: number, otherEnd: number) {
  const overlap = Math.min(end, otherEnd) - Math.max(start, otherStart);
  return { overlap: Math.max(0, overlap), separation: Math.max(0, -overlap) };
}

interface PaneEdges {
  left: number;
  right: number;
  top: number;
  bottom: number;
}

/** How far ``pane`` lies past ``from``'s edge in ``direction``; negative where they overlap. */
function gapToward(direction: TerminalPaneDirection, from: PaneEdges, pane: PaneEdges): number {
  switch (direction) {
    case "right":
      return pane.left - from.right;
    case "left":
      return from.left - pane.right;
    case "down":
      return pane.top - from.bottom;
    case "up":
      return from.top - pane.bottom;
  }
}

const compareLengths = (a: number, b: number): number =>
  Math.abs(a - b) <= GEOMETRY_EPSILON ? 0 : a - b;

/**
 * The pane next to ``fromKey`` in ``direction``, or null at that edge (focus
 * doesn't wrap). Panes beside it along the other axis come first, then the
 * nearest; ties go to the larger overlap, the nearer center, then layout order.
 * ``aspect`` is the view's width over its height, so distances are physical.
 */
export function neighborTerminalPane(
  layout: TerminalLayout,
  fromKey: string,
  direction: TerminalPaneDirection,
  aspect = 1,
): string | null {
  // Measure both axes in units of the view's height.
  const panes = terminalLayoutGeometry(layout).panes.map((pane, order) => ({
    key: pane.key,
    order,
    left: pane.left * aspect,
    right: (pane.left + pane.width) * aspect,
    top: pane.top,
    bottom: pane.top + pane.height,
  }));
  const from = panes.find((pane) => pane.key === fromKey);
  if (!from) return null;
  const horizontal = directionAxis(direction) === "columns";
  const candidates = panes.flatMap((pane) => {
    if (pane.key === fromKey) return [];
    const gap = gapToward(direction, from, pane);
    if (gap < -GEOMETRY_EPSILON) return [];
    const across = horizontal
      ? intervalRelation(from.top, from.bottom, pane.top, pane.bottom)
      : intervalRelation(from.left, from.right, pane.left, pane.right);
    const centerOffset = horizontal
      ? Math.abs((pane.top + pane.bottom) / 2 - (from.top + from.bottom) / 2)
      : Math.abs((pane.left + pane.right) / 2 - (from.left + from.right) / 2);
    const distance = Math.hypot(Math.max(0, gap), across.separation);
    return [
      { ...pane, beside: across.overlap > GEOMETRY_EPSILON, distance, ...across, centerOffset },
    ];
  });
  // Lengths within rounding of each other are equal, so flush edges fall through to the overlap.
  candidates.sort(
    (a, b) =>
      Number(b.beside) - Number(a.beside) ||
      compareLengths(a.distance, b.distance) ||
      compareLengths(b.overlap, a.overlap) ||
      compareLengths(a.centerOffset, b.centerOffset) ||
      a.order - b.order,
  );
  return candidates[0]?.key ?? null;
}

/**
 * Move the divider nearest ``key`` along ``direction``'s axis one step that way,
 * within the split limits. Returns ``layout`` itself when no such divider exists
 * or it is already at its limit.
 */
export function nudgeTerminalSplit(
  layout: TerminalLayout,
  key: string,
  direction: TerminalPaneDirection,
  step = SPLIT_NUDGE_STEP,
): TerminalLayout {
  const path = pathToKey(layout, key);
  if (path === null) return layout;
  const axis = directionAxis(direction);
  for (let depth = path.length - 1; depth >= 0; depth--) {
    const ancestorPath = path.slice(0, depth);
    const ancestor = nodeAt(layout, ancestorPath);
    if (typeof ancestor === "string" || ancestor.axis !== axis) continue;
    const delta = direction === "right" || direction === "down" ? step : -step;
    const ratio = steppedSplitRatio(ancestor.ratio, delta);
    return ratio === null ? layout : resizeTerminalSplit(layout, ancestorPath, ratio);
  }
  return layout;
}

/** Cells ``layout`` takes along ``axis``: a perpendicular split counts as one. */
function tileUnits(layout: TerminalLayout, axis: TerminalSplitAxis): number {
  if (typeof layout === "string" || layout.axis !== axis) return 1;
  return tileUnits(layout.children[0], axis) + tileUnits(layout.children[1], axis);
}

/**
 * Give panes equal space along each split's axis: ``A | (B | C)`` becomes
 * thirds, and a group split the other way counts as one cell. The tree's shape
 * doesn't change.
 */
export function equalizeTerminalLayout(layout: TerminalLayout): TerminalLayout {
  if (typeof layout === "string") return layout;
  const first = tileUnits(layout.children[0], layout.axis);
  const second = tileUnits(layout.children[1], layout.axis);
  return {
    axis: layout.axis,
    ratio: first / (first + second),
    children: [
      equalizeTerminalLayout(layout.children[0]),
      equalizeTerminalLayout(layout.children[1]),
    ],
  };
}

/**
 * Close ``key``'s pane. A focused pane hands focus to the leaf of its sibling
 * subtree next to it (the sibling's first leaf for a first child, its last for
 * a second). Undefined when one pane would be left, which is the unsplit view.
 */
export function closeTerminalPane(
  state: TerminalSplitState,
  key: string,
): TerminalSplitState | undefined {
  const path = pathToKey(state.layout, key);
  if (path === null || path.length === 0) return state;
  const index = path[path.length - 1];
  const sibling = nodeAt(state.layout, [...path.slice(0, -1), 1 - index]);
  const siblingKeys = terminalLayoutKeys(sibling);
  const layout = removeTerminalLayoutKey(state.layout, key);
  if (layout === null || typeof layout === "string") return undefined;
  if (state.focusedKey !== key) return { layout, focusedKey: state.focusedKey };
  // The keyboard goes to the sibling's pane nearest the closed one.
  const nearest = index === 0 ? siblingKeys[0] : siblingKeys[siblingKeys.length - 1];
  return { layout, focusedKey: nearest };
}

/** A view's or pane's size in terminal cells. */
export interface PaneCells {
  columns: number;
  rows: number;
}

/** The smallest a pane may be made, in terminal cells: columns wide and rows tall. */
export const MIN_PANE_CELLS: Readonly<PaneCells> = Object.freeze({ columns: 20, rows: 6 });
