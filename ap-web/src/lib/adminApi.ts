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

/** A user row from ``GET /v1/admin/users`` (with a usage rollup). */
export interface AdminUser {
  user_id: string;
  is_admin: boolean;
  cost_usd: number;
  total_tokens: number;
  session_count: number;
}

/** Response of ``GET /v1/admin/users``. */
export interface AdminUserList {
  users: AdminUser[];
  /** Count of invite-only phantom accounts filtered out of ``users``. */
  hidden: number;
}

/** A session row from ``GET /v1/admin/users/{id}/sessions``. */
export interface AdminSession {
  id: string;
  title: string | null;
  created_at: number;
  updated_at: number;
  cost_usd: number;
  total_tokens: number;
  /** This user's role on the session: "owner" | "manage" | "edit" | "read". */
  role: string | null;
  /** The session's owner (the LEVEL_OWNER grantee), or null if none. */
  owner: string | null;
  /** Whether this user is the session's owner. */
  is_owner: boolean;
}

/** Aggregate usage across a user's sessions. */
export interface UsageTotals {
  cost_usd: number;
  total_tokens: number;
  session_count: number;
}

/** Response of ``GET /v1/admin/users/{id}/sessions``. */
export interface AdminUserSessions {
  sessions: AdminSession[];
  totals: UsageTotals;
}

/**
 * GET /v1/admin/users — list every real user + the hidden-phantom count
 * (admin only).
 *
 * :returns: ``{users, hidden}``, or ``null`` on error / forbidden.
 */
export async function listAllUsers(): Promise<AdminUserList | null> {
  try {
    const res = await authenticatedFetch("/v1/admin/users");
    if (!res.ok) return null;
    const data = (await res.json()) as { users: AdminUser[]; hidden?: number };
    return { users: data.users, hidden: data.hidden ?? 0 };
  } catch {
    return null;
  }
}

/**
 * GET /v1/admin/users/{id}/sessions — list a user's sessions + usage totals
 * (admin only).
 *
 * :param userId: The user whose sessions to list.
 * :returns: The sessions + totals, or ``null`` on error / forbidden.
 */
export async function listUserSessions(userId: string): Promise<AdminUserSessions | null> {
  try {
    const res = await authenticatedFetch(`/v1/admin/users/${encodeURIComponent(userId)}/sessions`);
    if (!res.ok) return null;
    return (await res.json()) as AdminUserSessions;
  } catch {
    return null;
  }
}
