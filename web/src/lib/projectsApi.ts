/**
 * Typed client for the first-class `/v1/projects/*` endpoints.
 * Mirrors `omnigent/server/routes/projects.py`.
 *
 * A project is a shareable collection of chats; a chat filed under a project
 * (its `project_id`) inherits the project's ACL, so sharing a project covers
 * every chat in it — current and future. Two "everyone" scopes exist, matching
 * the share-modal toggles:
 *   - {@link MEMBERS_USER} — every signed-in member.
 *   - {@link PUBLIC_USER} — anyone with a chat's link (anonymous).
 */

import { authenticatedFetch } from "./identity";

/** Sentinel grantee: every signed-in member (current + future). */
export const MEMBERS_USER = "__members__";
/** Sentinel grantee: anyone with the link, including logged-out visitors. */
export const PUBLIC_USER = "__public__";

/** A project plus the calling user's effective access. Mirrors `ProjectObject`. */
export interface ProjectSummary {
  id: string;
  name: string;
  created_by: string | null;
  created_at: number;
  updated_at: number;
  /** Caller's effective level: 1=read, 2=edit, 3=manage, 4=owner. */
  permission_level: number | null;
  /** Shared with all signed-in members. */
  members: boolean;
  /** Shared via public link. */
  public: boolean;
}

/** One grantee on a project. */
export interface ProjectMember {
  user_id: string;
  level: number;
}

async function readJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

const base = (projectId: string) => `/v1/projects/${encodeURIComponent(projectId)}`;

export async function listProjects(): Promise<ProjectSummary[]> {
  return readJson(await authenticatedFetch("/v1/projects"));
}

export async function createProject(name: string): Promise<ProjectSummary> {
  return readJson(
    await authenticatedFetch("/v1/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  );
}

export async function renameProject(projectId: string, name: string): Promise<ProjectSummary> {
  return readJson(
    await authenticatedFetch(base(projectId), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  );
}

export async function deleteProject(projectId: string): Promise<void> {
  const res = await authenticatedFetch(base(projectId), { method: "DELETE" });
  if (!res.ok && res.status !== 204) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
  }
}

export async function listProjectMembers(projectId: string): Promise<ProjectMember[]> {
  return readJson(await authenticatedFetch(`${base(projectId)}/permissions`));
}

/**
 * Share a project. Pass a sentinel ({@link MEMBERS_USER} / {@link PUBLIC_USER})
 * at level 1 for the "everyone" scopes, or a real user id to invite one person.
 */
export async function shareProject(
  projectId: string,
  userId: string,
  level: number,
): Promise<ProjectSummary> {
  return readJson(
    await authenticatedFetch(`${base(projectId)}/permissions`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, level }),
    }),
  );
}

export async function unshareProject(projectId: string, userId: string): Promise<void> {
  const res = await authenticatedFetch(
    `${base(projectId)}/permissions/${encodeURIComponent(userId)}`,
    { method: "DELETE" },
  );
  if (!res.ok && res.status !== 204) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
  }
}

/** Leave a project: drop the caller's own (non-owner) grant. */
export async function leaveProject(projectId: string): Promise<void> {
  const res = await authenticatedFetch(`${base(projectId)}/membership`, { method: "DELETE" });
  if (!res.ok && res.status !== 204) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
  }
}
