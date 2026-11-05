import { useLoadedConversations } from "@/hooks/useSidebarData";
/**
 * TanStack Query hooks for session permissions CRUD.
 * Wraps the fetch functions in `permissionsApi.ts`.
 */

import { useMemo } from "react";
import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  LEVEL_OWNER,
  PUBLIC_USER_ID,
  type Permission,
  derivePermissionLevel,
  getSessionOwner,
  grantPermission,
  listPermissions,
  revokePermission,
} from "@/lib/permissionsApi";
import { overlayListItemIntoCaches, PINNED_CONVERSATIONS_KEY } from "@/lib/sessionListCache";
import { isTempConvId } from "@/lib/tempConversationId";
import { useSession } from "./useSession";

function permissionsKey(sessionId: string) {
  return ["permissions", sessionId] as const;
}

function sessionOwnerKey(sessionId: string) {
  return ["sessionOwner", sessionId] as const;
}

/** Fetch all permission grants for a session. */
export function usePermissions(rawSessionId: string | null) {
  // A `temp:*` id (navigate-first new-chat window) has no server session.
  const sessionId = isTempConvId(rawSessionId) ? null : rawSessionId;
  return useQuery({
    queryKey: permissionsKey(sessionId ?? ""),
    queryFn: () => listPermissions(sessionId!),
    enabled: !!sessionId,
  });
}

/**
 * Fetch the owner (the ``user_id`` granted ``LEVEL_OWNER``) of a session.
 *
 * Returns ``null`` when permissions are disabled (single-user mode), so the
 * caller can omit any owner UI. Requires only read access, so it resolves for
 * anyone a session is shared with — letting the info popover answer "whose
 * session is this?" for chats shared into a workspace group. Disabled until a
 * sessionId is known.
 */
export function useSessionOwner(sessionId: string | null) {
  return useQuery({
    queryKey: sessionOwnerKey(sessionId ?? ""),
    queryFn: () => getSessionOwner(sessionId!),
    enabled: !!sessionId,
  });
}

// Session-list caches a share change patches (see overlayListItemIntoCaches).
const SESSION_LIST_ROOTS = new Set<unknown>([
  "conversations",
  "project-sessions",
  PINNED_CONVERSATIONS_KEY[0],
]);

/**
 * Apply a successful share change to the cached grant list, then derive the
 * sidebar row's shared flag from it and overlay it onto every cached list.
 * List reads already in flight predate the change, so they're restarted (not
 * cancelled) to keep a stale response from landing and to never strand a load.
 *
 * @param qc - The app QueryClient.
 * @param sessionId - Session whose grants changed.
 * @param update - Returns the grant list after the change.
 * @param sharedWithoutGrants - Flag to apply when no grant list is cached, or
 *   `undefined` to leave the row for the updates stream to correct.
 */
function applyShareChange(
  qc: QueryClient,
  sessionId: string,
  update: (grants: Permission[]) => Permission[],
  sharedWithoutGrants: boolean | undefined,
): void {
  const grants = qc.getQueryData<Permission[]>(permissionsKey(sessionId));
  const next = grants && update(grants);
  if (next) qc.setQueryData(permissionsKey(sessionId), next);
  const shared = next
    ? next.some((g) => g.user_id !== PUBLIC_USER_ID && g.level < LEVEL_OWNER)
    : sharedWithoutGrants;
  if (shared !== undefined) {
    overlayListItemIntoCaches(qc, { id: sessionId, shared_with_others: shared });
  }
  // Invalidation refetches with cancelRefetch, replacing any in-flight read.
  void qc.invalidateQueries({ queryKey: permissionsKey(sessionId) });
  void qc.invalidateQueries({
    predicate: (query) =>
      SESSION_LIST_ROOTS.has(query.queryKey[0]) && query.state.fetchStatus === "fetching",
    refetchType: "all",
  });
}

/** Grant or update a permission, keeping the grant list and sidebar flag current. */
export function useGrantPermission(sessionId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, level }: { userId: string; level: number }) =>
      grantPermission(sessionId, userId, level),
    onSuccess: (_data, { userId, level }) =>
      applyShareChange(
        qc,
        sessionId,
        (grants) => [
          ...grants.filter((g) => g.user_id !== userId),
          { user_id: userId, conversation_id: sessionId, level },
        ],
        // Any named grant means shared; a public grant alone doesn't change it.
        userId !== PUBLIC_USER_ID ? true : undefined,
      ),
  });
}

/** Revoke a permission, keeping the grant list and sidebar flag current. */
export function useRevokePermission(sessionId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) => revokePermission(sessionId, userId),
    onSuccess: (_data, userId) =>
      applyShareChange(
        qc,
        sessionId,
        (grants) => grants.filter((g) => g.user_id !== userId),
        undefined,
      ),
  });
}

/**
 * Returns whether the current user has edit access (level >= 2) to a session.
 * `null` permission level (single-user mode) is treated as unrestricted.
 */
export function useCanEdit(conversationId: string): boolean {
  const { data: conversationsData } = useLoadedConversations();
  const { session: activeSession, isLoading: sessionLoading } = useSession(conversationId);
  return useMemo(() => {
    const conversations = conversationsData?.pages.flatMap((p) => p.data);
    const activeConv = conversations?.find((c) => c.id === conversationId) ?? null;
    const permissionLevel = derivePermissionLevel(
      activeSession,
      sessionLoading,
      activeConv,
      conversationId,
      conversationsData !== undefined,
    );
    return permissionLevel == null || permissionLevel >= 2;
  }, [conversationsData, conversationId, activeSession, sessionLoading]);
}

export type { Permission };
