// Per-tile colors: twelve named hues, or Auto (a stable hue derived from the
// session id). Used for the header stripe, header tint and border.

import { createLocalJsonStore } from "./localJsonStore";

export interface TileHue {
  name: string;
  hue: number;
}

export const TILE_HUES: readonly TileHue[] = [
  { name: "Red", hue: 0 },
  { name: "Orange", hue: 24 },
  { name: "Amber", hue: 40 },
  { name: "Lime", hue: 90 },
  { name: "Green", hue: 142 },
  { name: "Teal", hue: 172 },
  { name: "Cyan", hue: 192 },
  { name: "Blue", hue: 214 },
  { name: "Indigo", hue: 240 },
  { name: "Violet", hue: 268 },
  { name: "Purple", hue: 292 },
  { name: "Pink", hue: 330 },
];

/** A stable palette hue for a session that has no chosen color. */
export function autoHue(sessionId: string): number {
  let hash = 0;
  for (const char of sessionId) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return TILE_HUES[hash % TILE_HUES.length].hue;
}

export interface TileColor {
  hue: number;
  /** Whether the user picked it (false means Auto). */
  picked: boolean;
  solid: string;
  tint: string;
  border: string;
}

export function tileColor(hue: number, picked: boolean): TileColor {
  return {
    hue,
    picked,
    solid: `hsl(${hue} 70% 52%)`,
    tint: `hsl(${hue} 70% 52% / 0.10)`,
    border: `hsl(${hue} 55% 50% / 0.45)`,
  };
}

const validHues = new Set(TILE_HUES.map((entry) => entry.hue));

const colorStore = createLocalJsonStore<Record<string, number>>(
  "omnigent:tiles-colors",
  {},
  (raw) =>
    Object.fromEntries(
      Object.entries((raw as Record<string, unknown>) ?? {}).filter(
        (entry): entry is [string, number] =>
          typeof entry[1] === "number" && validHues.has(entry[1]),
      ),
    ),
);

/** The session's color, picked or Auto. */
export function useTileColor(sessionId: string): TileColor {
  const picked = colorStore.use()[sessionId];
  return picked === undefined ? tileColor(autoHue(sessionId), false) : tileColor(picked, true);
}

/** Pick a hue for a session, or `null` to return it to Auto. */
export function setTileHue(sessionId: string, hue: number | null): void {
  const others = Object.fromEntries(
    Object.entries(colorStore.read()).filter(([id]) => id !== sessionId),
  );
  colorStore.write(hue === null ? others : { ...others, [sessionId]: hue });
}
