// Find results for the Changes tab. Plain substrings match synchronously on the UI thread;
// regular expressions match in a worker with a time budget (see pullRequestPatternSearch.ts).

import { useEffect, useMemo, useState } from "react";
import { searchPatternInWorker, type FindOutcome } from "./pullRequestPatternSearch";
import { findMatches, MATCH_LIMIT, type PatchMatchTarget } from "./pullRequestReview";

export interface FindSearch extends FindOutcome {
  /** A regular expression is still running for the current corpus and query. */
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

interface SettledSearch {
  corpus: readonly PatchMatchTarget[];
  query: string;
  matchCase: boolean;
  search: FindSearch;
}

export function useFindSearch(
  corpus: readonly PatchMatchTarget[],
  query: string,
  options: { matchCase: boolean; regex: boolean },
): FindSearch {
  const { matchCase, regex } = options;
  // Blank queries, an empty corpus and plain substrings are cheap and stay synchronous.
  const onWorker = regex && query.trim() !== "" && corpus.length > 0;
  const local = useMemo<FindSearch | null>(
    () =>
      onWorker
        ? null
        : {
            ...findMatches(corpus, query, { matchCase, regex, limit: MATCH_LIMIT }),
            tooSlow: false,
            failed: false,
            searching: false,
          },
    [corpus, query, matchCase, regex, onWorker],
  );
  const [settled, setSettled] = useState<SettledSearch | null>(null);
  useEffect(() => {
    if (!onWorker) return;
    const controller = new AbortController();
    void searchPatternInWorker(
      corpus,
      query,
      { matchCase, regex: true, limit: MATCH_LIMIT },
      controller.signal,
    ).then((outcome) => {
      if (outcome) {
        setSettled({ corpus, query, matchCase, search: { ...outcome, searching: false } });
      }
    });
    // Typing a new pattern stops the previous worker, so a runaway pattern can't keep running.
    return () => controller.abort();
  }, [corpus, query, matchCase, onWorker]);
  // A refreshed corpus under the same query keeps the last matches while the worker reruns,
  // so the occurrence the reader is on survives the wait. A new query drops them.
  const refreshing = useMemo<FindSearch | null>(
    () => (settled ? { ...settled.search, searching: true } : null),
    [settled],
  );

  if (local) return local;
  if (settled?.query === query && settled.matchCase === matchCase) {
    if (settled.corpus === corpus) return settled.search;
    if (refreshing) return refreshing;
  }
  return SEARCHING;
}
