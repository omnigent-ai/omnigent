import { useInfiniteQuery } from "@tanstack/react-query";
import { fetchUsageReport } from "@/lib/usageApi";

export function useUsageReport(since: string | null, until: string | null) {
  return useInfiniteQuery({
    queryKey: ["usage", since, until],
    queryFn: ({ pageParam }) =>
      fetchUsageReport({ limit: 50, after: pageParam ?? null, since, until }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) =>
      lastPage.sessionsHasMore ? lastPage.sessionsLastId : undefined,
    staleTime: 60_000,
  });
}
