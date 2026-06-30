/**
 * Typed client for the `/v1/sessions/projects/{name}/…` share endpoints.
 * Mirrors the project-share handlers in `omnigent/server/routes/sessions.py`.
 *
 * A project is an implicit collection of sessions sharing the same
 * `omni_project` label; these endpoints fan a single grant out across every
 * session in a project the caller can manage. Two "share with everyone" scopes
 * exist, matching the two modal toggles:
 *   - {@link MEMBERS_USER} — every signed-in Omnigent member sees the chats.
 *   - {@link PUBLIC_USER} — anyone with a chat's link can view it (anonymous).
 */

import { authenticatedFetch } from "./identity";

/** Sentinel grantee: every signed-in member (current + future). */
export const MEMBERS_USER = "__members__";
/** Sentinel grantee: anyone with the link, including logged-out visitors. */
export const PUBLIC_USER = "__public__";

/**
 * Aggregate share state of a project for the calling user. Mirrors
 * `ProjectShareStatus` in `omnigent/server/schemas.py`.
 */
export interface ProjectShareStatus {
  project: string;
  /** True when every manageable chat is shared with all signed-in members. */
  members: boolean;
  /** True when every manageable chat is shared via a public link. */
  public: boolean;
  /** Chats in the project the caller can manage. */
  manageable_count: number;
  /** Chats the last action touched (0 for a plain status read). */
  shared_count: number;
  /** Total non-archived chats in the project visible to the caller. */
  total_count: number;
  /** True when the caller holds a removable (non-owner) grant — can leave. */
  viewer_is_member: boolean;
}

/** One grantee on a project, aggregated across its sessions. */
export interface ProjectMember {
  user_id: string;
  /** Highest level held on any chat in the project (1=read…4=owner). */
  level: number;
  /** How many chats in the project carry a grant for this user. */
  session_count: number;
}

async function readJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

function base(projectName: string): string {
  return `/v1/sessions/projects/${encodeURIComponent(projectName)}`;
}

export async function getProjectShareStatus(projectName: string): Promise<ProjectShareStatus> {
  return readJson(await authenticatedFetch(`${base(projectName)}/share`));
}

export async function listProjectMembers(projectName: string): Promise<ProjectMember[]> {
  return readJson(await authenticatedFetch(`${base(projectName)}/members`));
}

/**
 * Fan a grant out to every chat in a project the caller can manage. Pass a
 * sentinel ({@link MEMBERS_USER} / {@link PUBLIC_USER}) at level 1 for the
 * "everyone" scopes, or a real user id to invite one person.
 */
export async function shareProject(
  projectName: string,
  userId: string,
  level: number,
): Promise<ProjectShareStatus> {
  return readJson(
    await authenticatedFetch(`${base(projectName)}/share`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, level }),
    }),
  );
}

/** Revoke a grantee from every chat in a project the caller can manage. */
export async function unshareProject(
  projectName: string,
  userId: string,
): Promise<ProjectShareStatus> {
  return readJson(
    await authenticatedFetch(`${base(projectName)}/share/${encodeURIComponent(userId)}`, {
      method: "DELETE",
    }),
  );
}

/** Leave a project: drop the caller's own (non-owner) grants across its chats. */
export async function leaveProject(projectName: string): Promise<ProjectShareStatus> {
  return readJson(
    await authenticatedFetch(`${base(projectName)}/membership`, { method: "DELETE" }),
  );
}
