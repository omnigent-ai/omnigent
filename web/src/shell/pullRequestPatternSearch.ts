// Runs a regular-expression find in a module worker, so a backtracking pattern can't block the
// UI thread. The worker is terminated when a budget runs out, the search is aborted, or it fails.

import type { Match, PatchMatchTarget } from "./pullRequestReview";

/** Matching budget, counted from when the worker reports that it started matching. */
export const PATTERN_TIME_BUDGET_MS = 1000;
/** Budget for the worker to load and receive the corpus. Exceeding it is a failure, not a slow pattern. */
export const WORKER_START_TIMEOUT_MS = 5000;

export interface FindOutcome {
  matches: Match[];
  truncated: boolean;
  invalid: boolean;
  /** The pattern ran past its matching budget. */
  tooSlow: boolean;
  /** The search could not complete: the worker failed or never started. */
  failed: boolean;
}

export interface PatternSearchOptions {
  matchCase: boolean;
  regex: boolean;
  limit: number;
}

export const TOO_SLOW: FindOutcome = {
  matches: [],
  truncated: false,
  invalid: false,
  tooSlow: true,
  failed: false,
};
export const FAILED: FindOutcome = { ...TOO_SLOW, tooSlow: false, failed: true };

/** Resolves to the outcome, or to null when the signal aborts first. Never rejects. */
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
    let timer: ReturnType<typeof setTimeout> | undefined;
    let done = false;
    const finish = (outcome: FindOutcome | null) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      worker.terminate();
      resolve(outcome);
    };
    const abort = () => finish(null);
    signal?.addEventListener("abort", abort);
    timer = setTimeout(() => finish(FAILED), WORKER_START_TIMEOUT_MS);
    worker.onerror = () => finish(FAILED);
    worker.onmessageerror = () => finish(FAILED);
    worker.onmessage = (event: MessageEvent<PatternSearchMessage>) => {
      if (done) return;
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
    try {
      const request: PatternSearchRequest = { corpus, query, options };
      worker.postMessage(request);
    } catch {
      // A corpus that can't be cloned, such as one with unclonable values, means the search can't run.
      finish(FAILED);
    }
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

/** Either the `{ started: true }` notice sent before matching, or the final reply. */
export type PatternSearchMessage = { started: true } | PatternSearchReply;
