// Runs a regular-expression find in a worker, so a backtracking pattern can't block the UI
// thread. The worker is terminated once a time budget runs out or the query is superseded.

import type { Match, PatchMatchTarget } from "./pullRequestReview";

/** Wall-clock budget for matching one pattern, counted from when the worker starts it. */
export const PATTERN_TIME_BUDGET_MS = 1000;
/** Budget for loading the worker and receiving the corpus, which a cold start can spend. */
export const WORKER_START_TIMEOUT_MS = 5000;

export interface FindOutcome {
  matches: Match[];
  truncated: boolean;
  invalid: boolean;
  /** The pattern ran past its budget, so no matches are reported. */
  tooSlow: boolean;
  /** The worker could not run or failed, so the search has no result. */
  failed: boolean;
}

export interface PatternSearchOptions {
  matchCase: boolean;
  regex: boolean;
  limit: number;
}

const TOO_SLOW: FindOutcome = {
  matches: [],
  truncated: false,
  invalid: false,
  tooSlow: true,
  failed: false,
};
const FAILED: FindOutcome = { ...TOO_SLOW, tooSlow: false, failed: true };

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
    let worker: Worker;
    try {
      worker = new Worker(new URL("./pullRequestPatternSearch.worker.ts", import.meta.url), {
        type: "module",
      });
    } catch {
      resolve(FAILED);
      return;
    }
    const finish = (outcome: FindOutcome | null) => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      worker.terminate();
      resolve(outcome);
    };
    const abort = () => finish(null);
    let timer = setTimeout(() => finish(TOO_SLOW), WORKER_START_TIMEOUT_MS);
    signal?.addEventListener("abort", abort);
    worker.onerror = () => finish(FAILED);
    worker.onmessageerror = () => finish(FAILED);
    worker.onmessage = (event: MessageEvent<PatternSearchMessage>) => {
      if ("started" in event.data) {
        // The worker has the corpus and begins matching, so the match budget starts now.
        clearTimeout(timer);
        timer = setTimeout(() => finish(TOO_SLOW), PATTERN_TIME_BUDGET_MS);
        return;
      }
      const { matches, truncated, invalid } = event.data;
      finish({
        matches: matches.map(({ index, start, end }) => ({ target: corpus[index], start, end })),
        truncated,
        invalid,
        tooSlow: false,
        failed: false,
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

/** Sent once the worker has the corpus and starts matching, before its reply. */
export type PatternSearchMessage = { started: true } | PatternSearchReply;
