import { authenticatedFetch } from "./identity";
import type { ProjectStatusWindow } from "./projectStatusApi";
import { apiErrorFromResponse } from "./sessionsApi";

/** A count is `null` when it isn't measured on this server (e.g. message authors in single-user mode). */
export interface TelemetryWorkStart {
  sessions_started_by_people: number;
  tickets_filed_directly: number;
  tickets_filed_from_a_chat: number;
  tickets_filed_by_agents: number;
  people: { session_first: number; ticket_first: number; both: number };
}

export interface TelemetryRoleInteraction {
  messages_to_lead: number | null;
  messages_to_roles: number | null;
  people_messaging_roles: number | null;
  side_chats_opened: number;
  side_chats_with_roles: number;
  ticket_comments_by_people: number;
  people_ticket_only: number | null;
}

export interface TelemetryTeamBuilding {
  roles: number;
  roles_created_by_lead: number;
  roles_created_by_hand: number;
  roles_created_in_window: number;
  roles_created_in_window_by_lead: number;
  roles_edited_after_creation: number;
  median_hours_to_last_role_edit: number | null;
}

export interface TelemetryThroughput {
  tickets_shipped: number | null;
  merged_prs: number | null;
  /** Distinct commits landed in the window; null when the project never landed one. */
  changes_landed?: number | null;
  /** Tickets reopened after finishing or sent back from review, in the window. */
  rework_tickets: number | null;
  rework_reopened_tickets?: number | null;
  rework_sent_back_tickets?: number | null;
  time_to_merged_median_hours: number | null;
  spend_usd: number | null;
  review_rounds_per_shipped_ticket: number | null;
  stalled_tickets: number;
}

export interface TelemetryPerson {
  user_id: string;
  sessions_started: number;
  tickets_filed: number;
  /** Null until people's messages are measured. */
  messages_to_lead: number | null;
  messages_to_roles: number | null;
  side_chats_opened: number;
  ticket_comments: number;
}

export interface ProjectTelemetry {
  object: "project.telemetry";
  project_id: string;
  project_name: string;
  window: ProjectStatusWindow;
  since_day_utc: string | null;
  today_utc: string;
  work_start: TelemetryWorkStart;
  role_interaction: TelemetryRoleInteraction;
  team_building: TelemetryTeamBuilding;
  throughput: TelemetryThroughput;
  /** Everyone for the owner, the viewer's own row for a member, null for agents. */
  people: TelemetryPerson[] | null;
}

export interface TelemetryOverview {
  object: "telemetry.overview";
  window: ProjectStatusWindow;
  since_day_utc: string | null;
  today_utc: string;
  projects: ProjectTelemetry[];
}

export function projectTelemetryKey(projectId: string | null, window: ProjectStatusWindow) {
  return ["project-telemetry", projectId, window] as const;
}

export function telemetryOverviewKey(window: ProjectStatusWindow) {
  return ["telemetry-overview", window] as const;
}

export async function getProjectTelemetry(
  projectId: string,
  window: ProjectStatusWindow,
  signal?: AbortSignal,
): Promise<ProjectTelemetry> {
  const res = await authenticatedFetch(
    `/v1/projects/${encodeURIComponent(projectId)}/telemetry?window=${window}`,
    { signal },
  );
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ProjectTelemetry;
}

export async function getTelemetryOverview(
  window: ProjectStatusWindow,
  signal?: AbortSignal,
): Promise<TelemetryOverview> {
  const res = await authenticatedFetch(`/v1/telemetry?window=${window}`, { signal });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as TelemetryOverview;
}

const NUMBER = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

/** A telemetry number, or a dash when it isn't measured. */
export function formatTelemetryNumber(value: number | null | undefined, suffix = ""): string {
  if (value === null || value === undefined) return "—";
  return `${NUMBER.format(value)}${suffix}`;
}

/** Sum a column across projects; null only when every project left it unmeasured. */
export function sumMeasured(values: readonly (number | null)[]): number | null {
  const measured = values.filter((value): value is number => value !== null);
  return measured.length ? measured.reduce((total, value) => total + value, 0) : null;
}
