// The note a find bar shows when some matches are where it can't show them, such as in
// collapsed steps or in files a filter hides.

/**
 * Why some matches can't be shown, e.g. "2 more matches are in hidden steps." A capped count
 * reads as a lower bound: "20,000+ more matches are in hidden files."
 */
export function hiddenMatchesNote(
  hiddenCount: number,
  hasShownMatches: boolean,
  { where = "hidden steps", truncated = false }: { where?: string; truncated?: boolean } = {},
): string {
  const count = `${hiddenCount.toLocaleString()}${truncated ? "+" : ""}`;
  const matches = hiddenCount === 1 && !truncated ? "match is" : "matches are";
  return `${count} ${hasShownMatches ? "more " : ""}${matches} in ${where}.`;
}
