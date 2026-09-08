// Persisted card positions for the Canvas page. The view itself is not saved:
// every canvas opens fitted to its cards.
//
// One localStorage entry per server (keyed by the server identity, so an
// embedded host that proxies several backends keeps their layouts apart).
// Reads never throw — a missing, malformed, or older-version entry reads as
// an empty layout. Writes may throw (quota); callers surface that as a warning.

import { getOmnigentServerIdentity } from "@/lib/host";
import type { CanvasPosition, CanvasPositions } from "./canvasLayout";

export const LAYOUT_VERSION = 1;
/** Hard cap on remembered card spots; the oldest entries beyond it are dropped. */
export const MAX_SAVED_POSITIONS = 5_000;
const MAX_ABS_COORDINATE = 1_000_000;
const STORAGE_KEY_PREFIX = "omnigent:canvas-layout";

export interface CanvasLayout {
  positions: CanvasPositions;
}

interface StoredLayout {
  version: number;
  positions: Record<string, [x: number, y: number]>;
}

export const EMPTY_CANVAS_LAYOUT: CanvasLayout = { positions: {} };

export function canvasLayoutStorageKey(): string {
  return `${STORAGE_KEY_PREFIX}:${getOmnigentServerIdentity() ?? "default"}`;
}

function boundedCoordinate(value: number): number {
  return Math.max(-MAX_ABS_COORDINATE, Math.min(MAX_ABS_COORDINATE, Math.round(value)));
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function parsePositions(value: unknown): CanvasPositions {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const positions: CanvasPositions = {};
  for (const [id, entry] of Object.entries(value as Record<string, unknown>)) {
    if (!Array.isArray(entry) || entry.length !== 2 || !finite(entry[0]) || !finite(entry[1])) {
      continue;
    }
    positions[id] = { x: boundedCoordinate(entry[0]), y: boundedCoordinate(entry[1]) };
  }
  return positions;
}

export function readCanvasLayout(): CanvasLayout {
  if (typeof window === "undefined") return EMPTY_CANVAS_LAYOUT;
  try {
    const raw = window.localStorage.getItem(canvasLayoutStorageKey());
    if (!raw) return EMPTY_CANVAS_LAYOUT;
    const parsed = JSON.parse(raw) as Partial<StoredLayout> | null;
    if (!parsed || typeof parsed !== "object" || parsed.version !== LAYOUT_VERSION) {
      return EMPTY_CANVAS_LAYOUT;
    }
    return { positions: parsePositions(parsed.positions) };
  } catch {
    return EMPTY_CANVAS_LAYOUT;
  }
}

/** Persist the layout. Throws when localStorage is unavailable or full. */
export function writeCanvasLayout(layout: CanvasLayout): void {
  const entries = Object.entries(layout.positions).slice(-MAX_SAVED_POSITIONS);
  const stored: StoredLayout = {
    version: LAYOUT_VERSION,
    positions: Object.fromEntries(
      entries.map(([id, position]) => [
        id,
        [boundedCoordinate(position.x), boundedCoordinate(position.y)],
      ]),
    ),
  };
  window.localStorage.setItem(canvasLayoutStorageKey(), JSON.stringify(stored));
}

export function withPosition(
  layout: CanvasLayout,
  sessionId: string,
  position: CanvasPosition,
): CanvasLayout {
  // Re-inserting moves the id to the end, so the cap evicts the least recently placed cards.
  const positions = Object.fromEntries(
    Object.entries(layout.positions).filter(([id]) => id !== sessionId),
  ) as CanvasPositions;
  positions[sessionId] = { x: boundedCoordinate(position.x), y: boundedCoordinate(position.y) };
  return { ...layout, positions };
}

export function withPositions(layout: CanvasLayout, positions: CanvasPositions): CanvasLayout {
  return { ...layout, positions };
}

/** Forget the given cards' spots (one canvas's cards); other canvases keep theirs. */
export function withoutPositions(layout: CanvasLayout, sessionIds: Iterable<string>): CanvasLayout {
  const removed = new Set(sessionIds);
  return {
    positions: Object.fromEntries(
      Object.entries(layout.positions).filter(([id]) => !removed.has(id)),
    ) as CanvasPositions,
  };
}
