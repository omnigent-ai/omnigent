import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

export const TICKET_STATUSES = ["open", "planning", "in_progress", "review", "done"] as const;
export type TicketStatus = (typeof TICKET_STATUSES)[number];
export const TICKET_STATUS_LABELS: Record<TicketStatus, string> = {
  open: "Open",
  planning: "Planning",
  in_progress: "Working",
  review: "Review",
  done: "Done",
};
export interface TicketTag {
  key: string;
  value?: string | null;
}
export interface TicketOutput {
  type: "pull_request" | "link";
  url: string;
}
export interface ProjectTicket {
  id: string;
  project_id: string;
  title: string;
  description: string;
  status: TicketStatus;
  tags: TicketTag[];
  completion_summary: string | null;
  plan?: string;
  review_summary?: string;
  lead_session_id?: string | null;
  latest_update: string;
  comment_count: number;
  created_at: number;
  updated_at: number;
}
export interface TicketEvent {
  id: string;
  ticket_id: string;
  position: number;
  kind: "created" | "updated" | "comment" | "execution" | "output";
  user_id: string | null;
  session_id: string | null;
  agent_id: string | null;
  payload: Record<string, unknown>;
  created_at: number;
}
export interface TicketPage<T> {
  data: T[];
  has_more: boolean;
  last_id: string | null;
}
export interface EventPage {
  data: TicketEvent[];
  has_more: boolean;
  last_position: number;
}
export interface TicketSession {
  session_id: string;
  created_at: number;
}
export type TicketChanges = Partial<
  Pick<
    ProjectTicket,
    | "title"
    | "description"
    | "status"
    | "tags"
    | "completion_summary"
    | "plan"
    | "review_summary"
    | "lead_session_id"
  >
> & { comment?: string };
export interface TicketComment {
  text: string;
  kind?: "comment" | "execution";
  outputs?: TicketOutput[];
}

function path(projectId: string, ticketId?: string): string {
  return `/v1/projects/${encodeURIComponent(projectId)}/tickets${ticketId ? `/${encodeURIComponent(ticketId)}` : ""}`;
}

async function request<T>(url: string, method = "GET", body?: unknown): Promise<T> {
  const response = await authenticatedFetch(url, {
    method,
    ...(body === undefined
      ? {}
      : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as T;
}

export function listTickets(
  projectId: string,
  options: { status?: TicketStatus; after?: string; limit?: number } = {},
) {
  const params = new URLSearchParams({ limit: String(options.limit ?? 50) });
  if (options.status) params.set("status", options.status);
  if (options.after) params.set("after", options.after);
  return request<TicketPage<ProjectTicket>>(`${path(projectId)}?${params}`);
}
export function getTicket(projectId: string, ticketId: string) {
  return request<ProjectTicket>(path(projectId, ticketId));
}
export function createTicket(
  projectId: string,
  body: { title: string; description?: string; tags?: TicketTag[] },
) {
  return request<ProjectTicket>(path(projectId), "POST", body);
}
export function updateTicket(projectId: string, ticketId: string, body: TicketChanges) {
  return request<ProjectTicket>(path(projectId, ticketId), "PATCH", body);
}
export function commentOnTicket(projectId: string, ticketId: string, body: TicketComment) {
  return request<ProjectTicket>(`${path(projectId, ticketId)}/comments`, "POST", body);
}
export function listTicketEvents(
  projectId: string,
  ticketId: string,
  after = -1,
  kind?: TicketEvent["kind"],
) {
  const params = new URLSearchParams({ after: String(after), limit: "50" });
  if (kind) params.set("kind", kind);
  return request<EventPage>(`${path(projectId, ticketId)}/events?${params}`);
}
export function listTicketSessions(projectId: string, ticketId: string, after?: string) {
  const params = new URLSearchParams({ limit: "50" });
  if (after) params.set("after", after);
  return request<TicketPage<TicketSession>>(`${path(projectId, ticketId)}/sessions?${params}`);
}

export function listRelatedTickets(projectId: string, ticketId: string, after?: string) {
  const params = new URLSearchParams({ limit: "50" });
  if (after) params.set("after", after);
  return request<TicketPage<ProjectTicket>>(`${path(projectId, ticketId)}/related?${params}`);
}

export function linkRelatedTicket(
  projectId: string,
  ticketId: string,
  relatedTicketId: string,
  action: "attach" | "remove",
) {
  return request<ProjectTicket>(`${path(projectId, ticketId)}/related`, "POST", {
    related_ticket_id: relatedTicketId,
    action,
  });
}

/** PRs are output events, not mutable associations; preserve all events in activity. */
export function ticketOutputs(events: TicketEvent[]): TicketOutput[] {
  const outputs = new Map<string, TicketOutput>();
  for (const { kind, payload } of events) {
    if (kind !== "output" || typeof payload.url !== "string" || !/^https?:\/\//i.test(payload.url))
      continue;
    if (payload.type !== "pull_request" && payload.type !== "link") continue;
    const output: TicketOutput = { type: payload.type, url: payload.url };
    outputs.set(`${output.type}:${output.url}`, output);
  }
  return [...outputs.values()];
}
