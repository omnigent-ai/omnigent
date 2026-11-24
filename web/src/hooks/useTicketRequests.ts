import { useQuery } from "@tanstack/react-query";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { listSessionTickets, listTicketRequests } from "@/lib/ticketRequestsApi";
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

// The server caps one lookup at this many sessions.
const SESSION_TICKET_LOOKUP_MAX = 100;

/** The ticket each listed session works on, so its Inbox card can open the ticket. */
export function useSessionTickets(sessionIds: readonly string[]) {
  const ids = [...new Set(sessionIds)].sort().slice(0, SESSION_TICKET_LOOKUP_MAX);
  const query = useQuery({
    queryKey: ["inbox-session-tickets", ids],
    queryFn: () => listSessionTickets(ids),
    enabled: ids.length > 0,
    staleTime: 60_000,
    retry: 1,
  });
  return query.data ?? {};
}
