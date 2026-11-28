// Runs a bulk action's per-PR changes a few at a time, outside React. Each PR's change is
// sent by its own call, which decides at that moment where it goes and as whom, so support or
// the account going away mid-batch blocks the rest rather than sending them elsewhere.

/** How one PR's change ended: made, refused, unknown (held for Check Again), or not sent. */
export type BulkOutcome = "applied" | "refused" | "uncertain" | "skipped";

export interface BulkProgress {
  total: number;
  /** How many have ended, whatever their outcome. */
  done: number;
  outcomes: ReadonlyMap<string, BulkOutcome>;
}

/** At most this many PRs' changes are in flight at once. */
export const BULK_CONCURRENCY = 3;

/**
 * Run `step` for each of `urls`, at most `limit` at a time, in order. Once `stillCurrent` says
 * no (the page's account or workspace user changed), nothing more starts; those already sent
 * settle as they would. Resolves with every outcome, unsent ones as `skipped`.
 */
export async function runBulk(
  urls: readonly string[],
  step: (url: string) => Promise<BulkOutcome>,
  {
    limit = BULK_CONCURRENCY,
    stillCurrent = () => true,
    onProgress,
  }: {
    limit?: number;
    stillCurrent?: () => boolean;
    onProgress?: (progress: BulkProgress) => void;
  } = {},
): Promise<BulkProgress> {
  const outcomes = new Map<string, BulkOutcome>();
  let next = 0;
  const report = () => onProgress?.({ total: urls.length, done: outcomes.size, outcomes });
  const worker = async () => {
    while (next < urls.length) {
      const url = urls[next++]!;
      if (!stillCurrent()) {
        outcomes.set(url, "skipped");
        continue;
      }
      // A change that throws is one GitHub didn't make: the page lists it, with Retry.
      // oxlint-disable-next-line no-await-in-loop -- each worker sends its PRs one at a time
      const outcome = await step(url).catch((): BulkOutcome => "refused");
      outcomes.set(url, outcome);
      report();
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, urls.length) }, worker));
  report();
  return { total: urls.length, done: outcomes.size, outcomes };
}
