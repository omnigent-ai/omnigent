import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

/** The Summary page's time windows, in UTC days. */
export type ProjectStatusWindow = "7d" | "30d" | "all";

export const PROJECT_STATUS_WINDOWS: readonly { id: ProjectStatusWindow; label: string }[] = [
  { id: "7d", label: "7 days" },
  { id: "30d", label: "30 days" },
  { id: "all", label: "All time" },
];

/** A metric is `null` when its source isn't available on this server, never 0. */
export interface ProjectStatusTotals {
  tickets_done: number;
  /** Tickets created in the window, counted for who requested them. */
  tickets_submitted?: number;
  prs_merged: number | null;
  /** Distinct commits landed in the window; null when the project never landed one. */
  changes_landed?: number | null;
  /** Lines those commits added and removed, generated files left out; null until counted. */
  changes_landed_lines_added?: number | null;
  changes_landed_lines_removed?: number | null;
  /** How many of the window's landed commits have line counts. */
  changes_landed_lines_counted?: number;
  spend_cents: number | null;
  cost_per_merged_pr_cents: number | null;
  members: number;
}

/** One person, persona or the pooled ad-hoc agent workers. */
export interface ProjectStatusMember {
  key: string;
  kind: "human" | "identity" | "agents";
  name: string;
  user_id: string | null;
  identity_id: string | null;
  tickets_done: number;
  /** Tickets this person requested in the window; 0 for agents. */
  tickets_submitted?: number;
  prs_merged: number | null;
  spend_cents: number | null;
  cost_per_merged_pr_cents: number | null;
  /** Lines added and removed by landed commits credited to this member; null until counted. */
  lines_added?: number | null;
  lines_removed?: number | null;
}

export interface ProjectStatusSummary {
  object: "project.status_summary";
  project_id: string;
  window: ProjectStatusWindow;
  time_zone: "UTC";
  today_utc: string;
  since_day_utc: string | null;
  generated_at: number;
  totals: ProjectStatusTotals;
  members: ProjectStatusMember[];
  sources: { spend: "available" | "unavailable"; prs_merged: "available" | "unavailable" };
  description?: string | null;
}

/** Cache key shared with the description dialog, which refreshes it after a save. */
export function projectStatusSummaryKey(projectId: string | null, window?: ProjectStatusWindow) {
  return window
    ? (["project-status-summary", projectId, window] as const)
    : (["project-status-summary", projectId] as const);
}

export async function getProjectStatusSummary(
  projectId: string,
  window: ProjectStatusWindow,
  signal?: AbortSignal,
): Promise<ProjectStatusSummary> {
  const res = await authenticatedFetch(
    `/v1/projects/${encodeURIComponent(projectId)}/status/summary?window=${window}`,
    { signal },
  );
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ProjectStatusSummary;
}

/** Whole cents as dollars, e.g. 123456 -> "$1,234.56"; `null` (no source) -> "—". */
export function formatCents(cents: number | null): string {
  if (cents === null) return "—";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(cents / 100);
}

/**
 * Why "PRs merged" reads 0 while tickets still finished in the window: only pull requests
 * linked to a ticket or session count, and work that lands as direct commits links none.
 */
export function prsMergedZeroNote(
  prsMerged: number | null | undefined,
  ticketsDone: number | null | undefined,
): string | undefined {
  return prsMerged === 0 && (ticketsDone ?? 0) > 0
    ? "No finished ticket links a pull request"
    : undefined;
}

/** Added and removed as two numbers, never one total, e.g. "+1,234 / −56". */
export function formatLines(added: number | null | undefined, removed: number | null | undefined) {
  if (added == null || removed == null) return "—";
  const count = new Intl.NumberFormat("en-US");
  return `+${count.format(added)} / −${count.format(removed)}`;
}

export type ProjectStatusSortKey =
  | "name"
  | "tickets_done"
  | "prs_merged"
  | "lines_added"
  | "spend_cents"
  | "cost_per_merged_pr_cents";

/** Sort member rows by a column; missing values sort last in either direction. */
export function sortStatusMembers(
  members: readonly ProjectStatusMember[],
  key: ProjectStatusSortKey,
  direction: "asc" | "desc",
): ProjectStatusMember[] {
  const sign = direction === "asc" ? 1 : -1;
  return [...members].sort((left, right) => {
    if (key === "name") return sign * left.name.localeCompare(right.name);
    const a = left[key] ?? null;
    const b = right[key] ?? null;
    if (a === null && b === null) return left.name.localeCompare(right.name);
    if (a === null) return 1;
    if (b === null) return -1;
    return sign * (a - b) || left.name.localeCompare(right.name);
  });
}
