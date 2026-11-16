// The Stack: tiles you parked, plus quiet ones (idle, not waiting on you, and
// untouched for a day) unless you chose to keep them out. Stacked tiles leave
// the grid and give up their live stream. Saved per device by session id.

import { useSyncExternalStore } from "react";
import type { Conversation } from "@/hooks/useConversations";
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

const STACK_KEY = "omnigent:tiles-stack";
const listeners = new Set<() => void>();
let cache: { raw: string | null; value: TileStackState } = { raw: null, value: EMPTY };

function readStack(): TileStackState {
  let raw: string | null;
  try {
    raw = localStorage.getItem(STACK_KEY);
  } catch {
    return EMPTY;
  }
  if (raw === cache.raw) return cache.value;
  let value = EMPTY;
  try {
    const parsed = JSON.parse(raw ?? "null") as Partial<TileStackState> | null;
    const ids = (list: unknown) =>
      Array.isArray(list) ? list.filter((id): id is string => typeof id === "string") : [];
    value = { parked: ids(parsed?.parked), kept: ids(parsed?.kept) };
  } catch {
    // A corrupt entry reads as an empty Stack.
  }
  cache = { raw, value };
  return value;
}

export function writeTileStack(state: TileStackState): void {
  try {
    localStorage.setItem(STACK_KEY, JSON.stringify(state));
  } catch {
    // Storage unavailable: the Stack still works for this page view.
  }
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  const onStorage = (event: StorageEvent) => event.key === STACK_KEY && listener();
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

export function useTileStack(): TileStackState {
  return useSyncExternalStore(subscribe, readStack);
}
