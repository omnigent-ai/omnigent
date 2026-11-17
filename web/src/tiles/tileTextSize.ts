// Per-tile text size, applied as a zoom on the tile's body so the chat's
// rem-based type and spacing scale together.

import { createLocalJsonStore } from "./localJsonStore";

export const TILE_TEXT_SIZES = [0.85, 1, 1.15, 1.3] as const;
export const DEFAULT_TILE_TEXT_SIZE = 1;

const sizeStore = createLocalJsonStore<Record<string, number>>(
  "omnigent:tiles-text-size",
  {},
  (raw) =>
    Object.fromEntries(
      Object.entries((raw as Record<string, unknown>) ?? {}).filter(
        (entry): entry is [string, number] =>
          typeof entry[1] === "number" && (TILE_TEXT_SIZES as readonly number[]).includes(entry[1]),
      ),
    ),
);

export function useTileTextSize(sessionId: string): number {
  return sizeStore.use()[sessionId] ?? DEFAULT_TILE_TEXT_SIZE;
}

/** The next size `step` places away, clamped to the scale. */
export function stepTextSize(size: number, step: number): number {
  const index = Math.max(0, TILE_TEXT_SIZES.indexOf(size as (typeof TILE_TEXT_SIZES)[number]));
  return TILE_TEXT_SIZES[Math.min(Math.max(index + step, 0), TILE_TEXT_SIZES.length - 1)];
}

export function setTileTextSize(sessionId: string, size: number): void {
  const others = Object.fromEntries(
    Object.entries(sizeStore.read()).filter(([id]) => id !== sessionId),
  );
  sizeStore.write(size === DEFAULT_TILE_TEXT_SIZE ? others : { ...others, [sessionId]: size });
}
