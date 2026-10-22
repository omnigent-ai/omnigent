import { useQuery } from "@tanstack/react-query";
import { fetchUsageReport, type UsageReport } from "@/lib/usageApi";

export function useUsageReport() {
  return useQuery<UsageReport>({
    queryKey: ["usage"],
    queryFn: fetchUsageReport,
    staleTime: 60_000,
  });
}
