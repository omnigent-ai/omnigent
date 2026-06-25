export type ChangedSort = "alpha" | "recent" | "size" | "type";

const VALID_SORTS = new Set<ChangedSort>(["alpha", "recent", "size", "type"]);

export function isValidSort(value: string): value is ChangedSort {
  return VALID_SORTS.has(value as ChangedSort);
}
