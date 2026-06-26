/**
 * Client for the admin discovery API (``/v1/admin/*``).
 *
 * Powers the OIDC/SSO admin surface — a user list and an admin's view
 * of any user's sessions — where the accounts-mode Members page never
 * mounts. Every endpoint is gated on the caller's ``is_admin`` flag by
 * the server; these helpers resolve to ``null`` on any non-2xx so the
 * UI can render a friendly "no access / unreachable" state instead of
 * throwing.
 */

import { authenticatedFetch } from "./identity";

/** A user row from ``GET /v1/admin/users``. */
export interface AdminUser {
  user_id: string;
  is_admin: boolean;
}

/** A session row from ``GET /v1/admin/users/{id}/sessions``. */
export interface AdminSession {
  id: string;
  title: string | null;
  created_at: number;
  updated_at: number;
}

/**
 * GET /v1/admin/users — list every real user (admin only).
 *
 * :returns: The user list, or ``null`` on error / forbidden.
 */
export async function listAllUsers(): Promise<AdminUser[] | null> {
  try {
    const res = await authenticatedFetch("/v1/admin/users");
    if (!res.ok) return null;
    const data = (await res.json()) as { users: AdminUser[] };
    return data.users;
  } catch {
    return null;
  }
}

/**
 * GET /v1/admin/users/{id}/sessions — list a user's sessions (admin only).
 *
 * :param userId: The user whose sessions to list.
 * :returns: The session list, or ``null`` on error / forbidden.
 */
export async function listUserSessions(userId: string): Promise<AdminSession[] | null> {
  try {
    const res = await authenticatedFetch(`/v1/admin/users/${encodeURIComponent(userId)}/sessions`);
    if (!res.ok) return null;
    const data = (await res.json()) as { sessions: AdminSession[] };
    return data.sessions;
  } catch {
    return null;
  }
}
