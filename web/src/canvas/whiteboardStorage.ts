// Persisted whiteboard drawings, one entry per server, viewer and canvas, like
// the card layout in `canvasStorage.ts`: the server never sees the drawing.
// Reads never throw — a missing, malformed or other-version entry reads as an
// empty board, and malformed elements are dropped. Writes may throw (quota);
// callers surface that as a warning.

import { getOmnigentServerIdentity } from "@/lib/host";
import {
  MAX_ELEMENTS,
  MAX_STROKE_POINTS,
  MAX_TEXT_LENGTH,
  type ArrowEnd,
  type WhiteboardElement,
  type WhiteboardPoint,
} from "./whiteboardScene";

export const WHITEBOARD_VERSION = 1;
const STORAGE_KEY_PREFIX = "omnigent:canvas-whiteboard";
const MAX_ABS_COORDINATE = 1_000_000;
const MAX_SIZE = 100_000;
const MAX_ID_LENGTH = 200;

type StoredEnd = [x: number, y: number] | { bind: string };

type StoredElement =
  | { id: string; kind: "stroke"; points: number[] }
  | { id: string; kind: "rectangle" | "note"; box: number[]; text: string }
  | { id: string; kind: "arrow"; from: StoredEnd; to: StoredEnd };

interface StoredWhiteboard {
  version: number;
  elements: StoredElement[];
}

export function whiteboardStorageKey(viewerId: string | null, canvasId: string): string {
  return `${STORAGE_KEY_PREFIX}:${getOmnigentServerIdentity() ?? "default"}:${viewerId ?? "anonymous"}:${canvasId}`;
}

function coordinate(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return Math.max(-MAX_ABS_COORDINATE, Math.min(MAX_ABS_COORDINATE, Math.round(value)));
}

function size(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return null;
  return Math.min(MAX_SIZE, Math.round(value));
}

function validId(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= MAX_ID_LENGTH;
}

function parseEnd(value: unknown): ArrowEnd | null {
  if (Array.isArray(value)) {
    const x = coordinate(value[0]);
    const y = coordinate(value[1]);
    return value.length === 2 && x !== null && y !== null ? { x, y } : null;
  }
  if (value && typeof value === "object" && validId((value as { bind?: unknown }).bind)) {
    return { bind: (value as { bind: string }).bind };
  }
  return null;
}

function parsePoints(value: unknown): WhiteboardPoint[] | null {
  if (!Array.isArray(value) || value.length < 2 || value.length % 2 !== 0) return null;
  const points: WhiteboardPoint[] = [];
  for (let index = 0; index < value.length && points.length < MAX_STROKE_POINTS; index += 2) {
    const x = coordinate(value[index]);
    const y = coordinate(value[index + 1]);
    if (x === null || y === null) return null;
    points.push({ x, y });
  }
  return points;
}

function parseElement(value: unknown): WhiteboardElement | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  if (!validId(raw.id)) return null;
  switch (raw.kind) {
    case "stroke": {
      const points = parsePoints(raw.points);
      return points ? { id: raw.id, kind: "stroke", points } : null;
    }
    case "rectangle":
    case "note": {
      if (!Array.isArray(raw.box) || raw.box.length !== 4) return null;
      const [x, y, width, height] = [
        coordinate(raw.box[0]),
        coordinate(raw.box[1]),
        size(raw.box[2]),
        size(raw.box[3]),
      ];
      if (x === null || y === null || width === null || height === null) return null;
      const text = typeof raw.text === "string" ? raw.text.slice(0, MAX_TEXT_LENGTH) : "";
      return { id: raw.id, kind: raw.kind, x, y, width, height, text };
    }
    case "arrow": {
      const from = parseEnd(raw.from);
      const to = parseEnd(raw.to);
      return from && to ? { id: raw.id, kind: "arrow", from, to } : null;
    }
    default:
      return null;
  }
}

export function readWhiteboard(viewerId: string | null, canvasId: string): WhiteboardElement[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(whiteboardStorageKey(viewerId, canvasId));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as Partial<StoredWhiteboard> | null;
    if (!parsed || parsed.version !== WHITEBOARD_VERSION || !Array.isArray(parsed.elements)) {
      return [];
    }
    const seen = new Set<string>();
    const elements: WhiteboardElement[] = [];
    for (const entry of parsed.elements.slice(-MAX_ELEMENTS)) {
      const element = parseElement(entry);
      if (!element || seen.has(element.id)) continue;
      seen.add(element.id);
      elements.push(element);
    }
    return elements;
  } catch {
    return [];
  }
}

function storeEnd(end: ArrowEnd): StoredEnd {
  return "bind" in end ? { bind: end.bind } : [Math.round(end.x), Math.round(end.y)];
}

function storeElement(element: WhiteboardElement): StoredElement {
  switch (element.kind) {
    case "stroke":
      return {
        id: element.id,
        kind: "stroke",
        points: element.points
          .slice(0, MAX_STROKE_POINTS)
          .flatMap((point) => [Math.round(point.x), Math.round(point.y)]),
      };
    case "arrow":
      return {
        id: element.id,
        kind: "arrow",
        from: storeEnd(element.from),
        to: storeEnd(element.to),
      };
    default:
      return {
        id: element.id,
        kind: element.kind,
        box: [element.x, element.y, element.width, element.height].map(Math.round),
        text: element.text.slice(0, MAX_TEXT_LENGTH),
      };
  }
}

/** Persist the drawing; an empty board removes the entry. Throws when storage is unavailable or full. */
export function writeWhiteboard(
  elements: readonly WhiteboardElement[],
  viewerId: string | null,
  canvasId: string,
): void {
  const key = whiteboardStorageKey(viewerId, canvasId);
  if (elements.length === 0) {
    window.localStorage.removeItem(key);
    return;
  }
  const stored: StoredWhiteboard = {
    version: WHITEBOARD_VERSION,
    elements: elements.slice(-MAX_ELEMENTS).map(storeElement),
  };
  window.localStorage.setItem(key, JSON.stringify(stored));
}
