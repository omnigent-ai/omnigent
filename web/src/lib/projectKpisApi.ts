import { authenticatedFetch } from "./identity";
import type { ProjectStatusWindow } from "./projectStatusApi";
import { apiErrorFromResponse } from "./sessionsApi";

export type KpiMetricId =
  | "merged_prs"
  | "spend_usd"
  | "tickets_shipped"
  | "rework_tickets"
  | "time_to_merged_median_hours"
  | "time_to_merged_p90_hours"
  | "none";
export type KpiDirection = "at_least" | "at_most";
export type KpiUnit = "count" | "usd" | "hours";
/** met / missed against the target; the rest say why there's no verdict. */
export type KpiStatus = "met" | "missed" | "no_data" | "unavailable" | "not_measured";

export const PROJECT_KPIS_MAX = 6;
export const UNMEASURED_METRIC: KpiMetricId = "none";

/** One KPI as stored and as sent to the server. */
export interface ProjectKpiDefinition {
  name: string;
  metric: KpiMetricId;
  why: string;
  target?: number | null;
  direction?: KpiDirection | null;
  not_measured_reason?: string | null;
}

export interface ProjectKpiTrend {
  since_day_utc: string;
  until_day_utc: string;
  /** From since_day_utc through today; today is last. */
  days: { day_utc: string; value: number | null }[];
  last_7_days: number | null;
  last_7_days_sample_size: number | null;
  today: number | null;
  /** The target check over the 7 full days ending yesterday. */
  status: KpiStatus;
}

export interface ProjectKpiValue extends ProjectKpiDefinition {
  position: number;
  metric_label: string | null;
  unit: KpiUnit | null;
  value: number | null;
  sample_size: number | null;
  status: KpiStatus;
  trend: ProjectKpiTrend | null;
}

export interface KpiMetric {
  id: KpiMetricId;
  label: string;
  unit: KpiUnit;
  description: string;
}

export interface ProjectKpis {
  object: "project.kpis";
  /** The role these KPIs belong to; absent or `null` for the project's own. */
  identity_id?: string | null;
  project_id: string;
  window: ProjectStatusWindow;
  today_utc: string;
  since_day_utc: string | null;
  generated_at: number;
  /** 0 when KPIs were never set. */
  revision: number;
  updated_at: number | null;
  updated_by_user_id: string | null;
  updated_by_session_id: string | null;
  can_edit: boolean;
  kpis: ProjectKpiValue[];
  metrics: KpiMetric[];
}

export function projectKpisKey(
  projectId: string | null,
  window?: ProjectStatusWindow,
  identityId?: string,
) {
  if (!window) return ["project-kpis", projectId] as const;
  return identityId
    ? (["project-kpis", projectId, window, identityId] as const)
    : (["project-kpis", projectId, window] as const);
}

function kpisPath(projectId: string, window: ProjectStatusWindow, identityId?: string) {
  const project = `/v1/projects/${encodeURIComponent(projectId)}`;
  const scope = identityId ? `${project}/identities/${encodeURIComponent(identityId)}` : project;
  return `${scope}/kpis?window=${window}`;
}

/** The project's KPIs, or one role's when `identityId` is given. */
export async function getProjectKpis(
  projectId: string,
  window: ProjectStatusWindow,
  signal?: AbortSignal,
  identityId?: string,
): Promise<ProjectKpis> {
  const res = await authenticatedFetch(kpisPath(projectId, window, identityId), { signal });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ProjectKpis;
}

/** Replace the set as a new revision; refused (409) when `baseRevision` is stale. */
export async function setProjectKpis(
  projectId: string,
  kpis: ProjectKpiDefinition[],
  baseRevision: number,
  window: ProjectStatusWindow,
  identityId?: string,
): Promise<ProjectKpis> {
  const res = await authenticatedFetch(kpisPath(projectId, window, identityId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kpis, base_revision: baseRevision }),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ProjectKpis;
}

const COUNT = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
const DOLLARS = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

/** A value in its unit, e.g. "12", "$4.50" or "3.5 h"; `null` -> "—". */
export function formatKpiValue(value: number | null | undefined, unit: KpiUnit | null): string {
  if (value === null || value === undefined) return "—";
  if (unit === "usd") return DOLLARS.format(value);
  if (unit === "hours") return `${COUNT.format(value)} h`;
  return COUNT.format(value);
}

/** The target in words, e.g. "at least 10" or "at most $50.00". */
export function formatKpiTarget(kpi: ProjectKpiValue): string | null {
  if (kpi.target === null || kpi.target === undefined || !kpi.direction) return null;
  const word = kpi.direction === "at_least" ? "at least" : "at most";
  return `${word} ${formatKpiValue(kpi.target, kpi.unit)}`;
}

/** Strip a stored KPI back to the fields the server accepts. */
export function kpiDefinition(kpi: ProjectKpiDefinition): ProjectKpiDefinition {
  if (kpi.metric === UNMEASURED_METRIC)
    return {
      name: kpi.name,
      metric: kpi.metric,
      why: kpi.why,
      not_measured_reason: kpi.not_measured_reason ?? "",
      ...(kpi.target !== null && kpi.target !== undefined && kpi.direction
        ? { target: kpi.target, direction: kpi.direction }
        : {}),
    };
  return {
    name: kpi.name,
    metric: kpi.metric,
    why: kpi.why,
    target: kpi.target ?? null,
    direction: kpi.direction ?? null,
  };
}
