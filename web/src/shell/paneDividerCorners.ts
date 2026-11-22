// Where a split's vertical and horizontal dividers meet, so one drag there resizes both ways.
// Positions are percent of the view, as the layout geometry gives them.

import {
  resizeTerminalSplit,
  type TerminalDivider,
  type TerminalLayout,
  terminalLayoutGeometry,
  terminalLayoutKeys,
} from "@/lib/paneSplitLayout";

export interface DividerCorner {
  /** Stable for the same dividers meeting, as a React key. */
  key: string;
  left: number;
  top: number;
  /** The vertical and horizontal dividers meeting here, each moved by a drag. */
  dividers: TerminalDivider[];
  /** The handle's parts, one per pane corner at the point, each with that corner's diagonal. */
  regions: CornerRegion[];
}

export type CornerQuadrant = "top-left" | "top-right" | "bottom-left" | "bottom-right";
export type CornerCursor = "nwse-resize" | "nesw-resize";

/**
 * The part of a corner's handle over one pane's corner, in percent of the handle. A T's two pane
 * corners split it in halves along its stem; a cross's four split it in quadrants.
 */
export interface CornerRegion {
  /** Where the pane is, from the point. */
  quadrant: CornerQuadrant;
  cursor: CornerCursor;
  left: number;
  top: number;
  width: number;
  height: number;
}

/** A divider resize, as the layout applies it. */
export interface DividerResize {
  path: number[];
  ratio: number;
}

// Geometry is computed from ratios; equal points can differ in the last digits.
const EPSILON = 1e-6;

function linePosition(divider: TerminalDivider): number {
  const { axis, area, ratio } = divider;
  return axis === "columns" ? area.left + area.width * ratio : area.top + area.height * ratio;
}

function within(value: number, start: number, length: number): boolean {
  return value >= start - EPSILON && value <= start + length + EPSILON;
}

/**
 * The points where a vertical divider and a horizontal one touch, each with every divider
 * meeting there: a nested split's divider ends on its parent's, and two aligned ones cross.
 */
export function dividerCorners(dividers: readonly TerminalDivider[]): DividerCorner[] {
  const vertical = dividers.filter((divider) => divider.axis === "columns");
  const horizontal = dividers.filter((divider) => divider.axis === "rows");
  const corners: DividerCorner[] = [];
  for (const column of vertical) {
    const left = linePosition(column);
    for (const row of horizontal) {
      const top = linePosition(row);
      if (!within(left, row.area.left, row.area.width)) continue;
      if (!within(top, column.area.top, column.area.height)) continue;
      const shared = corners.find(
        (corner) => Math.abs(corner.left - left) < EPSILON && Math.abs(corner.top - top) < EPSILON,
      );
      if (shared) {
        for (const divider of [column, row]) {
          if (!shared.dividers.includes(divider)) shared.dividers.push(divider);
        }
      } else {
        corners.push({ key: "", left, top, dividers: [column, row], regions: [] });
      }
    }
  }
  for (const corner of corners) {
    corner.dividers.sort((a, b) => a.path.length - b.path.length);
    corner.key = corner.dividers.map((divider) => divider.path.join(".") || "root").join("|");
    corner.regions = cornerRegions(corner);
  }
  return corners;
}

const QUADRANTS: readonly CornerQuadrant[] = [
  "top-left",
  "top-right",
  "bottom-left",
  "bottom-right",
];

/**
 * One region per pane with a corner at the point: a pane has one in a quadrant where dividers run
 * both along its side and along its top or bottom. A region takes the whole width or height where
 * its neighbouring quadrant has no corner, as at a T.
 */
function cornerRegions({ left, top, dividers }: DividerCorner): CornerRegion[] {
  const reaches = (axis: TerminalDivider["axis"], before: boolean) =>
    dividers.some((divider) => {
      if (divider.axis !== axis) return false;
      const { area } = divider;
      if (axis === "rows") {
        return before ? area.left < left - EPSILON : area.left + area.width > left + EPSILON;
      }
      return before ? area.top < top - EPSILON : area.top + area.height > top + EPSILON;
    });
  const has = (quadrant: CornerQuadrant) => {
    const [vertical, horizontal] = quadrant.split("-");
    return reaches("rows", horizontal === "left") && reaches("columns", vertical === "top");
  };
  const present = new Set(QUADRANTS.filter(has));
  return [...present].map((quadrant) => {
    const [vertical, horizontal] = quadrant.split("-");
    const otherVertical = vertical === "top" ? "bottom" : "top";
    const otherHorizontal = horizontal === "left" ? "right" : "left";
    const fullHeight = !present.has(`${otherVertical}-${horizontal}` as CornerQuadrant);
    const fullWidth = !present.has(`${vertical}-${otherHorizontal}` as CornerQuadrant);
    return {
      quadrant,
      // A pane's top-left or bottom-right corner drags along the nwse diagonal.
      cursor:
        quadrant === "top-left" || quadrant === "bottom-right" ? "nwse-resize" : "nesw-resize",
      left: fullWidth || horizontal === "left" ? 0 : 50,
      top: fullHeight || vertical === "top" ? 0 : 50,
      width: fullWidth ? 100 : 50,
      height: fullHeight ? 100 : 50,
    };
  });
}

/**
 * A corner held by a drag, where its dividers are now: the same key and dividers until release,
 * though its junction may have split in two or merged with another meanwhile. Null once one of
 * its dividers is gone, as when a pane closed.
 */
export function followCorner(
  corner: DividerCorner,
  dividers: readonly TerminalDivider[],
): DividerCorner | null {
  const byPath = new Map(dividers.map((divider) => [divider.path.join("."), divider]));
  const held: TerminalDivider[] = [];
  for (const divider of corner.dividers) {
    const now = byPath.get(divider.path.join("."));
    if (!now || now.axis !== divider.axis) return null;
    held.push(now);
  }
  const column = held.find((divider) => divider.axis === "columns");
  const row = held.find((divider) => divider.axis === "rows");
  if (!column || !row) return null;
  return { ...corner, dividers: held, left: linePosition(column), top: linePosition(row) };
}

/**
 * `layout` with each resize applied in turn. A resize naming the ratio its divider already has
 * leaves it exactly there: a refused move keeps a proportional split's ratio past the resize
 * limits, which clamping would change.
 */
export function applyDividerResizes(
  layout: TerminalLayout,
  resizes: readonly DividerResize[],
): TerminalLayout {
  return resizes.reduce(
    (current, { path, ratio }) =>
      dividerAt(current, path)?.ratio === ratio
        ? current
        : resizeTerminalSplit(current, path, ratio),
    layout,
  );
}

function dividerAt(layout: TerminalLayout, path: readonly number[]): TerminalDivider | undefined {
  const target = path.join(".");
  return terminalLayoutGeometry(layout).dividers.find(
    (divider) => divider.path.join(".") === target,
  );
}

/**
 * Where a corner's dividers go for the pointer at `point` (percent of the view), outer splits
 * first, so a nested divider follows its parent's move. A move `keeps` refuses, as one leaving a
 * pane too small, keeps that divider where `layout` has it: the other way still follows.
 */
export function cornerDragResizes(
  layout: TerminalLayout,
  corner: Pick<DividerCorner, "dividers">,
  point: { left: number; top: number },
  keeps: (before: TerminalLayout, after: TerminalLayout) => boolean,
): DividerResize[] {
  const resizes: DividerResize[] = [];
  let current = layout;
  for (const { path } of corner.dividers) {
    const divider = dividerAt(current, path);
    if (!divider) continue;
    const { axis, area } = divider;
    const ratio =
      axis === "columns"
        ? (point.left - area.left) / area.width
        : (point.top - area.top) / area.height;
    const next = resizeTerminalSplit(current, path, ratio);
    if (keeps(current, next)) {
      resizes.push({ path, ratio });
      current = next;
    } else {
      resizes.push({ path, ratio: divider.ratio });
    }
  }
  return resizes;
}

/**
 * One pointer drag on a divider or a corner, from press to release. Its sizes show only while it
 * lives, and are saved only by its release.
 */
/** A divider a drag holds: where it is, and the panes on either side of it when taken. */
export interface HeldDivider extends Pick<TerminalDivider, "path" | "axis"> {
  sides: readonly [readonly string[], readonly string[]];
}

export interface DividerDragSession {
  /** The dividers the drag took: one, or a corner's. */
  held: readonly HeldDivider[];
  /** The corner it took, kept as taken; null for a single divider. */
  corner: DividerCorner | null;
  /** Where the drag has its dividers; null before the pointer moved. */
  resizes: readonly DividerResize[] | null;
}

/** The panes on either side of the split at `path` in `layout`, or null with no split there. */
function splitSides(
  layout: TerminalLayout,
  path: readonly number[],
): { axis: TerminalDivider["axis"]; sides: HeldDivider["sides"] } | null {
  let node = layout;
  for (const index of path) {
    if (typeof node === "string") return null;
    node = node.children[index];
  }
  if (typeof node === "string") return null;
  const [first, second] = node.children;
  return { axis: node.axis, sides: [terminalLayoutKeys(first), terminalLayoutKeys(second)] };
}

/** A drag in `layout` taking `corner`, or the one divider `divider`. */
export function dividerDragSession(
  taken: DividerCorner | TerminalDivider,
  layout: TerminalLayout,
): DividerDragSession {
  const corner = "dividers" in taken ? taken : null;
  const held = (corner ? corner.dividers : [taken as TerminalDivider]).map(({ path, axis }) => ({
    path,
    axis,
    sides: splitSides(layout, path)?.sides ?? [[], []],
  }));
  return { held, corner, resizes: null };
}

const shareAPane = (before: readonly string[], now: readonly string[]) =>
  before.some((key) => now.includes(key));

/**
 * Whether every divider the drag holds is still in `layout`, the same way between the same
 * panes. A split another took the place of, as when a pane beside it closed, ends the drag.
 */
export function holdsItsDividers(session: DividerDragSession, layout: TerminalLayout): boolean {
  return session.held.every((held) => {
    const now = splitSides(layout, held.path);
    return (
      now !== null &&
      now.axis === held.axis &&
      shareAPane(held.sides[0], now.sides[0]) &&
      shareAPane(held.sides[1], now.sides[1])
    );
  });
}
