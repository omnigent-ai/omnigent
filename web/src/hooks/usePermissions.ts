import { useLoadedConversations } from "@/hooks/useSidebarData";
/**
 * TanStack Query hooks for session and automation permissions CRUD.
 * Wraps the fetch functions in `permissionsApi.ts`.
 */

import { useMemo } from "react";
import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PINNED_CONVERSATIONS_KEY } from "@/hooks/useConversations";
import {
  type Permission,
  type ShareGrant,
  type ShareTarget,
  derivePermissionLevel,
  getSessionManagingAutomation,
  getSessionOwner,
  grantAutomationPermission,
  grantPermission,
  isOwnerLevel,
  listAutomationPermissions,
  listPermissions,
  revokeAutomationPermission,
  revokePermission,
} from "@/lib/permissionsApi";
import { isTempConvId } from "@/lib/tempConversationId";
import { useSession } from "./useSession";

const SESSION_SHARING_MANAGER_KEY = ["session-sharing-manager"] as const;

function permissionsKey(target: ShareTarget) {
  return target.kind === "session"
    ? (["permissions", target.id] as const)
    : (["automation-permissions", target.id] as const);
}

function sessionOwnerKey(sessionId: string) {
  return ["sessionOwner", sessionId] as const;
}

/** Fetch all permission grants for a session or an automation. */
export function usePermissions(rawTarget: ShareTarget | null) {
  // A `temp:*` id (navigate-first new-chat window) has no server session.
  const target = rawTarget?.kind === "session" && isTempConvId(rawTarget.id) ? null : rawTarget;
  return useQuery<ShareGrant[]>({
    queryKey: target ? permissionsKey(target) : ["permissions", ""],
    queryFn: () =>
      target!.kind === "session"
        ? listPermissions(target!.id)
        : listAutomationPermissions(target!.id),
    enabled: !!target,
  });
}

/**
 * The viewer's automation that overwrites a session's sharing on each run, or
 * `null`. Disabled until a sessionId is known.
 */
export function useSessionManagingAutomation(sessionId: string | null) {
  const id = isTempConvId(sessionId) ? null : sessionId;
  return useQuery({
    queryKey: [...SESSION_SHARING_MANAGER_KEY, id ?? ""],
    queryFn: () => getSessionManagingAutomation(id!),
    enabled: !!id,
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
 * Mark the grants and every session list stale after a share change, so active
 * lists refetch and pick up the server's `shared_with_others`.
 */
function invalidateAfterShareChange(qc: QueryClient, target: ShareTarget): void {
  void qc.invalidateQueries({ queryKey: permissionsKey(target) });
  if (target.kind === "automation") {
    // The server shares its continued session at once, which can also start or
    // stop the automation managing that session's sharing.
    void qc.invalidateQueries({ queryKey: ["permissions"] });
    void qc.invalidateQueries({ queryKey: SESSION_SHARING_MANAGER_KEY });
  }
  void qc.invalidateQueries({ queryKey: ["conversations"] });
  void qc.invalidateQueries({ queryKey: ["project-sessions"] });
  void qc.invalidateQueries({ queryKey: PINNED_CONVERSATIONS_KEY });
}

/**
 * Grant or update a permission. Invalidates the grants and session lists on
 * success. An automation is shared at read access whatever `level` says.
 */
export function useGrantPermission(target: ShareTarget) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      userId,
      level,
      ensureMinimum,
    }: {
      userId: string;
      level: number;
      ensureMinimum?: boolean;
    }): Promise<ShareGrant> => {
      if (target.kind === "automation") return grantAutomationPermission(target.id, userId);
      return ensureMinimum
        ? grantPermission(target.id, userId, level, true)
        : grantPermission(target.id, userId, level);
    },
    onSuccess: () => invalidateAfterShareChange(qc, target),
  });
}

/** Revoke a permission. Invalidates the grants and session lists on success. */
export function useRevokePermission(target: ShareTarget) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) =>
      target.kind === "automation"
        ? revokeAutomationPermission(target.id, userId)
        : revokePermission(target.id, userId),
    onSuccess: () => invalidateAfterShareChange(qc, target),
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

/**
 * Whether the viewer can start side chats in a session: only the owner of its
 * root can. Uses the server's answer, falling back to the viewer's permission
 * level for servers that don't send one.
 */
export function useCanStartSideChat(
  sessionId: string | null | undefined,
  permissionLevel: number | null,
): boolean {
  const { session, isLoading } = useSession(sessionId);
  return session?.sideChatAllowed ?? (!isLoading && isOwnerLevel(permissionLevel));
}

export type { Permission, ShareGrant, ShareTarget };
