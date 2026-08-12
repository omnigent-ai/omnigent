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

/**
 * Default session settings a project stores, pre-filled into the new-chat
 * composer. All fields optional — an unset key means "no default for this
 * slot". The vocabulary is client-owned; the server persists the object whole
 * and never acts on it, so adding a key here needs no backend change.
 */
export interface ProjectConfig {
  /** Default host id, or the sandbox sentinel. */
  host_id?: string;
  /** Default working directory / repo path on that host. */
  workspace?: string;
  /** Additional directory roots attached to each new session. */
  directories?: ProjectDirectoryDefault[];
  /** Default agent id for new sessions. */
  agent_id?: string;
  /**
   * Opt-in worktree default: only `true` is meaningful. When `true`, a new
   * session in a git workspace starts in a fresh randomly-named worktree; unset
   * (the only other value the dialog stores) starts directly in the workspace.
   * `false` is never written and is treated the same as unset.
   */
  use_worktree?: boolean;
  /**
   * Default base branch a new worktree forks from, pre-filled into the
   * composer's base-branch field. Takes precedence over the user-global default
   * (Settings › Git); an unset key falls through to that global default. Blank
   * is never stored (treated the same as unset).
   */
  base_branch?: string;
}

/** One additional directory stored as a project default. */
export interface ProjectDirectoryDefault {
  path: string;
}

/** Session creation accepts at most fifteen roots in addition to the workspace. */
export const MAX_PROJECT_ADDITIONAL_DIRECTORIES = 15;

/**
 * Normalize project directory defaults before storing or applying them.
 *
 * Project config is intentionally an opaque server-side JSON object, so data
 * read from it still needs a defensive client boundary. Invalid and duplicate
 * paths are dropped, the primary workspace is excluded, and the result is
 * capped to the session-create limit.
 */
export function normalizeProjectDirectories(
  directories: unknown,
  workspace?: string,
): ProjectDirectoryDefault[] {
  if (!Array.isArray(directories)) return [];

  const normalizePath = (value: unknown): string | null => {
    if (typeof value !== "string") return null;
    const trimmed = value.trim();
    if (!trimmed.startsWith("/")) return null;
    const stripped = trimmed.replace(/\/+$/, "");
    return stripped === "" ? "/" : stripped;
  };

  const primary = normalizePath(workspace);
  const seen = new Set<string>();
  const normalized: ProjectDirectoryDefault[] = [];
  for (const value of directories) {
    const path = normalizePath(
      value && typeof value === "object" ? (value as { path?: unknown }).path : undefined,
    );
    if (path === null || path === primary || seen.has(path)) continue;
    seen.add(path);
    normalized.push({ path });
    if (normalized.length === MAX_PROJECT_ADDITIONAL_DIRECTORIES) break;
  }
  return normalized;
}

/** A first-class project. Mirrors the `ProjectObject` response shape. */
export interface Project {
  id: string;
  name: string;
  /** Owner user id; `null` in single-user / OSS mode. */
  user_id?: string | null;
  created_at?: number;
  updated_at?: number | null;
  /** Stored default session settings; `{}` when the project has none. */
  config?: ProjectConfig;
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

/** Fetch a single project (including its `config`) by id. 404s if not owned. */
export async function getProject(id: string): Promise<Project> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}`);
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Project;
}

/**
 * Create a project, optionally with initial `config` defaults. Rejects with the
 * server's message on a duplicate name (409) so callers can surface it inline.
 */
export async function createProject(name: string, config?: ProjectConfig): Promise<Project> {
  const res = await authenticatedFetch("/v1/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config ? { name, config } : { name }),
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
 * Replace a project's stored `config` defaults. Passing `{}` clears them
 * (the server treats `{}` as "clear", distinct from omitting the field, which
 * leaves config unchanged). Only `config` is sent, so the name is untouched.
 */
export async function updateProjectConfig(id: string, config: ProjectConfig): Promise<Project> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config }),
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
