import { attachmentFilename } from "./attachments";
import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

export type TicketStageColor =
  "gray" | "blue" | "purple" | "pink" | "red" | "orange" | "amber" | "green";
export type TicketStageIconName =
  | "circle-dashed"
  | "circle"
  | "circle-dot"
  | "circle-check"
  | "clipboard-list"
  | "eye"
  | "clock"
  | "pause"
  | "search"
  | "flag"
  | "wrench"
  | "rocket";

export interface TicketStage {
  id: string;
  name: string;
  instructions: string;
  is_complete: boolean;
  color?: TicketStageColor | null;
  icon?: TicketStageIconName | null;
}
export interface TicketStages {
  stages: TicketStage[];
  initial_stage_id: string;
  occupied_stage_ids?: string[];
}
export interface TicketStageChanges extends Omit<TicketStages, "occupied_stage_ids"> {
  moves: Record<string, string>;
}

const LEGACY_STATUS_LABELS: Record<string, string> = {
  open: "Open",
  planning: "Planning",
  in_progress: "Working",
  review: "Review",
  done: "Done",
};
/** Defaults only for label-only projects and servers predating customizable stages. */
export const DEFAULT_TICKET_STAGES: TicketStages = {
  stages: Object.entries(LEGACY_STATUS_LABELS).map(([id, name]) => ({
    id,
    name,
    instructions: "",
    is_complete: id === "done",
  })),
  initial_stage_id: "open",
};
export interface TicketLabel {
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
  stage_id: string;
  labels: TicketLabel[];
  /** Latest linked-agent activity; absent or null means unknown. */
  has_running_agents?: boolean | null;
  /** Authorized linked-agent identities, batched with the ticket page. */
  agents?: { agent_name: string; harness: string | null }[];
  agents_has_more?: boolean;
  latest_update: string;
  comment_count: number;
  created_at: number;
  updated_at: number;
  /** The viewer's last-seen activity position; null when never opened. Absent for agents. */
  viewer_seen_position?: number | null;
  /** Comments and updates by others after the viewer's last-seen position. */
  new_comment_count?: number;
  new_update_count?: number;
  /** Position of the first new comment or update, or null when nothing is new. */
  first_new_position?: number | null;
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
export interface TicketSummary {
  needs_response_count: number;
  is_complete: boolean;
}
export interface EventPage {
  data: TicketEvent[];
  has_more: boolean;
  last_position: number;
}
export interface TicketSession {
  session_id: string;
  created_at: number;
  involvement?: string;
}
export type TicketChanges = Partial<
  Pick<ProjectTicket, "title" | "description" | "stage_id" | "labels">
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
  options: { stage_id?: string; after?: string; limit?: number; legacy?: boolean } = {},
) {
  const params = new URLSearchParams({ limit: String(options.limit ?? 50) });
  if (options.stage_id) params.set(options.legacy ? "status" : "stage_id", options.stage_id);
  if (options.after) params.set("after", options.after);
  return request<TicketPage<ProjectTicket>>(`${path(projectId)}?${params}`).then((page) => ({
    ...page,
    data: page.data.map(normalizeTicket),
  }));
}
export function getTicket(projectId: string, ticketId: string) {
  return request<ProjectTicket>(path(projectId, ticketId)).then(normalizeTicket);
}

export function getTicketSummary(projectId: string) {
  return request<TicketSummary>(`${path(projectId)}/summary`);
}

function normalizeTicket(ticket: ProjectTicket & { status?: string }): ProjectTicket {
  const stageId = ticket.stage_id ?? ticket.status;
  if (!stageId) throw new Error("The server returned a ticket without a stage.");
  return { ...ticket, stage_id: stageId };
}

export function getTicketStages(projectId: string, includeUsage = false) {
  return request<TicketStages>(`${path(projectId)}/stages?include_usage=${includeUsage}`);
}

export function updateTicketStages(projectId: string, body: TicketStageChanges) {
  return request<TicketStages>(`${path(projectId)}/stages`, "PUT", body);
}
export function createTicket(
  projectId: string,
  body: { title: string; description?: string; labels?: TicketLabel[] },
) {
  return request<ProjectTicket>(path(projectId), "POST", body).then(normalizeTicket);
}
export function updateTicket(
  projectId: string,
  ticketId: string,
  body: TicketChanges,
  legacy = false,
) {
  const { stage_id, ...fields } = body;
  return request<ProjectTicket>(
    path(projectId, ticketId),
    "PATCH",
    legacy ? { ...fields, ...(stage_id ? { status: stage_id } : {}) } : body,
  ).then(normalizeTicket);
}
export function commentOnTicket(projectId: string, ticketId: string, body: TicketComment) {
  return request<ProjectTicket>(`${path(projectId, ticketId)}/comments`, "POST", body).then(
    normalizeTicket,
  );
}
export function markTicketSeen(projectId: string, ticketId: string, seenPosition: number) {
  return request<{ viewer_seen_position: number }>(
    `${path(projectId, ticketId)}/read-state`,
    "PUT",
    { seen_position: seenPosition },
  );
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

export function setTicketSessionInvolvement(
  projectId: string,
  ticketId: string,
  sessionId: string,
  involvement: string,
) {
  return request<ProjectTicket>(`${path(projectId, ticketId)}/sessions`, "POST", {
    session_id: sessionId,
    action: "attach",
    involvement,
  }).then(normalizeTicket);
}

export function listRelatedTickets(projectId: string, ticketId: string, after?: string) {
  const params = new URLSearchParams({ limit: "50" });
  if (after) params.set("after", after);
  return request<TicketPage<ProjectTicket>>(`${path(projectId, ticketId)}/related?${params}`).then(
    (page) => ({
      ...page,
      data: page.data.map(normalizeTicket),
    }),
  );
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
  }).then(normalizeTicket);
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

/** The current response contact is the session that raised the unresolved label. */
export function ticketResponseSession(
  ticket: Pick<ProjectTicket, "labels" | "updated_at">,
  pages: readonly EventPage[],
): string | null {
  const current = ticket.labels.find((label) => label.key === "needs-human");
  const lastPage = pages.at(-1);
  if (!current || !lastPage || lastPage.has_more) return null;
  const events = pages.flatMap((page) => page.data);
  if (events[0]?.position !== 0) return null;
  if ((events.at(-1)?.created_at ?? 0) < ticket.updated_at) return null;
  let active: TicketLabel | undefined;
  let requester: string | null = null;
  for (const event of events) {
    const changes = event.payload.changes as { labels?: unknown } | undefined;
    const labels =
      event.kind === "created"
        ? event.payload.labels
        : event.kind === "updated"
          ? changes?.labels
          : undefined;
    if (!Array.isArray(labels)) continue;
    const next = labels.find((label): label is TicketLabel => label?.key === "needs-human");
    if (!next) requester = null;
    else if (!active) requester = event.session_id;
    active = next;
  }
  return active && (active.value ?? "") === (current.value ?? "") ? requester : null;
}

/** Snapshots retain the stage's name, meaning and appearance even after removal. */
export function ticketEventStage(event: TicketEvent): TicketStage | null {
  const snapshot = event.payload.stage as Partial<TicketStage> | undefined;
  if (snapshot && typeof snapshot.id === "string" && typeof snapshot.name === "string")
    return {
      id: snapshot.id,
      name: snapshot.name,
      instructions: "",
      is_complete: snapshot.is_complete === true,
      color: snapshot.color,
      icon: snapshot.icon,
    };
  const changes = event.payload.changes as { stage_id?: unknown; status?: unknown } | undefined;
  if (typeof changes?.stage_id === "string")
    return { id: changes.stage_id, name: changes.stage_id, instructions: "", is_complete: false };
  if (typeof changes?.status === "string" && LEGACY_STATUS_LABELS[changes.status])
    return {
      id: changes.status,
      name: LEGACY_STATUS_LABELS[changes.status],
      instructions: "",
      is_complete: changes.status === "done",
    };
  return null;
}

export interface TicketImage {
  id: string;
  filename: string;
  url: string;
}

/** Upload a pasted or dropped image for use in a ticket comment or description. */
export async function uploadTicketImage(projectId: string, file: File): Promise<TicketImage> {
  const form = new FormData();
  form.append("file", file, attachmentFilename(file));
  const response = await authenticatedFetch(
    `/v1/projects/${encodeURIComponent(projectId)}/ticket-attachments`,
    { method: "POST", body: form },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as TicketImage;
}
