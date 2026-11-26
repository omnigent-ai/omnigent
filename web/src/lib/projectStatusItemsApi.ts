import { authenticatedFetch } from "./identity";
import type { KpiUnit } from "./projectKpisApi";
import { apiErrorFromResponse } from "./sessionsApi";

/** The metrics a Summary number or KPI tile can drill into. */
export type StatusItemMetric =
  | "merged_prs"
  | "spend_usd"
  | "tickets_shipped"
  | "rework_tickets"
  | "time_to_merged_median_hours"
  | "time_to_merged_p90_hours"
  | "changes_landed"
  | "tickets_submitted";

/** 7d / 30d / all as on the Summary, or `trend`: the KPI tiles' 7 full days plus today. */
export type StatusItemRange = "7d" | "30d" | "all" | "trend";

export interface ProjectStatusItem {
  day_utc: string | null;
  at: number | null;
  kind: "ticket" | "pull_request" | "session";
  /** Ticket ID, PR URL or root session ID. */
  id: string;
  title: string | null;
  url: string | null;
  member: string | null;
  /** Hours for time metrics, USD for spend, else null. */
  value: number | null;
  detail?: { from_stage_id?: string; to_stage_id?: string } | null;
  /** False when the total counts it on a later day instead. */
  counted_in_total: boolean;
  reason?: string | null;
}

export interface ProjectStatusItems {
  object: "project.status_items";
  project_id: string;
  metric: StatusItemMetric;
  unit: KpiUnit;
  range: StatusItemRange;
  member: string | null;
  since_day_utc: string | null;
  today_utc: string;
  available: boolean;
  total: number | null;
  sample_size: number | null;
  days: { day_utc: string; value: number | null; items: number }[];
  items: ProjectStatusItem[];
  excluded: ProjectStatusItem[];
  truncated: boolean;
}

export interface StatusItemsQuery {
  metric: StatusItemMetric;
  range: StatusItemRange;
  member?: string | null;
}

export function projectStatusItemsKey(projectId: string, query: StatusItemsQuery) {
  return ["project-status-items", projectId, query.metric, query.range, query.member ?? null];
}

export async function getProjectStatusItems(
  projectId: string,
  query: StatusItemsQuery,
  signal?: AbortSignal,
): Promise<ProjectStatusItems> {
  const params = new URLSearchParams({ metric: query.metric, range: query.range });
  if (query.member) params.set("member", query.member);
  const res = await authenticatedFetch(
    `/v1/projects/${encodeURIComponent(projectId)}/status/items?${params}`,
    { signal },
  );
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ProjectStatusItems;
}
