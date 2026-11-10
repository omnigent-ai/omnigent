// Find results for the Changes tab. Plain substrings match synchronously on the UI thread;
// regular expressions match in a worker (see pullRequestPatternSearch.ts) and read `searching`
// until the worker settles.

import { useEffect, useMemo, useState } from "react";
import { FAILED, searchPatternInWorker, type FindOutcome } from "./pullRequestPatternSearch";
import { findMatches, MATCH_LIMIT, type PatchMatchTarget } from "./pullRequestReview";

export interface FindSearch extends FindOutcome {
  /** A regular expression is still running for this corpus and query; there are no matches yet. */
  searching: boolean;
}

const SEARCHING: FindSearch = {
  matches: [],
  truncated: false,
  invalid: false,
  tooSlow: false,
  failed: false,
  searching: true,
};

export function useFindSearch(
  corpus: readonly PatchMatchTarget[],
  query: string,
  options: { matchCase: boolean; regex: boolean },
): FindSearch {
  const { matchCase, regex } = options;
  // A blank query or an empty corpus has nothing to match, so it never starts a worker.
  const runsInWorker = regex && query.trim() !== "" && corpus.length > 0;
  const synchronous = useMemo<FindSearch | null>(
    () =>
      runsInWorker
        ? null
        : {
            ...findMatches(corpus, query, { matchCase, regex, limit: MATCH_LIMIT }),
            tooSlow: false,
            failed: false,
            searching: false,
          },
    [corpus, query, matchCase, regex, runsInWorker],
  );
  // A new object for each set of inputs, so a result is shown only for the inputs that produced it.
  // A later query, corpus or mode is a new run, and nothing from the earlier one is reused.
  const run = useMemo(
    () => ({ corpus, query, matchCase, runsInWorker }),
    [corpus, query, matchCase, runsInWorker],
  );
  const [settled, setSettled] = useState<{ run: object; search: FindSearch } | null>(null);
  useEffect(() => {
    if (!runsInWorker) return;
    const controller = new AbortController();
    void searchPatternInWorker(
      corpus,
      query,
      { matchCase, regex: true, limit: MATCH_LIMIT },
      controller.signal,
    ).then(
      (outcome) => {
        if (outcome && !controller.signal.aborted) {
          setSettled({ run, search: { ...outcome, searching: false } });
        }
      },
      () => {
        if (!controller.signal.aborted)
          setSettled({ run, search: { ...FAILED, searching: false } });
      },
    );
    // Abort on any input change or unmount so a runaway pattern stops.
    return () => controller.abort();
  }, [run, corpus, query, matchCase, runsInWorker]);

  if (synchronous) return synchronous;
  return settled?.run === run ? settled.search : SEARCHING;
}
