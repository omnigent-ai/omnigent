// Gallery layout: per-session tile spans, the column choice, and the slot math
// behind drag-to-resize. Preferences persist per browser in localStorage.

import { useSyncExternalStore } from "react";

export interface TileSpan {
  columns: number;
  rows: number;
}

export const DEFAULT_SPAN: TileSpan = { columns: 1, rows: 1 };
export const MAX_SPAN: TileSpan = { columns: 4, rows: 3 };
export const MAX_COLUMNS = 8;
export const TILE_MIN_WIDTH_PX = 360;
export const TILE_ROW_HEIGHT_PX = 320;
export const TILE_GAP_PX = 12;

/** Round to whole slots and keep within 1…max (and within the columns shown). */
export function clampSpan(span: TileSpan, visibleColumns = MAX_SPAN.columns): TileSpan {
  const limit = (value: number, max: number) =>
    Math.max(1, Math.min(max, Math.round(Number.isFinite(value) ? value : 1)));
  return {
    columns: limit(span.columns, Math.min(MAX_SPAN.columns, Math.max(1, visibleColumns))),
    rows: limit(span.rows, MAX_SPAN.rows),
  };
}

/** Columns that fit a container width (at least one). */
export function fittingColumns(
  containerWidthPx: number,
  minTileWidthPx = TILE_MIN_WIDTH_PX,
): number {
  return Math.max(1, Math.floor((containerWidthPx + TILE_GAP_PX) / (minTileWidthPx + TILE_GAP_PX)));
}

/** The chosen column count, capped by what fits; 0 ("Auto") means fit the window. */
export function effectiveColumns(chosen: number, containerWidthPx: number): number {
  const fit = fittingColumns(containerWidthPx);
  return chosen > 0 ? Math.min(chosen, fit) : fit;
}

/**
 * The span a drag produces: the tile's start size plus the pointer delta,
 * converted to whole slots of `slotWidthPx` × `TILE_ROW_HEIGHT_PX`.
 */
export function spanFromDrag(
  startWidthPx: number,
  startHeightPx: number,
  deltaXPx: number,
  deltaYPx: number,
  slotWidthPx: number,
  visibleColumns: number,
): TileSpan {
  return clampSpan(
    {
      columns: (startWidthPx + deltaXPx + TILE_GAP_PX) / (slotWidthPx + TILE_GAP_PX),
      rows: (startHeightPx + deltaYPx + TILE_GAP_PX) / (TILE_ROW_HEIGHT_PX + TILE_GAP_PX),
    },
    visibleColumns,
  );
}

// ── Persistence ───────────────────────────────────────────────────────────────

const SPANS_KEY = "omnigent:tiles-spans";
const COLUMNS_KEY = "omnigent:tiles-columns";
const listeners = new Set<() => void>();

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage unavailable: the layout still works for this page view.
  }
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  // Moving the window to another display resizes it; re-read that display's choice.
  window.addEventListener("resize", listener);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("resize", listener);
  };
}

let spansCache: { raw: string | null; value: Record<string, TileSpan> } = { raw: null, value: {} };
function readSpans(): Record<string, TileSpan> {
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(SPANS_KEY);
  } catch {
    // fall through with no saved spans
  }
  if (raw !== spansCache.raw) spansCache = { raw, value: readJson(SPANS_KEY, {}) };
  return spansCache.value;
}

/** A session's saved span (keyed by session id, so it survives restarts). */
export function useTileSpan(sessionId: string): TileSpan {
  const spans = useSyncExternalStore(subscribe, readSpans);
  const saved = spans[sessionId];
  return saved ? clampSpan(saved) : DEFAULT_SPAN;
}

export function setTileSpan(sessionId: string, span: TileSpan): void {
  const clamped = clampSpan(span);
  const others = Object.fromEntries(
    Object.entries(readSpans()).filter(([savedId]) => savedId !== sessionId),
  );
  const isDefault = clamped.columns === 1 && clamped.rows === 1;
  writeJson(SPANS_KEY, isDefault ? others : { ...others, [sessionId]: clamped });
}

/** Column choices are remembered per display, so a laptop and a large monitor keep their own. */
export function displayKey(): string {
  if (typeof window === "undefined" || !window.screen) return "default";
  return `${window.screen.width}x${window.screen.height}`;
}

function validColumns(value: unknown): number | null {
  return Number.isInteger(value) && (value as number) >= 0 && (value as number) <= MAX_COLUMNS
    ? (value as number)
    : null;
}

function readColumns(): number {
  const stored = readJson<{ byDisplay?: Record<string, number>; last?: number }>(COLUMNS_KEY, {});
  return validColumns(stored.byDisplay?.[displayKey()]) ?? validColumns(stored.last) ?? 0;
}

/** Chosen column count for this display; 0 = Auto. */
export function useTileColumns(): number {
  return useSyncExternalStore(subscribe, readColumns);
}

export function setTileColumns(columns: number): void {
  const value = Math.max(0, Math.min(MAX_COLUMNS, Math.round(columns)));
  const stored = readJson<{ byDisplay?: Record<string, number> }>(COLUMNS_KEY, {});
  writeJson(COLUMNS_KEY, {
    byDisplay: { ...stored.byDisplay, [displayKey()]: value },
    last: value,
  });
}
