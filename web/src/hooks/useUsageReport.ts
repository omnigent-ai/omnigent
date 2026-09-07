import { useQuery } from "@tanstack/react-query";
import { fetchUsageReport, type UsageReport } from "@/lib/usageApi";

export function useUsageReport(params?: { since?: string | null; until?: string | null }) {
  return useQuery<UsageReport>({
    queryKey: ["usage", params?.since ?? null, params?.until ?? null],
    queryFn: () => fetchUsageReport(params ?? undefined),
    staleTime: 60_000,
  });
}
