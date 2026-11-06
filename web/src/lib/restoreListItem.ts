/**
 * Put a removed item back into a list where it sat in an earlier snapshot:
 * before the first item that followed it there and is still present, else at
 * the end. Other changes made since the snapshot are kept.
 *
 * @param current - The list as it is now.
 * @param snapshot - The list before the item was removed.
 * @param key - Identity of an entry, used to find the item and its neighbours.
 * @param itemKey - Identity of the item to restore.
 * @returns `current` itself when the item is already present or not in the snapshot.
 */
export function restoreListItem<T>(
  current: T[],
  snapshot: readonly T[],
  key: (entry: T) => string | null,
  itemKey: string,
): T[] {
  const index = snapshot.findIndex((entry) => key(entry) === itemKey);
  if (index < 0 || current.some((entry) => key(entry) === itemKey)) return current;
  const remaining = new Set(current.map(key));
  const next = snapshot.slice(index + 1).find((entry) => remaining.has(key(entry)));
  const at = next === undefined ? current.length : current.findIndex((e) => key(e) === key(next));
  return [...current.slice(0, at), snapshot[index], ...current.slice(at)];
}
