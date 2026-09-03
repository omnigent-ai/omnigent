import { useQuery } from "@tanstack/react-query";
import {
  fetchDpiaContributorSummaries,
  fetchDpiaRequestSummaries,
  type DpiaContributorSummary,
  type DpiaRequestSummary,
} from "@/lib/dpia/requestInbox";

export function useDpiaRequests(options: { enabled?: boolean } = {}) {
  return useQuery<DpiaRequestSummary[]>({
    queryKey: ["dpia-requests"],
    queryFn: ({ signal }) => fetchDpiaRequestSummaries(signal),
    refetchInterval: 20_000,
    enabled: options.enabled ?? true,
  });
}

export function useDpiaContributorResponses(caseId: string, options: { enabled?: boolean } = {}) {
  return useQuery<DpiaContributorSummary[]>({
    queryKey: ["dpia-contributor-responses", caseId],
    queryFn: ({ signal }) => fetchDpiaContributorSummaries(caseId, signal),
    refetchInterval: 20_000,
    enabled: options.enabled ?? true,
  });
}
