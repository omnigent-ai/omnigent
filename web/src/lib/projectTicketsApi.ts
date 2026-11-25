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
const DEFAULT_STAGE_IDS = ["open", "in_progress", "review", "done"];
/** Defaults only for label-only projects and servers predating customizable stages. */
export const DEFAULT_TICKET_STAGES: TicketStages = {
  stages: DEFAULT_STAGE_IDS.map((id) => [id, LEGACY_STATUS_LABELS[id]] as const).map(
    ([id, name]) => ({
      id,
      name,
      instructions: "",
      is_complete: id === "done",
    }),
  ),
  initial_stage_id: "open",
};
export interface TicketLabel {
  key: string;
  value?: string | null;
}
export interface TicketOutput {
  type: "pull_request" | "link" | "artifact";
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
  agents?: {
    agent_name: string;
    harness: string | null;
    /** The project persona the session works as, e.g. "Lead" or "Reviewer". */
    persona?: string | null;
  }[];
  agents_has_more?: boolean;
  latest_update: string;
  /**
   * Newest comment by a person or for the human; stage moves and label edits don't
   * replace it. Null when there is none; absent on single-ticket reads and older servers.
   */
  latest_comment?: TicketLatestComment | null;
  comment_count: number;
  /** Who asked for the work: a user ID, or `github:<login>` for an imported PR. */
  requested_by?: string | null;
  created_at: number;
  updated_at: number;
  /** The viewer's last-seen activity position; null when never opened. Absent for agents. */
  viewer_seen_position?: number | null;
  /** Human-facing comments by others after the viewer's last-seen position. */
  new_comment_count?: number;
  /** Position of the first new comment, or null when nothing is new. */
  first_new_position?: number | null;
  /** The pinned result while the ticket stays complete; only on single-ticket reads. */
  final_update?: TicketFinalUpdate | null;
  /** Complete without a final update yet; only on single-ticket reads. */
  needs_final_update?: boolean;
  /** Sign-off labels the open ask waits on that the viewer may add; single-ticket reads. */
  sign_offs?: TicketLabel[];
  /** Who the open needs-human ask waits on, routed as in the Inbox; null once answered. */
  waiting_on?: TicketWaitingOn | null;
}
export interface TicketLatestComment {
  text: string;
  user_id: string | null;
  session_id: string | null;
  /** Written by a person rather than an agent. */
  by_person: boolean;
  position: number;
  created_at: number;
}
export interface TicketWaitingOn {
  reason: string;
  responders: string[];
  /** The person who raised the ask, when a person did; it isn't waiting on them. */
  raised_by_user_id: string | null;
}
export interface TicketFinalUpdate {
  event_id: string;
  position: number;
  text: string;
  user_id: string | null;
  session_id: string | null;
  created_at: number;
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
  /** Every matching ticket, on a list's first page; absent on later pages and older servers. */
  total?: number;
}
export interface TicketSummary {
  needs_response_count: number;
  is_complete: boolean;
  /** Newest ticket's creation time (epoch seconds); absent on older servers. */
  latest_created_at?: number | null;
  /** Tickets created after the requested `created_after`; present only when it was sent. */
  created_after_count?: number;
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
> & {
  comment?: string;
  final?: boolean;
  /** Labels to add or replace by key, applied to the current labels in one server write. */
  add_labels?: TicketLabel[];
  /** Label keys to remove, applied in the same write. */
  remove_labels?: string[];
};

/** The adds and removes that turn *before* into *after*, so concurrent label edits survive. */
export function labelChanges(
  before: TicketLabel[],
  after: TicketLabel[],
): Pick<TicketChanges, "add_labels" | "remove_labels"> {
  const valueOf = (label: TicketLabel) => label.value ?? null;
  const previous = new Map(before.map((label) => [label.key, valueOf(label)]));
  const kept = new Set(after.map((label) => label.key));
  return {
    add_labels: after.filter(
      (label) => !previous.has(label.key) || previous.get(label.key) !== valueOf(label),
    ),
    remove_labels: before.filter((label) => !kept.has(label.key)).map((label) => label.key),
  };
}
export interface TicketComment {
  text: string;
  kind?: "comment" | "execution";
  outputs?: TicketOutput[];
  final?: boolean;
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
  options: {
    stage_id?: string;
    after?: string;
    limit?: number;
    legacy?: boolean;
    /** Only tickets the viewer requested. */
    mine?: boolean;
  } = {},
) {
  const params = new URLSearchParams({ limit: String(options.limit ?? 50) });
  if (options.stage_id) params.set(options.legacy ? "status" : "stage_id", options.stage_id);
  if (options.after) params.set("after", options.after);
  if (options.mine) params.set("mine", "true");
  return request<TicketPage<ProjectTicket>>(`${path(projectId)}?${params}`).then((page) => ({
    ...page,
    data: page.data.map(normalizeTicket),
  }));
}
export function getTicket(projectId: string, ticketId: string) {
  return request<ProjectTicket>(path(projectId, ticketId)).then(normalizeTicket);
}

/**
 * The Merger's push-prefix report (`sys_ticket_push_prefix`): how much of the
 * integration branch's unpushed range can ship, and the commit that stops it.
 * Only the fields the board reads are typed; the report carries more.
 */
export interface PushPrefix {
  pushable_sha: string | null;
  /** The first commit that can't ship yet, usually a landed ticket not yet Verified. */
  stop: { sha: string; ticket_id: string | null; reason: string } | null;
  /** Verified tickets after the stop, held back by it. */
  blocked_verified_ticket_ids: string[];
  /** Each ticket in the unpushed range; `verified` means it's in a Verified stage. */
  tickets?: { ticket_id: string; verified: boolean }[];
}

/** The project's push-prefix report, or null where the server doesn't provide one. */
export async function getPushPrefix(projectId: string): Promise<PushPrefix | null> {
  const response = await authenticatedFetch(`${path(projectId)}/push-prefix`);
  if (response.status === 404) return null;
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as PushPrefix;
}

export function getTicketSummary(projectId: string) {
  return request<TicketSummary>(`${path(projectId)}/summary`);
}

/** Ticket creation for new-ticket badges, skipping the human-request scan. */
export function getTicketCreationSummary(projectId: string, createdAfter?: number) {
  const query = new URLSearchParams({ responses: "false" });
  if (createdAfter !== undefined) query.set("created_after", String(createdAfter));
  return request<Pick<TicketSummary, "latest_created_at" | "created_after_count">>(
    `${path(projectId)}/summary?${query}`,
  );
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
/** A summary of a ticket's history, shown only to the person who asked for it. */
export interface TicketHistorySummary {
  object: "project.ticket.summary";
  /** Whether this server has a model for summaries. */
  available: boolean;
  summary: {
    text: string;
    through_position: number;
    generated_at: number;
    model: string;
    cached: boolean;
  } | null;
  /** Activity followed the summary. */
  stale: boolean;
}
export function getTicketHistorySummary(projectId: string, ticketId: string) {
  return request<TicketHistorySummary>(`${path(projectId, ticketId)}/summary`);
}
export function summarizeTicketHistory(projectId: string, ticketId: string) {
  return request<TicketHistorySummary>(`${path(projectId, ticketId)}/summary`, "POST");
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
    if (kind !== "output" || typeof payload.url !== "string") continue;
    if (payload.type === "artifact") {
      if (!PROJECT_FILE_PATH.test(payload.url)) continue;
    } else if (payload.type !== "pull_request" && payload.type !== "link") continue;
    else if (!/^https?:\/\//i.test(payload.url)) continue;
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
  /** The stored type, e.g. ``"image/png"`` or ``"video/mp4"``; missing from older servers. */
  content_type?: string;
}

/** Upload a pasted, dropped or picked image or video for a ticket comment or description. */
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

/** Content path of a project file, the form an `artifact` ticket output stores. */
export const PROJECT_FILE_PATH = /^\/v1\/projects\/([0-9a-f]{32})\/files\/([0-9a-f]{32})\/content$/;

export interface ProjectFile {
  id: string;
  filename: string;
  title: string;
  bytes: number;
  content_type: string | null;
  created_at: number;
  kind: "artifact" | "ticket_attachment";
  ticket_id: string | null;
  url: string;
}

export async function listProjectFiles(projectId: string): Promise<ProjectFile[]> {
  const response = await authenticatedFetch(`/v1/projects/${encodeURIComponent(projectId)}/files`);
  if (!response.ok) throw await apiErrorFromResponse(response);
  return ((await response.json()) as { data: ProjectFile[] }).data;
}

export async function getProjectFile(contentPath: string): Promise<ProjectFile> {
  const response = await authenticatedFetch(contentPath.replace(/\/content$/, ""));
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as ProjectFile;
}

// Blob URLs share the app's origin, so only types that cannot run script open inline.
const INLINE_FILE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "application/pdf",
  "text/plain",
  "video/mp4",
  "video/webm",
]);

/** Dispatched on `window` to open a project file in the project page's viewer. */
export const OPEN_PROJECT_FILE_EVENT = "omnigent:open-project-file";

/** Open a project file in the project page's viewer, else preview or download it. */
export async function openProjectFile(contentPath: string, filename: string): Promise<void> {
  const openRequest = new CustomEvent(OPEN_PROJECT_FILE_EVENT, {
    cancelable: true,
    detail: { contentPath },
  });
  if (!window.dispatchEvent(openRequest)) return;
  const response = await authenticatedFetch(contentPath);
  if (!response.ok) throw await apiErrorFromResponse(response);
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const type = blob.type.split(";", 1)[0].trim().toLowerCase();
  if (INLINE_FILE_TYPES.has(type)) {
    window.open(objectUrl, "_blank", "noopener");
  } else {
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.click();
  }
  setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
}

/** A ticket requester for display: GitHub authors as `@login (GitHub PR)`, the viewer as "you". */
export function requesterLabel(
  requestedBy: string | null | undefined,
  viewer: string | null,
): string | null {
  if (!requestedBy) return null;
  if (requestedBy.startsWith("github:")) return `@${requestedBy.slice(7)} (GitHub PR)`;
  // An agent in another shared project filed it; the description names the project.
  if (requestedBy.startsWith("project:")) return "another project (agent)";
  return requestedBy === viewer ? "you" : requestedBy;
}
