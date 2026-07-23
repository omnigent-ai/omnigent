// Typed client for the `/v1/projects` first-class projects CRUD
// (`omnigent/server/routes/projects.py`). Projects are owner-private
// containers that group sessions and exist independently of their members —
// so they can be empty, renamed, and deleted without touching sessions.
//
// Session→project membership lives on the session, not here: file/unfile a
// session with `PATCH /v1/sessions/{id}` `{ project_id }` (see sessionsApi).
//
// All requests go through the existing Vite `/v1` proxy. TS surface is
// camelCase-friendly, but the project shape is already flat snake_case-free
// (`id`, `name`), so no boundary conversion is needed.

import { authenticatedFetch } from "./identity";

/** A first-class project. Mirrors the `ProjectObject` response shape. */
export interface Project {
  id: string;
  name: string;
  /** Owner user id; `null` in single-user / OSS mode. */
  owner_user_id?: string | null;
  created_at?: number;
  updated_at?: number | null;
}

interface ProjectListResponse {
  object: "list";
  data: Project[];
}

async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { error?: { message?: string }; message?: string };
    return body.error?.message ?? body.message ?? `${res.status} ${res.statusText}`;
  } catch {
    return `${res.status} ${res.statusText}`;
  }
}

/** List the caller's projects (owner-scoped), oldest first. */
export async function listProjects(): Promise<Project[]> {
  const res = await authenticatedFetch("/v1/projects");
  if (!res.ok) throw new Error(await readError(res));
  const body = (await res.json()) as ProjectListResponse;
  return body.data;
}

/**
 * Create an empty project. Rejects with the server's message on a duplicate
 * name (409) so callers can surface it inline.
 */
export async function createProject(name: string): Promise<Project> {
  const res = await authenticatedFetch("/v1/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Project;
}

/** Rename a project (O(1) — members reference the id, not the name string). */
export async function renameProject(id: string, name: string): Promise<Project> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Project;
}

/**
 * Delete a project. Only the container is removed; member sessions are kept
 * (never cascade-deleted). Their `project_id` is left dangling server-side, but
 * the dual-read listing joins against the (now-absent) project, so they surface
 * as unfiled. Returns 404 if not found / not owned.
 */
export async function deleteProject(id: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(await readError(res));
}
