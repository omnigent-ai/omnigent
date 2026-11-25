// Which pull requests a Refresh is reading again right now, so reads this Mac would answer from
// what it kept read GitHub again instead.

/** Every pull request, for a Refresh of a whole session's pull requests. */
export const EVERY_PULL_REQUEST = "*";

const refreshing = new Map<string, number>();

/**
 * Read `prUrl`, or {@link EVERY_PULL_REQUEST}, past this Mac's kept answers until the returned
 * function is called.
 */
export function beginFreshReads(prUrl: string): () => void {
  refreshing.set(prUrl, (refreshing.get(prUrl) ?? 0) + 1);
  let ended = false;
  return () => {
    if (ended) return;
    ended = true;
    const left = (refreshing.get(prUrl) ?? 1) - 1;
    if (left > 0) refreshing.set(prUrl, left);
    else refreshing.delete(prUrl);
  };
}

/** Whether a Refresh that covers `prUrl` is under way. */
export function readsFresh(prUrl: string): boolean {
  return refreshing.has(prUrl) || refreshing.has(EVERY_PULL_REQUEST);
}
