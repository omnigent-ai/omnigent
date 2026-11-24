import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

/** A project ticket whose open `needs-human` ask waits on the viewer. */
export interface TicketRequest {
  project_id: string;
  project_name: string;
  ticket_id: string;
  title: string;
  stage: { id: string; name: string };
  /** `"product-ok"` for a feature awaiting a product approver; `"owner"` otherwise. */
  reason: "product-ok" | "owner";
  /** Everyone the ask is routed to; any one of them answering clears it for all. */
  responders: string[];
  raised_at: number;
  raised_by_session_id: string | null;
  /** The asking agent's latest comment, when it left one. */
  question: string | null;
}

export interface TicketRequestPage {
  data: TicketRequest[];
  has_more: boolean;
  is_complete: boolean;
}

export async function listTicketRequests(): Promise<TicketRequestPage> {
  const response = await authenticatedFetch("/v1/inbox/ticket-requests");
  // Servers without ticket boards don't serve the route; treat that as nothing waiting.
  if (response.status === 404) return { data: [], has_more: false, is_complete: true };
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as TicketRequestPage;
}
