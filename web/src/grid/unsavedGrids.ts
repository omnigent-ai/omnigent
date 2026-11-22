import type { SessionGrid } from "./gridLayout";

/**
 * Grid changes a failed storage write left only in this tab. Each remembers
 * the stored copy it replaced, so it applies only while storage still holds
 * that copy: another tab's later edit or delete wins.
 */
export interface UnsavedGrids {
  /** Grids this tab added or edited; `baseline` is null for a new grid. */
  changed: Map<string, { grid: SessionGrid; baseline: string | null }>;
  /** Stored grids this tab deleted, with the stored copy each removed. */
  removed: Map<string, string>;
}

const serialize = (grid: SessionGrid | undefined) => (grid ? JSON.stringify(grid) : null);

/**
 * Record what a failed write of `next` couldn't store. Only grids the write
 * changed or dropped from `base`, the list it was built from, count.
 */
export function recordUnsavedWrite(
  previous: UnsavedGrids | undefined,
  stored: readonly SessionGrid[],
  base: readonly SessionGrid[],
  next: readonly SessionGrid[],
): UnsavedGrids | undefined {
  const storedById = new Map(stored.map((grid) => [grid.id, grid]));
  const baseById = new Map(base.map((grid) => [grid.id, grid]));
  const nextIds = new Set(next.map((grid) => grid.id));
  const changed = new Map(previous?.changed);
  const removed = new Map(previous?.removed);
  // An earlier change keeps its baseline while storage still holds that copy;
  // otherwise the change replaces the copy this write was built from.
  const baselineOf = (id: string): string | null => {
    const earlier = changed.has(id) ? changed.get(id)!.baseline : removed.get(id);
    if (earlier !== undefined && earlier === serialize(storedById.get(id))) return earlier;
    return serialize(baseById.get(id));
  };
  for (const grid of next) {
    if (serialize(grid) === serialize(baseById.get(grid.id))) continue;
    const baseline = baselineOf(grid.id);
    removed.delete(grid.id);
    changed.set(grid.id, { grid, baseline });
  }
  for (const id of baseById.keys()) {
    if (nextIds.has(id)) continue;
    const baseline = baselineOf(id);
    changed.delete(id);
    if (baseline !== null) removed.set(id, baseline);
  }
  return changed.size > 0 || removed.size > 0 ? { changed, removed } : undefined;
}

/** Stored grids with this tab's still-applicable unsaved changes on top. */
export function withUnsaved(
  stored: SessionGrid[],
  unsaved: UnsavedGrids | undefined,
): SessionGrid[] {
  if (unsaved === undefined) return stored;
  const grids: SessionGrid[] = [];
  for (const grid of stored) {
    const copy = serialize(grid);
    if (unsaved.removed.get(grid.id) === copy) continue;
    const edit = unsaved.changed.get(grid.id);
    grids.push(edit && edit.baseline === copy ? edit.grid : grid);
  }
  const storedIds = new Set(stored.map((grid) => grid.id));
  for (const [id, { grid, baseline }] of unsaved.changed)
    if (baseline === null && !storedIds.has(id)) grids.push(grid);
  return grids;
}
