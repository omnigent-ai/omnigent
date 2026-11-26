import { authenticatedFetch } from "./identity";
import type { ProjectStatusWindow } from "./projectStatusApi";
import { apiErrorFromResponse } from "./sessionsApi";

/** One chart bucket; a metric is `null` when its source isn't on this server. */
export interface ProjectStatusTrendDay {
  day_utc: string;
  tickets_filed: number;
  tickets_finished: number;
  open_tickets: number;
  changes_landed: number | null;
  prs_merged: number | null;
  spend_usd: number | null;
}

export interface ProjectStatusTrends {
  object: "project.status_trends";
  project_id: string;
  window: ProjectStatusWindow;
  today_utc: string;
  bucket: "day" | "week";
  days: ProjectStatusTrendDay[];
}

export function projectStatusTrendsKey(projectId: string | null, window: ProjectStatusWindow) {
  return ["project-status-trends", projectId, window] as const;
}

export async function getProjectStatusTrends(
  projectId: string,
  window: ProjectStatusWindow,
  signal?: AbortSignal,
): Promise<ProjectStatusTrends> {
  const res = await authenticatedFetch(
    `/v1/projects/${encodeURIComponent(projectId)}/status/trends?window=${window}`,
    { signal },
  );
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ProjectStatusTrends;
}
