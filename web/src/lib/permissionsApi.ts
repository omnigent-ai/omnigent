/**
 * Typed client for the `/v1/sessions/{id}/permissions` and
 * `/v1/scheduled-tasks/{id}/permissions` endpoints. Mirrors the handlers in
 * `omnigent/server/routes/sessions/routes_permissions.py` and
 * `omnigent/server/routes/scheduled_task_permissions.py`.
 */

import type { Conversation } from "@/hooks/useConversations";
import { isSingleUserMode, type ServerInfo } from "@/lib/capabilities";
import type { Session } from "@/lib/types";
import { authenticatedFetch } from "./identity";

/** What a share dialog shares: a session, or an automation whose runs' sessions follow it. */
export type ShareTarget = { kind: "session"; id: string } | { kind: "automation"; id: string };

/** Numeric read level, the only level an automation can be shared at. */
export const LEVEL_READ = 1;

/**
 * Numeric permission level of the session owner. Mirrors
 * ``LEVEL_OWNER`` in ``omnigent/server/auth.py`` (READ=1, EDIT=2,
 * MANAGE=3, OWNER=4).
 */
export const LEVEL_OWNER = 4;

export function workspaceSharingBlocked(workspace: string | null | undefined): boolean {
  if (!workspace?.startsWith("/")) return false;

  const parts: string[] = [];
  for (const part of workspace.split("/")) {
    if (part === "..") parts.pop();
    else if (part && part !== ".") parts.push(part);
  }
  const prefix = workspace.startsWith("//") && !workspace.startsWith("///") ? "//" : "/";
  const path = prefix + parts.join("/");
  return /^(?:\/|\/root|\/(?:home|Users|var\/home)\/[^/]+)$/.test(path);
}

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
 * Numeric permission level required to mutate the session's shared
 * workspace. Mirrors ``LEVEL_EDIT`` in ``omnigent/server/auth.py``.
 */
export const LEVEL_EDIT = 2;

/**
 * Return whether a permission level grants edit access — mutating the
 * session's shared workspace, e.g. opening or closing a shell tab (both
 * server-gated on ``LEVEL_EDIT``). ``null`` is treated permissively
 * (single-user / still loading), matching ``isOwnerLevel`` / ``useCanEdit``.
 */
export function isEditorLevel(level: number | null): boolean {
  return level == null || level >= LEVEL_EDIT;
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
 * 2. ``activeConv.permission_level`` — sidebar list row, but ONLY when it
 *    actually carries a level. Available synchronously the moment the user
 *    navigates between top-level conversations, so it's a fast path before
 *    the single fetch resolves. A row whose level is ``null`` (a deployment
 *    whose session list is owner-only and omits the caller's effective
 *    level, e.g. the Databricks-managed server) carries no conclusion, so we
 *    skip it and fall through to the authoritative snapshot / fallback
 *    rather than mistaking the absent level for the permissive ``null``
 *    sentinel.
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
  if (activeConv != null && activeConv.permission_level != null) return activeConv.permission_level;
  if (sessionLoading) return null;
  if (conversationId && conversationsLoaded) return 1;
  return null;
}

/**
 * Whether a session is visible to anyone other than the viewer: another
 * principal owns it (so it's shared *with* the viewer), or the viewer owns
 * it and granted access to a non-viewer principal (a user or the
 * ``__public__`` sentinel). ``ownerGrants`` is ``undefined`` until loaded /
 * when the viewer isn't the owner and can't read the manage-only grant list.
 *
 * Used to gate owner-attribution UI (author labels, the info popover's Owner
 * row) so private solo sessions stay uncluttered.
 */
export function isSessionSharedWithOthers(
  owner: string | null,
  viewerId: string | null,
  ownerGrants: readonly { user_id: string }[] | undefined,
): boolean {
  if (owner !== null && viewerId !== null && owner !== viewerId) return true;
  const viewerOwnsSession = owner !== null && owner === viewerId;
  return viewerOwnsSession && (ownerGrants ?? []).some((g) => g.user_id !== viewerId);
}

/** One grant as a share dialog lists it, for a session or an automation. */
export interface ShareGrant {
  user_id: string;
  level: number;
  /** The grantee's name; automations' grants carry it, sessions' do not. */
  display_name?: string;
}

export interface Permission extends ShareGrant {
  conversation_id: string;
}

/** One grant on an automation; each carries the grantee's display name. */
export interface AutomationPermission extends ShareGrant {
  scheduled_task_id: string;
  display_name: string;
}

/** The viewer's automation that sets a session's sharing on each run. */
export interface ManagingAutomation {
  id: string;
  name: string;
}

/** Every grant on a session or an automation, as one list. */
export interface GrantList<T extends ShareGrant = ShareGrant> {
  permissions: T[];
  /**
   * The viewer's continue-session automation that replaces a session's shares
   * on each run, or `null`; always `null` for an automation's own grants.
   */
  managedByAutomation: ManagingAutomation | null;
}

function permissionsPath(target: ShareTarget): string {
  const collection = target.kind === "session" ? "sessions" : "scheduled-tasks";
  return `/v1/${collection}/${encodeURIComponent(target.id)}/permissions`;
}

/** The server's error message for a failed response, or its status line. */
async function responseError(res: Response): Promise<Error> {
  const body = await res.json().catch(() => ({}));
  return new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
}

/**
 * Every grant from a cursor-paginated ({permissions, next_cursor}) list, with
 * the managing automation the server names on its first page.
 */
async function listAllGrants<T extends ShareGrant>(target: ShareTarget): Promise<GrantList<T>> {
  const all: T[] = [];
  let managedByAutomation: ManagingAutomation | null = null;
  let after: string | null = null;
  // Each page provides the cursor for the next request.
  /* oxlint-disable no-await-in-loop */
  do {
    const query = after !== null ? `?after=${encodeURIComponent(after)}` : "";
    const res = await authenticatedFetch(`${permissionsPath(target)}${query}`);
    if (!res.ok) throw await responseError(res);
    const data = (await res.json()) as {
      permissions: T[];
      next_cursor: string | null;
      managed_by_automation?: ManagingAutomation | null;
    };
    if (after === null) managedByAutomation = data.managed_by_automation ?? null;
    all.push(...data.permissions);
    after = data.next_cursor;
  } while (after !== null);
  /* oxlint-enable no-await-in-loop */
  return { permissions: all, managedByAutomation };
}

export async function listPermissions(sessionId: string): Promise<GrantList<Permission>> {
  return listAllGrants<Permission>({ kind: "session", id: sessionId });
}

/** Everyone an automation is shared with, which only its owner may list. */
export async function listAutomationPermissions(
  taskId: string,
): Promise<GrantList<AutomationPermission>> {
  return listAllGrants<AutomationPermission>({ kind: "automation", id: taskId });
}

export async function getSessionOwner(sessionId: string): Promise<string | null> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/owner`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const data = (await res.json()) as { owner: string | null };
  return data.owner;
}

/**
 * Grant a user or group access to a session or an automation. An automation
 * takes read access only; the server refuses the owner, the public and reserved
 * IDs (400) and what its sharing policy forbids (403), with a message saying why.
 */
export async function grantSharePermission<T extends ShareGrant = ShareGrant>(
  target: ShareTarget,
  userId: string,
  level: number,
  ensureMinimum = false,
): Promise<T> {
  const res = await authenticatedFetch(permissionsPath(target), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user_id: userId,
      level,
      ...(ensureMinimum ? { ensure_minimum: true } : {}),
    }),
  });
  if (!res.ok) throw await responseError(res);
  return (await res.json()) as T;
}

export async function grantPermission(
  sessionId: string,
  userId: string,
  level: number,
  ensureMinimum = false,
): Promise<Permission> {
  return grantSharePermission<Permission>(
    { kind: "session", id: sessionId },
    userId,
    level,
    ensureMinimum,
  );
}

/** Remove a grant on a session or an automation; removing a missing one is harmless (204). */
export async function revokeSharePermission(target: ShareTarget, userId: string): Promise<void> {
  const res = await authenticatedFetch(`${permissionsPath(target)}/${encodeURIComponent(userId)}`, {
    method: "DELETE",
  });
  if (!res.ok && res.status !== 204) throw await responseError(res);
}

/**
 * Remove a permission grant on a session.
 *
 * Passing another user's id revokes them (manage access required). Passing the
 * caller's OWN id leaves the session — "unshare myself", so a shared session
 * drops out of your sidebar — which the server allows with only read access.
 * Either way it refuses to remove the owner's grant (403).
 */
export async function revokePermission(sessionId: string, userId: string): Promise<void> {
  return revokeSharePermission({ kind: "session", id: sessionId }, userId);
}

/**
 * Whether the viewer may share an automation: the server can share automations,
 * has other users (as session sharing requires), and the viewer owns it.
 */
export function canShareAutomation(
  info: ServerInfo | "loading",
  viewerId: string | null,
  ownerUserId: string | null,
): boolean {
  return (
    info !== "loading" &&
    info.scheduled_task_sharing === true &&
    !isSingleUserMode(info) &&
    viewerId !== null &&
    ownerUserId === viewerId
  );
}
