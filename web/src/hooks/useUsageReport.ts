import { useInfiniteQuery } from "@tanstack/react-query";
import { fetchUsageReport } from "@/lib/usageApi";

export function useUsageReport() {
  return useInfiniteQuery({
    queryKey: ["usage"],
    queryFn: ({ pageParam }) => fetchUsageReport({ limit: 50, after: pageParam ?? null }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) =>
      lastPage.sessionsHasMore ? lastPage.sessionsLastId : undefined,
    staleTime: 60_000,
  });
}
