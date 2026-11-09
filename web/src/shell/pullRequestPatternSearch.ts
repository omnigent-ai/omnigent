// Runs a regular-expression find in a worker, so a backtracking pattern can't block the UI
// thread. The worker is terminated once the time budget runs out or the query is superseded.

import type { Match, PatchMatchTarget } from "./pullRequestReview";

/** Wall-clock budget for one pattern search before the Changes tab reports it as too slow. */
export const PATTERN_TIME_BUDGET_MS = 1000;

export interface FindOutcome {
  matches: Match[];
  truncated: boolean;
  invalid: boolean;
  /** The pattern ran past `PATTERN_TIME_BUDGET_MS`, so no matches are reported. */
  tooSlow: boolean;
}

export interface PatternSearchOptions {
  matchCase: boolean;
  regex: boolean;
  limit: number;
}

const TOO_SLOW: FindOutcome = { matches: [], truncated: false, invalid: false, tooSlow: true };

/** Resolves to the outcome, or to null when the signal aborts first. */
export function searchPatternInWorker(
  corpus: readonly PatchMatchTarget[],
  query: string,
  options: PatternSearchOptions,
  signal?: AbortSignal,
): Promise<FindOutcome | null> {
  return new Promise((resolve) => {
    if (signal?.aborted) {
      resolve(null);
      return;
    }
    const worker = new Worker(new URL("./pullRequestPatternSearch.worker.ts", import.meta.url), {
      type: "module",
    });
    const finish = (outcome: FindOutcome | null) => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      worker.terminate();
      resolve(outcome);
    };
    const abort = () => finish(null);
    const timer = setTimeout(() => finish(TOO_SLOW), PATTERN_TIME_BUDGET_MS);
    signal?.addEventListener("abort", abort);
    // A worker that fails cannot finish the pattern, so it reports the same state as a timeout.
    worker.onerror = () => finish(TOO_SLOW);
    worker.onmessage = (event: MessageEvent<PatternSearchReply>) => {
      const { matches, truncated, invalid } = event.data;
      finish({
        matches: matches.map(({ index, start, end }) => ({ target: corpus[index], start, end })),
        truncated,
        invalid,
        tooSlow: false,
      });
    };
    const request: PatternSearchRequest = { corpus, query, options };
    worker.postMessage(request);
  });
}

/** Message shapes shared with `pullRequestPatternSearch.worker.ts`. */
export interface PatternSearchRequest {
  corpus: readonly PatchMatchTarget[];
  query: string;
  options: PatternSearchOptions;
}

export interface PatternSearchReply {
  /** Indices into the request's corpus, so the reply doesn't clone each target. */
  matches: { index: number; start: number; end: number }[];
  truncated: boolean;
  invalid: boolean;
}
