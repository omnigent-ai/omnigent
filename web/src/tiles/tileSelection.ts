// Pure rules for the Tile view: which sessions get a tile, which tile is the
// active agent, and which tiles hold one of the scarce live streams.

import type { Conversation } from "@/hooks/useConversations";

export type TilesView = "gallery" | "focus" | "cards";
export const TILES_VIEWS: readonly TilesView[] = ["gallery", "focus", "cards"];

/** Sessions quiet for longer than this leave the tiles (AgentFarm's "Quiet 2d+"). */
export const QUIET_AFTER_MS = 2 * 24 * 60 * 60 * 1000;

type TileRow = Pick<
  Conversation,
  "id" | "status" | "updated_at" | "archived" | "pending_elicitations_count"
>;

export function needsYou(row: Pick<Conversation, "pending_elicitations_count">): boolean {
  return (row.pending_elicitations_count ?? 0) > 0;
}

/**
 * Split sessions into active tiles and dormant ones. A session is active when it
 * is running, waiting on you, or was touched within the quiet window.
 * `updated_at` is epoch seconds, as on the session list.
 */
export function partitionActive<T extends TileRow>(
  rows: readonly T[],
  nowMs: number,
  quietAfterMs = QUIET_AFTER_MS,
): { active: T[]; dormant: T[] } {
  const active: T[] = [];
  const dormant: T[] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    // One tile per conversation, even if a page boundary repeats a row.
    if (seen.has(row.id) || row.archived) continue;
    seen.add(row.id);
    const recent = nowMs - row.updated_at * 1000 <= quietAfterMs;
    if (row.status === "running" || needsYou(row) || recent) active.push(row);
    else dormant.push(row);
  }
  return { active, dormant };
}

/** The active agent: your choice when it still has a tile, else the first needing you, else the first. */
export function resolveActiveId(tiles: readonly TileRow[], chosenId: string | null): string | null {
  if (chosenId && tiles.some((t) => t.id === chosenId)) return chosenId;
  return (tiles.find(needsYou) ?? tiles[0])?.id ?? null;
}

/**
 * Tiles that stream live, within the browser's stream budget. The active tile
 * always gets a slot; the rest go in display order. Tiles not returned render
 * as status cards until they become active.
 */
export function pickLiveIds(
  orderedIds: readonly string[],
  activeId: string | null,
  budget: number,
): Set<string> {
  const live = new Set<string>();
  if (budget <= 0) return live;
  if (activeId && orderedIds.includes(activeId)) live.add(activeId);
  for (const id of orderedIds) {
    if (live.size >= budget) break;
    live.add(id);
  }
  return live;
}

/**
 * Live tiles allowed on this connection. Over HTTP/1.1 the browser holds ~6
 * connections per origin, shared with the updates socket and any open terminal,
 * so tiles keep two streams there; HTTP/2 can use the registry's full budget.
 */
export function tileStreamBudget(protocol: "multiplexed" | "serial", registryMax: number): number {
  return protocol === "multiplexed" ? registryMax : Math.min(2, registryMax);
}

export function parseTilesView(value: string | null | undefined): TilesView | null {
  return TILES_VIEWS.includes(value as TilesView) ? (value as TilesView) : null;
}

/**
 * Clicks that should not activate a tile: its controls and anything you can
 * type into or follow (AgentFarm's activation guard).
 */
export function isInteractiveTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  return (
    target.closest(
      'button, a, input, textarea, select, [role="button"], [role="menuitem"], [contenteditable="true"], [data-tile-no-activate]',
    ) !== null
  );
}
