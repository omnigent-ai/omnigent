// What a spied `invalidateQueries` reached, by key: each call's filters as TanStack matches them,
// so a test says which reads went stale, whether the code named them by prefix or by predicate.

import { hashKey, matchQuery, type Query, type QueryFilters } from "@tanstack/react-query";

/** A spied `invalidateQueries`, `refetchQueries` or the like: its first argument is the filters. */
interface FiltersSpy {
  mock: { calls: readonly (readonly unknown[])[] };
}

/**
 * Whether any call `spy` saw would reach a query cached under `queryKey`, by its key and
 * predicate; filters on a query's state (stale, active, fetching) are the caller's to check.
 */
export function invalidatedKey(spy: FiltersSpy, queryKey: readonly unknown[]): boolean {
  const query = { queryKey, queryHash: hashKey(queryKey) } as unknown as Query;
  return spy.mock.calls.some(([filters]) => {
    const { queryKey: prefix, exact, predicate } = (filters ?? {}) as QueryFilters;
    return matchQuery({ queryKey: prefix, exact, predicate }, query);
  });
}
