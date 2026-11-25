import { useIdentityReady } from "@/hooks/useViewerId";
import { useTicketRequests } from "@/hooks/useTicketRequests";
import type { TicketRequest } from "@/lib/ticketRequestsApi";

/** `?needs=you` narrows the board to the tickets waiting on the viewer. */
export const NEEDS_YOU_PARAM = "needs";
export const NEEDS_YOU_VALUE = "you";

/** This project's tickets whose ask waits on the viewer: the Inbox's asks, without cc's. */
export function needsYouTicketIds(
  requests: readonly TicketRequest[],
  projectId: string | null,
): string[] {
  if (!projectId) return [];
  const ids = requests
    // A cc informs and an answered entry is a receipt; neither waits on the viewer.
    .filter(
      (request) =>
        request.project_id === projectId &&
        request.reason !== "cc" &&
        request.reason !== "answered",
    )
    .map((request) => request.ticket_id);
  return [...new Set(ids)];
}

/** The ids behind the board's "Needs you" chip, from the same query as the Inbox. */
export function useNeedsYouTicketIds(projectId: string | null): string[] {
  const requests = useTicketRequests(useIdentityReady());
  return needsYouTicketIds(requests.items, projectId);
}
