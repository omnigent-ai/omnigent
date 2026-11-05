import { useLoadedConversations } from "@/hooks/useSidebarData";
/**
 * TanStack Query hooks for session permissions CRUD.
 * Wraps the fetch functions in `permissionsApi.ts`.
 */

import { useMemo } from "react";
import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PINNED_CONVERSATIONS_KEY } from "@/hooks/useConversations";
import {
  type Permission,
  derivePermissionLevel,
  getSessionOwner,
  grantPermission,
  listPermissions,
  revokePermission,
} from "@/lib/permissionsApi";
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

/**
 * Refetch the grants and every session list after a share change, so rows pick up
 * the server's `shared_with_others`. Invalidation also restarts in-flight reads.
 */
function invalidateAfterShareChange(qc: QueryClient, sessionId: string): void {
  void qc.invalidateQueries({ queryKey: permissionsKey(sessionId) });
  void qc.invalidateQueries({ queryKey: ["conversations"] });
  void qc.invalidateQueries({ queryKey: ["project-sessions"] });
  void qc.invalidateQueries({ queryKey: PINNED_CONVERSATIONS_KEY });
}

/** Grant or update a permission. Invalidates the grants and session lists on success. */
export function useGrantPermission(sessionId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, level }: { userId: string; level: number }) =>
      grantPermission(sessionId, userId, level),
    onSuccess: () => invalidateAfterShareChange(qc, sessionId),
  });
}

/** Revoke a permission. Invalidates the grants and session lists on success. */
export function useRevokePermission(sessionId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) => revokePermission(sessionId, userId),
    onSuccess: () => invalidateAfterShareChange(qc, sessionId),
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
