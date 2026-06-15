/**
 * Typed client for the `/v1/sessions/{id}/permissions` endpoints.
 * Mirrors `omnigent/server/routes/sessions.py` permission handlers.
 */

import type { Conversation } from "@/hooks/useConversations";
import type { Session } from "@/lib/types";
import { authenticatedFetch } from "./identity";

/**
 * Numeric permission level of the session owner. Mirrors
 * ``LEVEL_OWNER`` in ``omnigent/server/auth.py`` (READ=1, EDIT=2,
 * MANAGE=3, OWNER=4).
 */
export const LEVEL_OWNER = 4;

/**
 * Return whether a permission level denotes the session owner.
 *
 * A ``null`` level means "unknown / single-user / still loading" and
 * is treated permissively (owner), matching the rest of the UI's
 * permissive-on-null stance (see ``derivePermissionLevel`` and
 * ``useCanEdit``). Used to gate owner-only affordances such as
 * typing into a session's shared terminal.
 *
 * :param level: The effective permission level, e.g. ``2`` for edit
 *     access, or ``null`` when unresolved.
 * :returns: ``true`` for the owner (level ``null`` or ``>= 4``).
 */
export function isOwnerLevel(level: number | null): boolean {
  return level == null || level >= LEVEL_OWNER;
}

/**
 * Derive the effective permission level for the active conversation.
 *
 * Resolution order:
 *
 * 1. ``session.permissionLevel`` — the snapshot fetched by
 *    ``chatStore.bindStream`` (via ``getSession``) is the authoritative
 *    source. Sub-agent (child) sessions are filtered out of the
 *    sidebar list query, so this is the only place their level is
 *    observable.
 * 2. ``activeConv.permission_level`` — sidebar list row. Available
 *    synchronously the moment the user navigates between top-level
 *    conversations, so we use it as a fast path before the single
 *    fetch resolves.
 * 3. ``null`` while the single fetch is still in flight (the UI
 *    treats ``null`` permissively, avoiding a read-only flicker
 *    during the snapshot's first round-trip on child sessions).
 * 4. Read-only (1) — final fallback when the URL points at a
 *    conversation the sidebar didn't return AND the single fetch
 *    either errored or hasn't been initiated.
 */
export function derivePermissionLevel(
  session: Session | null | undefined,
  sessionLoading: boolean,
  activeConv: Conversation | null | undefined,
  conversationId: string | undefined,
  conversationsLoaded: boolean,
): number | null {
  if (session != null) return session.permissionLevel;
  if (activeConv != null) return activeConv.permission_level ?? null;
  if (sessionLoading) return null;
  if (conversationId && conversationsLoaded) return 1;
  return null;
}

export interface Permission {
  user_id: string;
  conversation_id: string;
  level: number;
}

export async function listPermissions(sessionId: string): Promise<Permission[]> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/permissions`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as Permission[];
}

export async function getSessionOwner(sessionId: string): Promise<string | null> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/owner`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const data = (await res.json()) as { owner: string | null };
  return data.owner;
}

export async function grantPermission(
  sessionId: string,
  userId: string,
  level: number,
): Promise<Permission> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/permissions`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, level }),
    },
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as Permission;
}

export async function revokePermission(sessionId: string, userId: string): Promise<void> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/permissions/${encodeURIComponent(userId)}`,
    { method: "DELETE" },
  );
  if (!res.ok && res.status !== 204) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
  }
}
