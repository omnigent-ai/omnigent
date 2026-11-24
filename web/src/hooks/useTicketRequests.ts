import { useQuery } from "@tanstack/react-query";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { listTicketRequests } from "@/lib/ticketRequestsApi";
import { useViewerId } from "./useViewerId";

/** Project tickets waiting on the viewer's response, for the Inbox and its badge. */
export function useTicketRequests(identityReady: boolean) {
  const info = useServerInfo();
  const viewerId = useViewerId();
  const enabled = identityReady && info !== "loading";
  const query = useQuery({
    queryKey: ["inbox-ticket-requests", viewerId],
    queryFn: listTicketRequests,
    enabled,
    staleTime: 15_000,
    refetchInterval: 30_000,
    retry: 1,
  });
  return {
    ...query,
    items: enabled ? (query.data?.data ?? []) : [],
    isLoading: enabled && query.isLoading,
    isError: enabled && query.isError,
  };
}
