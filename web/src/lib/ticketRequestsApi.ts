import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

/** A project ticket whose open `needs-human` ask waits on the viewer. */
export interface TicketRequest {
  project_id: string;
  project_name: string;
  ticket_id: string;
  title: string;
  stage: { id: string; name: string };
  /**
   * `"product-ok"` for a feature awaiting a product approver; `"owner"` for other asks;
   * `"cc"` when a person cc'd the viewer in a comment they haven't read yet.
   */
  reason: "product-ok" | "owner" | "cc";
  /** Everyone the ask is routed to; any one of them answering clears it for all. */
  responders: string[];
  /** Activity position of the ask's question (or of the label change), to scroll to. */
  ask_position: number | null;
  raised_at: number;
  raised_by_session_id: string | null;
  /** The person who raised it, when a person rather than an agent did. */
  raised_by_user_id?: string | null;
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

/** The one ticket a session works on. */
export interface SessionTicket {
  ticket_id: string;
  title: string;
  project_id: string;
  project_name: string;
}

/** Map session ids to the single ticket each is linked to; others are omitted. */
export async function listSessionTickets(
  sessionIds: readonly string[],
): Promise<Record<string, SessionTicket>> {
  if (sessionIds.length === 0) return {};
  const query = new URLSearchParams(sessionIds.map((id) => ["session_id", id]));
  const response = await authenticatedFetch(`/v1/inbox/ticket-requests/sessions?${query}`);
  if (response.status === 404) return {};
  if (!response.ok) throw await apiErrorFromResponse(response);
  return ((await response.json()) as { data: Record<string, SessionTicket> }).data;
}
