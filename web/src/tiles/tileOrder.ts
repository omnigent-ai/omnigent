// The user's tile order, from drag-and-drop or the move keys. Ordered tiles
// trade places among themselves; tiles never moved keep their natural position.

import { createLocalJsonStore } from "./localJsonStore";

/** `ids` with the moved ones rearranged into the slots they occupy, in `order`. */
export function applyTileOrder(ids: readonly string[], order: readonly string[]): string[] {
  const rank = new Map(order.map((id, index) => [id, index]));
  const moved = ids.filter((id) => rank.has(id)).sort((a, b) => rank.get(a)! - rank.get(b)!);
  let next = 0;
  return ids.map((id) => (rank.has(id) ? moved[next++] : id));
}

/** The saved order after moving `id` to `targetId`'s place in the shown tiles. */
export function moveTile(
  order: readonly string[],
  shown: readonly string[],
  id: string,
  targetId: string,
): string[] {
  const from = shown.indexOf(id);
  const to = shown.indexOf(targetId);
  if (from < 0 || to < 0 || from === to) return [...order];
  const next = shown.filter((shownId) => shownId !== id);
  next.splice(to, 0, id);
  const shownSet = new Set(shown);
  // Hidden tiles (filtered or stacked) keep their saved places after the shown ones.
  return [...next, ...order.filter((savedId) => !shownSet.has(savedId))];
}

/** The saved order after moving `id` by `delta` places among the shown tiles. */
export function moveTileBy(
  order: readonly string[],
  shown: readonly string[],
  id: string,
  delta: number,
): string[] {
  const from = shown.indexOf(id);
  const target = shown[Math.min(Math.max(from + delta, 0), shown.length - 1)];
  return from < 0 || target === undefined ? [...order] : moveTile(order, shown, id, target);
}

export function pruneTileOrder(order: readonly string[], liveIds: ReadonlySet<string>): string[] {
  return order.filter((id) => liveIds.has(id));
}

const orderStore = createLocalJsonStore<string[]>("omnigent:tiles-order", [], (raw) =>
  Array.isArray(raw) ? raw.filter((id): id is string => typeof id === "string") : [],
);

export const readTileOrder = orderStore.read;
export const writeTileOrder = orderStore.write;
export const useTileOrder = orderStore.use;
