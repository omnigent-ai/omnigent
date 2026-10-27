import { useEffect, useRef } from "react";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { fetchUsageReport, StaleCursorError } from "@/lib/usageApi";

export function useUsageReport(since: string | null, until: string | null) {
  const queryClient = useQueryClient();
  const query = useInfiniteQuery({
    queryKey: ["usage", since, until],
    queryFn: ({ pageParam }) =>
      fetchUsageReport({ limit: 50, after: pageParam ?? null, since, until }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) =>
      lastPage.sessionsHasMore ? lastPage.sessionsLastId : undefined,
    staleTime: 60_000,
    // A stale cursor cannot be recovered by retrying the same page; let the
    // reset below restart from the first page instead of retrying blindly.
    retry: (failureCount, error) => !(error instanceof StaleCursorError) && failureCount < 3,
  });

  // A session the cursor pointed at was deleted between page fetches. The
  // loaded pages are now unanchored, so drop them and refetch the first page
  // rather than leaving Load More wedged on the dead cursor.
  const staleCursor = query.error instanceof StaleCursorError;
  const restartedForStale = useRef(false);
  useEffect(() => {
    if (query.isSuccess) {
      // A good page landed; re-arm so a later stale cursor can restart again.
      restartedForStale.current = false;
      return;
    }
    // Restart once per stale episode; if the fresh first page is also stale,
    // surface the error instead of looping. resetQueries briefly clears the
    // error, so re-arm on a real success, not merely on the error's absence.
    if (!staleCursor || restartedForStale.current) return;
    restartedForStale.current = true;
    void queryClient.resetQueries({ queryKey: ["usage", since, until] });
  }, [query.isSuccess, staleCursor, queryClient, since, until]);

  return query;
}
