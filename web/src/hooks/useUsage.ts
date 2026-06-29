import { useQuery } from "@tanstack/react-query";
import { getUsage, type UsageSummary } from "@/lib/usageApi";

/** Fetch the aggregated usage summary for the dashboard. Polls modestly. */
export function useUsage() {
  return useQuery<UsageSummary>({
    queryKey: ["usage"],
    queryFn: getUsage,
    staleTime: 10_000,
    refetchInterval: 30_000,
  });
}
