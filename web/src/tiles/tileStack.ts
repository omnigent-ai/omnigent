// The Stack: tiles you parked, plus quiet ones (idle, not waiting on you, and
// untouched for a day) unless you chose to keep them out. Stacked tiles leave
// the grid and give up their live stream. Saved per device by session id.

import type { Conversation } from "@/hooks/useConversations";
import { createLocalJsonStore } from "./localJsonStore";
import { needsYou } from "./tileSelection";

export const QUIET_STACK_AFTER_MS = 24 * 60 * 60 * 1000;

export interface TileStackState {
  /** Parked by the user. */
  parked: readonly string[];
  /** Quiet tiles the user brought back; never auto-stacked. */
  kept: readonly string[];
}

const EMPTY: TileStackState = { parked: [], kept: [] };

type StackRow = Pick<Conversation, "id" | "status" | "updated_at" | "pending_elicitations_count">;

/** Idle, not waiting on you, and untouched for `quietAfterMs`. */
export function isQuiet(row: StackRow, nowMs: number, quietAfterMs = QUIET_STACK_AFTER_MS) {
  return row.status !== "running" && !needsYou(row) && nowMs - row.updated_at * 1000 > quietAfterMs;
}

/** Split tiles into those on the grid and those in the Stack, keeping order. */
export function partitionStack<T extends StackRow>(
  rows: readonly T[],
  state: TileStackState,
  nowMs: number,
): { onGrid: T[]; stacked: T[] } {
  const parked = new Set(state.parked);
  const kept = new Set(state.kept);
  const onGrid: T[] = [];
  const stacked: T[] = [];
  for (const row of rows) {
    // An agent waiting on you always surfaces, even if parked.
    const stack =
      !needsYou(row) && (parked.has(row.id) || (isQuiet(row, nowMs) && !kept.has(row.id)));
    (stack ? stacked : onGrid).push(row);
  }
  return { onGrid, stacked };
}

export function parkTile(state: TileStackState, id: string): TileStackState {
  return {
    parked: state.parked.includes(id) ? state.parked : [...state.parked, id],
    kept: state.kept.filter((keptId) => keptId !== id),
  };
}

/** Bring a tile back; a quiet one is also kept out of the Stack from now on. */
export function unstackTile(state: TileStackState, id: string, quiet: boolean): TileStackState {
  return {
    parked: state.parked.filter((parkedId) => parkedId !== id),
    kept: quiet && !state.kept.includes(id) ? [...state.kept, id] : state.kept,
  };
}

/** Drop ids that are no longer sessions, so the saved state stays small. */
export function pruneStack(state: TileStackState, liveIds: ReadonlySet<string>): TileStackState {
  const keep = (id: string) => liveIds.has(id);
  return { parked: state.parked.filter(keep), kept: state.kept.filter(keep) };
}

// ── Persistence ───────────────────────────────────────────────────────────────

const ids = (list: unknown) =>
  Array.isArray(list) ? list.filter((id): id is string => typeof id === "string") : [];

const stackStore = createLocalJsonStore<TileStackState>("omnigent:tiles-stack", EMPTY, (raw) => {
  const parsed = raw as Partial<TileStackState> | null;
  return { parked: ids(parsed?.parked), kept: ids(parsed?.kept) };
});

export const writeTileStack = stackStore.write;
export const useTileStack = stackStore.use;
