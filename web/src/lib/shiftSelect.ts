/**
 * Compute the set of IDs to add for a shift-click range selection.
 * Returns null when the range can't be computed (missing anchor or id).
 */
export function computeShiftSelectRange(
  visibleIds: readonly string[],
  anchorId: string,
  targetId: string,
): string[] | null {
  const anchorIdx = visibleIds.indexOf(anchorId);
  const targetIdx = visibleIds.indexOf(targetId);
  if (anchorIdx === -1 || targetIdx === -1) return null;
  const [start, end] = anchorIdx < targetIdx ? [anchorIdx, targetIdx] : [targetIdx, anchorIdx];
  return visibleIds.slice(start, end + 1);
}
