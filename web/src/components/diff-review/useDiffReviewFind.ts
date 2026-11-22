// Find state for a diff review: the query, its options, and which match is
// current. Revealing and painting matches is the caller's; this hook decides
// when to, so the logic stays testable without a rendered diff.

import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { DEFAULT_FIND_OPTIONS, type FindOptions } from "@/components/FindBar";
import {
  findReviewMatches,
  findStatusLabel,
  type ReviewSearchLine,
  type ReviewSearchMatch,
} from "./findMatches";

export interface DiffReviewFindCallbacks {
  /** Scroll to and paint `match`, expanding its file if needed. */
  reveal: (match: ReviewSearchMatch) => void;
  /** Remove the painted match. */
  clear: () => void;
  /** The match to start from: the first one at or below the viewport. */
  startingIndex: (matches: readonly ReviewSearchMatch[]) => number;
}

/** Matches in files the review lists none of, for the find bar's note and Show. */
export interface HiddenFindMatches {
  count: number;
  /** The count reached the match limit, so there may be more. */
  truncated: boolean;
}

const NO_HIDDEN_MATCHES: HiddenFindMatches = { count: 0, truncated: false };

export interface DiffReviewFind {
  query: string;
  setQuery: (query: string) => void;
  options: FindOptions;
  setOptions: (options: FindOptions) => void;
  status: string;
  hasMatches: boolean;
  /** True while matching hasn't caught up with the latest query. */
  searching: boolean;
  next: () => void;
  previous: () => void;
  /** Matches of the same query in the files the review's filters leave out. */
  hidden: HiddenFindMatches;
}

export function useDiffReviewFind(
  open: boolean,
  lines: readonly ReviewSearchLine[],
  callbacks: DiffReviewFindCallbacks,
  /** The lines of the files the review doesn't list; only searched while find is open. */
  hiddenLines: readonly ReviewSearchLine[] = [],
): DiffReviewFind {
  const [query, setQueryState] = useState("");
  const [options, setOptionsState] = useState<FindOptions>(DEFAULT_FIND_OPTIONS);
  const [activeIndex, setActiveIndex] = useState(0);
  const activeIndexRef = useRef(0);
  const latest = useRef(callbacks);
  latest.current = callbacks;

  // A changed query or options withdraws the shown match at once, before its deferred result
  // arrives, so nothing still queued for the old one can paint or scroll in the meantime.
  // Each withdrawal is counted, so a query that returns to a cached result is revealed again.
  const accepted = useRef({ query, options });
  const [withdrawals, setWithdrawals] = useState(0);
  const setQuery = useCallback((next: string) => {
    if (next !== accepted.current.query) {
      accepted.current = { ...accepted.current, query: next };
      latest.current.clear();
      setWithdrawals((count) => count + 1);
    }
    setQueryState(next);
  }, []);
  const setOptions = useCallback((next: FindOptions) => {
    const current = accepted.current.options;
    const changed = (Object.keys(next) as (keyof FindOptions)[]).some(
      (key) => next[key] !== current[key],
    );
    if (changed) {
      accepted.current = { ...accepted.current, options: next };
      latest.current.clear();
      setWithdrawals((count) => count + 1);
    }
    setOptionsState(next);
  }, []);

  // A million-line review takes a moment to search; keep typing responsive.
  const deferredQuery = useDeferredValue(query);
  const result = useMemo(
    () => findReviewMatches(lines, deferredQuery, options),
    [lines, deferredQuery, options],
  );
  const hidden = useMemo((): HiddenFindMatches => {
    if (!open || hiddenLines.length === 0) return NO_HIDDEN_MATCHES;
    const found = findReviewMatches(hiddenLines, deferredQuery, options);
    // An invalid expression says so in the bar's status; it has no hidden matches to count.
    if (!found.valid || found.matches.length === 0) return NO_HIDDEN_MATCHES;
    return { count: found.matches.length, truncated: found.truncated };
  }, [open, hiddenLines, deferredQuery, options]);

  // While matching catches up with a newer query, the results shown are the old query's.
  const catchingUp = query !== deferredQuery;

  // A new query, new options, or new content restarts at the first match in view, once
  // matching has caught up with the accepted query.
  useEffect(() => {
    if (!open || catchingUp) return;
    if (result.matches.length === 0) {
      activeIndexRef.current = 0;
      setActiveIndex(0);
      latest.current.clear();
      return;
    }
    const start = latest.current.startingIndex(result.matches);
    const index = Math.max(0, Math.min(start, result.matches.length - 1));
    activeIndexRef.current = index;
    setActiveIndex(index);
    latest.current.reveal(result.matches[index]!);
  }, [open, result, withdrawals, catchingUp]);

  useEffect(() => {
    if (!open) latest.current.clear();
  }, [open]);

  const move = useCallback(
    (offset: 1 | -1) => {
      const count = result.matches.length;
      if (count === 0 || catchingUp) return;
      const index = (activeIndexRef.current + offset + count) % count;
      activeIndexRef.current = index;
      setActiveIndex(index);
      latest.current.reveal(result.matches[index]!);
    },
    [result, catchingUp],
  );

  return {
    query,
    setQuery,
    options,
    setOptions,
    status: findStatusLabel(deferredQuery, result, activeIndex),
    hasMatches: result.matches.length > 0,
    searching: catchingUp,
    next: useCallback(() => move(1), [move]),
    previous: useCallback(() => move(-1), [move]),
    hidden,
  };
}
