/** The part of a virtualizer's range that decides which rows mount. */
export interface VirtualRange {
  startIndex: number;
  endIndex: number;
  overscan: number;
  count: number;
}

/**
 * The visible window plus overscan, together with rows that must stay mounted
 * while scrolled away (a focused option, an open menu, a row being dragged).
 * Indices are ascending and unique; retained indices out of range are ignored.
 */
export function retainedRowIndices(range: VirtualRange, retained: readonly number[]): number[] {
  if (range.count === 0) return [];
  const start = Math.max(range.startIndex - range.overscan, 0);
  const end = Math.min(range.endIndex + range.overscan, range.count - 1);
  const indices = new Set<number>();
  for (let index = start; index <= end; index++) indices.add(index);
  for (const index of retained) {
    if (Number.isInteger(index) && index >= 0 && index < range.count) indices.add(index);
  }
  return [...indices].sort((a, b) => a - b);
}
