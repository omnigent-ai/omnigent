// A plain QueryCache subscription for `useSyncExternalStore`. Unlike an observer, it installs no
// fetch of its own into a shared query, so a later invalidation never runs one for it.

import type { QueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

/**
 * A stable subscribe that calls back on every change to `client`'s cache, or, given `hash`,
 * only on changes to that one entry: added, updated or removed.
 */
export function useQueryCacheSubscription(
  client: QueryClient,
  hash: string | null = null,
): (onChange: () => void) => () => void {
  return useCallback(
    (onChange: () => void) =>
      client.getQueryCache().subscribe((event) => {
        if (hash === null || event.query.queryHash === hash) onChange();
      }),
    [client, hash],
  );
}
