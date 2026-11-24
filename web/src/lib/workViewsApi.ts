import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

export type ViewEntity = "tasks" | "projects" | "sessions";
export type TaskStatus = "backlog" | "todo" | "in_progress" | "review" | "done" | "canceled";
export type ViewProperty = "status" | "project" | "priority" | "created_at" | "updated_at";
export interface ViewFilters {
  search: string;
  statuses: string[];
  project_ids: string[];
  no_project: boolean;
  priorities: number[];
  updated_after: number | null;
  include_archived: boolean;
}
export interface ViewDisplay {
  layout: "list" | "board";
  group_by: "none" | "status" | "project" | "priority";
  order_by: "created_at" | "updated_at" | "title" | "priority";
  direction: "asc" | "desc";
  show_empty_groups: boolean;
  properties: ViewProperty[];
}
export interface ViewDefinition {
  entity_type: ViewEntity;
  filters: ViewFilters;
  display: ViewDisplay;
}
export interface ViewInput extends ViewDefinition {
  name: string;
  description: string;
  favorite: boolean;
}
export interface SavedWorkView extends ViewInput {
  id: string;
  created_at: number;
  updated_at: number;
}
export interface ViewRecord {
  id: string;
  title: string;
  status?: string;
  priority?: number;
  project_id?: string | null;
  project_name?: string | null;
  created_at: number;
  updated_at: number;
}
export interface WorkTaskInput {
  title: string;
  description: string;
  status: TaskStatus;
  priority: number;
  project_id: string | null;
}
export interface WorkTask extends Omit<ViewRecord, keyof WorkTaskInput>, WorkTaskInput {
  sessions: { session_id: string; title: string; status: string; involvement: string }[];
}
export interface ViewPage<T> {
  data: T[];
  has_more: boolean;
  next_cursor: string | null;
}

export function newView(entity: ViewEntity = "tasks"): ViewInput {
  return {
    name: `All ${entity}`,
    description: "",
    favorite: false,
    entity_type: entity,
    filters: {
      search: "",
      statuses: [],
      project_ids: [],
      no_project: false,
      priorities: [],
      updated_after: null,
      include_archived: false,
    },
    display: {
      layout: "list",
      group_by: entity === "projects" ? "none" : "status",
      order_by: "updated_at",
      direction: "desc",
      show_empty_groups: false,
      properties:
        entity === "projects"
          ? ["updated_at"]
          : entity === "sessions"
            ? ["status", "project", "updated_at"]
            : ["status", "project", "priority", "updated_at"],
    },
  };
}

async function request<T>(
  path: string,
  method = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await authenticatedFetch(`/v1/${path}`, {
    method,
    signal,
    ...(body === undefined
      ? {}
      : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json() as Promise<T>;
}

export const listWorkViews = (cursor: string | null = null) =>
  request<ViewPage<SavedWorkView>>(
    `work-views${cursor ? `?after=${encodeURIComponent(cursor)}` : ""}`,
  );
export const getWorkView = (id: string) =>
  request<SavedWorkView>(`work-views/${encodeURIComponent(id)}`);
export const saveWorkView = (body: ViewInput, id?: string) =>
  request<SavedWorkView>(
    id ? `work-views/${encodeURIComponent(id)}` : "work-views",
    id ? "PUT" : "POST",
    body,
  );
export const deleteWorkView = (id: string) =>
  request<{ deleted: boolean }>(`work-views/${encodeURIComponent(id)}`, "DELETE");
export const queryWorkView = (
  definition: ViewDefinition,
  cursor: string | null = null,
  signal?: AbortSignal,
  limit = 100,
) =>
  request<ViewPage<ViewRecord>>(
    "work-views/query",
    "POST",
    {
      entity_type: definition.entity_type,
      filters: definition.filters,
      display: definition.display,
      cursor,
      limit,
    },
    signal,
  );
export const getWorkTask = (id: string) =>
  request<WorkTask>(`work-tasks/${encodeURIComponent(id)}`);
export const saveWorkTask = (body: WorkTaskInput, id?: string) =>
  request<Omit<WorkTask, "sessions">>(
    id ? `work-tasks/${encodeURIComponent(id)}` : "work-tasks",
    id ? "PUT" : "POST",
    body,
  );
export const deleteWorkTask = (id: string) =>
  request<{ deleted: boolean }>(`work-tasks/${encodeURIComponent(id)}`, "DELETE");
export const linkWorkSession = (taskId: string, sessionId: string, involvement: string) =>
  request<{ linked: boolean }>(`work-tasks/${encodeURIComponent(taskId)}/sessions`, "POST", {
    session_id: sessionId,
    involvement,
  });
export const unlinkWorkSession = (taskId: string, sessionId: string) =>
  request<{ linked: boolean }>(
    `work-tasks/${encodeURIComponent(taskId)}/sessions/${encodeURIComponent(sessionId)}`,
    "DELETE",
  );
