// Pure model for the Canvas whiteboard: freehand strokes, frame rectangles,
// sticky notes and arrows, all in React Flow's flow coordinates so they pan and
// zoom with the session cards. An arrow end can bind to a card or a shape and
// then follows it when either moves.

import { nanoid } from "nanoid";
import { GRID_STEP } from "./canvasLayout";

export type WhiteboardTool = "select" | "pen" | "rectangle" | "note" | "arrow";

export interface WhiteboardPoint {
  x: number;
  y: number;
}

export interface WhiteboardBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface StrokeElement {
  id: string;
  kind: "stroke";
  points: WhiteboardPoint[];
}

/** A frame around a group of cards; its label sits above the top-left corner. */
export interface RectangleElement extends WhiteboardBox {
  id: string;
  kind: "rectangle";
  text: string;
}

export interface NoteElement extends WhiteboardBox {
  id: string;
  kind: "note";
  text: string;
}

/** A free point, or the id of a session card or shape the end follows. */
export type ArrowEnd = WhiteboardPoint | { bind: string };

export interface ArrowElement {
  id: string;
  kind: "arrow";
  from: ArrowEnd;
  to: ArrowEnd;
}

export type WhiteboardElement = StrokeElement | RectangleElement | NoteElement | ArrowElement;
export type BoxedElement = RectangleElement | NoteElement;

/** Looks up the current box of a card or shape, or null when it is not on this canvas. */
export type BoxResolver = (id: string) => WhiteboardBox | null;

export const NOTE_WIDTH = GRID_STEP * 6;
export const NOTE_HEIGHT = GRID_STEP * 4;
export const DEFAULT_RECTANGLE_WIDTH = GRID_STEP * 10;
export const DEFAULT_RECTANGLE_HEIGHT = GRID_STEP * 6;
/** Smaller drags are treated as clicks and get the default size. */
export const MIN_DRAG_SIZE = 12;
export const MAX_ELEMENTS = 1_000;
export const MAX_STROKE_POINTS = 2_000;
export const MAX_TEXT_LENGTH = 2_000;
const MIN_POINT_SPACING = 2;

export function newWhiteboardElementId(): string {
  return `wb-${nanoid(12)}`;
}

export function isBoxed(element: WhiteboardElement): element is BoxedElement {
  return element.kind === "rectangle" || element.kind === "note";
}

export function isBoundEnd(end: ArrowEnd): end is { bind: string } {
  return "bind" in end;
}

export function boxFromCorners(a: WhiteboardPoint, b: WhiteboardPoint): WhiteboardBox {
  return {
    x: Math.min(a.x, b.x),
    y: Math.min(a.y, b.y),
    width: Math.abs(b.x - a.x),
    height: Math.abs(b.y - a.y),
  };
}

/** A dragged frame, or a default-sized one at the click when the drag was tiny. */
export function rectangleBox(anchor: WhiteboardPoint, end: WhiteboardPoint): WhiteboardBox {
  const box = boxFromCorners(anchor, end);
  if (box.width >= MIN_DRAG_SIZE && box.height >= MIN_DRAG_SIZE) return box;
  return { ...anchor, width: DEFAULT_RECTANGLE_WIDTH, height: DEFAULT_RECTANGLE_HEIGHT };
}

/** A note centered on the click. */
export function noteBoxAt(point: WhiteboardPoint): WhiteboardBox {
  return {
    x: Math.round(point.x - NOTE_WIDTH / 2),
    y: Math.round(point.y - NOTE_HEIGHT / 2),
    width: NOTE_WIDTH,
    height: NOTE_HEIGHT,
  };
}

/** Append a pen sample, skipping jitter closer than a few units and capping the length. */
export function appendStrokePoint(
  points: readonly WhiteboardPoint[],
  point: WhiteboardPoint,
): WhiteboardPoint[] {
  const last = points[points.length - 1];
  if (points.length >= MAX_STROKE_POINTS) return [...points];
  if (last && Math.hypot(point.x - last.x, point.y - last.y) < MIN_POINT_SPACING) {
    return [...points];
  }
  return [...points, { x: Math.round(point.x), y: Math.round(point.y) }];
}

/** A smoothed SVG path through the samples (quadratic curves between midpoints). */
export function strokePath(points: readonly WhiteboardPoint[]): string {
  if (points.length === 0) return "";
  const [first] = points;
  if (points.length === 1) return `M ${first.x} ${first.y} l 0.01 0`;
  let path = `M ${first.x} ${first.y}`;
  for (let index = 1; index < points.length - 1; index += 1) {
    const point = points[index];
    const next = points[index + 1];
    path += ` Q ${point.x} ${point.y} ${(point.x + next.x) / 2} ${(point.y + next.y) / 2}`;
  }
  const last = points[points.length - 1];
  return `${path} L ${last.x} ${last.y}`;
}

function center(box: WhiteboardBox): WhiteboardPoint {
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

/** Where the segment from the box's center toward ``toward`` crosses the box's border. */
export function edgePoint(box: WhiteboardBox, toward: WhiteboardPoint): WhiteboardPoint {
  const middle = center(box);
  const dx = toward.x - middle.x;
  const dy = toward.y - middle.y;
  if (dx === 0 && dy === 0) return middle;
  const scaleX = dx === 0 ? Infinity : box.width / 2 / Math.abs(dx);
  const scaleY = dy === 0 ? Infinity : box.height / 2 / Math.abs(dy);
  const scale = Math.min(scaleX, scaleY, 1);
  return { x: middle.x + dx * scale, y: middle.y + dy * scale };
}

/** The arrow's drawn endpoints, or null when a bound card or shape is not on this canvas. */
export function resolveArrow(
  arrow: Pick<ArrowElement, "from" | "to">,
  resolveBox: BoxResolver,
): { start: WhiteboardPoint; end: WhiteboardPoint } | null {
  const fromBox = isBoundEnd(arrow.from) ? resolveBox(arrow.from.bind) : null;
  const toBox = isBoundEnd(arrow.to) ? resolveBox(arrow.to.bind) : null;
  if ((isBoundEnd(arrow.from) && !fromBox) || (isBoundEnd(arrow.to) && !toBox)) return null;
  const fromAnchor = fromBox ? center(fromBox) : (arrow.from as WhiteboardPoint);
  const toAnchor = toBox ? center(toBox) : (arrow.to as WhiteboardPoint);
  return {
    start: fromBox ? edgePoint(fromBox, toAnchor) : fromAnchor,
    end: toBox ? edgePoint(toBox, fromAnchor) : toAnchor,
  };
}

/** The topmost box under the point; later entries are drawn above earlier ones. */
export function boxAt(
  point: WhiteboardPoint,
  boxes: readonly (readonly [id: string, box: WhiteboardBox])[],
): string | null {
  for (let index = boxes.length - 1; index >= 0; index -= 1) {
    const [id, box] = boxes[index];
    if (
      point.x >= box.x &&
      point.x <= box.x + box.width &&
      point.y >= box.y &&
      point.y <= box.y + box.height
    ) {
      return id;
    }
  }
  return null;
}

function movePoint(point: WhiteboardPoint, dx: number, dy: number): WhiteboardPoint {
  return { x: Math.round(point.x + dx), y: Math.round(point.y + dy) };
}

/** Shift an element; bound arrow ends stay attached to their card or shape. */
export function moveElement<T extends WhiteboardElement>(element: T, dx: number, dy: number): T {
  switch (element.kind) {
    case "stroke":
      return { ...element, points: element.points.map((point) => movePoint(point, dx, dy)) };
    case "arrow":
      return {
        ...element,
        from: isBoundEnd(element.from) ? element.from : movePoint(element.from, dx, dy),
        to: isBoundEnd(element.to) ? element.to : movePoint(element.to, dx, dy),
      };
    default:
      return { ...element, ...movePoint(element, dx, dy) };
  }
}

/** Frames and notes sit on the cards' lattice; strokes and arrows are left where they are. */
export function snapToGrid<T extends WhiteboardElement>(element: T): T {
  if (!isBoxed(element)) return element;
  return {
    ...element,
    x: Math.round(element.x / GRID_STEP) * GRID_STEP,
    y: Math.round(element.y / GRID_STEP) * GRID_STEP,
  };
}

export function withText(
  elements: readonly WhiteboardElement[],
  id: string,
  text: string,
): WhiteboardElement[] {
  const value = text.slice(0, MAX_TEXT_LENGTH);
  return elements.map((element) =>
    element.id === id && isBoxed(element) ? { ...element, text: value } : element,
  );
}

/**
 * Remove an element. An arrow bound to a removed shape keeps that end where
 * it touched the shape, so deleting a frame never deletes the arrows pointing
 * at it. The other end stays bound even while its card is not loaded yet.
 */
export function removeElement(
  elements: readonly WhiteboardElement[],
  id: string,
  resolveBox: BoxResolver,
): WhiteboardElement[] {
  const removed = elements.find((element) => element.id === id);
  const remaining = elements.filter((element) => element.id !== id);
  if (!removed || !isBoxed(removed)) return remaining;
  return remaining.flatMap((element): WhiteboardElement[] => {
    if (element.kind !== "arrow") return [element];
    const fromRemoved = isBoundEnd(element.from) && element.from.bind === id;
    const toRemoved = isBoundEnd(element.to) && element.to.bind === id;
    if (!fromRemoved && !toRemoved) return [element];
    // Both ends on the removed shape would leave a zero-length arrow.
    if (fromRemoved && toRemoved) return [];
    const other = fromRemoved ? element.to : element.from;
    const otherBox = isBoundEnd(other) ? resolveBox(other.bind) : null;
    const otherAnchor = isBoundEnd(other) ? otherBox && center(otherBox) : other;
    const loose = movePoint(otherAnchor ? edgePoint(removed, otherAnchor) : center(removed), 0, 0);
    return [
      {
        ...element,
        from: fromRemoved ? loose : element.from,
        to: toRemoved ? loose : element.to,
      },
    ];
  });
}

/** Append a new element, or null when the board is full; existing drawings are never evicted. */
export function addElement(
  elements: readonly WhiteboardElement[],
  element: WhiteboardElement,
): WhiteboardElement[] | null {
  return elements.length < MAX_ELEMENTS ? [...elements, element] : null;
}

/**
 * Apply this tab's changes since ``base`` onto ``stored``, which another tab
 * may have written meanwhile. Elements added or edited here replace stored
 * ones by id, elements deleted here are dropped, and everything else stored is
 * kept in order. New elements that no longer fit under the cap are left out.
 */
export function mergeBoards(
  stored: readonly WhiteboardElement[],
  base: readonly WhiteboardElement[],
  local: readonly WhiteboardElement[],
): WhiteboardElement[] {
  const baseVersions = new Map(base.map((element) => [element.id, JSON.stringify(element)]));
  const localIds = new Set(local.map((element) => element.id));
  const changed = new Map(
    local
      .filter((element) => baseVersions.get(element.id) !== JSON.stringify(element))
      .map((element) => [element.id, element]),
  );
  const merged = stored
    .filter((element) => !baseVersions.has(element.id) || localIds.has(element.id))
    .map((element) => changed.get(element.id) ?? element);
  const present = new Set(merged.map((element) => element.id));
  for (const element of changed.values()) {
    if (present.has(element.id)) continue;
    if (merged.length >= MAX_ELEMENTS) break;
    merged.push(element);
  }
  return merged;
}
