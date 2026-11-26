import { useLoadedConversations } from "@/hooks/useSidebarData";
/**
 * TanStack Query hooks for session, automation and workflow permissions CRUD.
 * Wraps the fetch functions in `permissionsApi.ts`.
 */

import { useMemo } from "react";
import {
  type QueryClient,
  queryOptions,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { PINNED_CONVERSATIONS_KEY } from "@/hooks/useConversations";
import {
  type GrantList,
  type Permission,
  type ShareGrant,
  type ShareTarget,
  LEVEL_READ,
  derivePermissionLevel,
  getSessionOwner,
  grantPermission,
  grantSharePermission,
  isOwnerLevel,
  listAutomationPermissions,
  listPermissions,
  listGadgetPermissions,
  listWorkflowPermissions,
  revokePermission,
  revokeSharePermission,
} from "@/lib/permissionsApi";
import { isTempConvId } from "@/lib/tempConversationId";
import { useSession } from "./useSession";
import type { Session } from "@/lib/types";

/** Every session's grants list, which also names the automation managing it. */
const SESSION_PERMISSIONS_KEY = ["permissions"] as const;

function permissionsKey(target: ShareTarget) {
  if (target.kind === "session") return [...SESSION_PERMISSIONS_KEY, target.id] as const;
  return [`${target.kind}-permissions`, target.id] as const;
}

function sessionOwnerKey(sessionId: string) {
  return ["sessionOwner", sessionId] as const;
}

function grantListQuery(rawTarget: ShareTarget | null, enabled: boolean) {
  // A `temp:*` id (navigate-first new-chat window) has no server session.
  const target = rawTarget?.kind === "session" && isTempConvId(rawTarget.id) ? null : rawTarget;
  return queryOptions<GrantList>({
    queryKey: target ? permissionsKey(target) : [...SESSION_PERMISSIONS_KEY, ""],
    queryFn: () => {
      if (target!.kind === "session") return listPermissions(target!.id);
      if (target!.kind === "workflow") return listWorkflowPermissions(target!.id);
      if (target!.kind === "gadget") return listGadgetPermissions(target!.id);
      return listAutomationPermissions(target!.id);
    },
    enabled: enabled && !!target,
  });
}

const selectGrants = (list: GrantList): ShareGrant[] => list.permissions;

/** Fetch all permission grants for a session or an automation. */
export function usePermissions(target: ShareTarget | null) {
  return useQuery({ ...grantListQuery(target, true), select: selectGrants });
}

/**
 * The grants with the automation that manages a session's sharing, from one
 * request. Turning `enabled` off keeps the loaded list, e.g. while a dialog closes.
 */
export function useGrantList(target: ShareTarget, enabled: boolean) {
  return useQuery(grantListQuery(target, enabled));
}

/**
 * Mark every session's grants stale: changing an automation, its sharing or
 * whether it continues a session can change which session it manages.
 */
export function invalidateSessionGrants(qc: QueryClient): void {
  void qc.invalidateQueries({ queryKey: SESSION_PERMISSIONS_KEY });
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
  // The server shares an automation's continued session at once.
  if (target.kind === "automation") invalidateSessionGrants(qc);
  // A change can be the viewer's own access, which every open view of the gadget shows.
  if (target.kind === "gadget") void qc.invalidateQueries({ queryKey: ["gadgets"] });
  void qc.invalidateQueries({ queryKey: ["conversations"] });
  void qc.invalidateQueries({ queryKey: ["project-sessions"] });
  void qc.invalidateQueries({ queryKey: PINNED_CONVERSATIONS_KEY });
}

/**
 * Grant or update a permission. Invalidates the grants and session lists on
 * success. An automation or a workflow is shared at read access whatever `level` says;
 * a gadget at the level given.
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
      if (target.kind === "gadget")
        return ensureMinimum
          ? grantSharePermission(target, userId, level, true)
          : grantSharePermission(target, userId, level);
      if (target.kind !== "session") return grantSharePermission(target, userId, LEVEL_READ);
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
      target.kind === "session"
        ? revokePermission(target.id, userId)
        : revokeSharePermission(target, userId),
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

/**
 * Whether the viewer holds the owner grant on the session's root, as the server decides
 * it for side chats (no admin bypass). `null` while loading or from a server that doesn't say.
 */
export function useIsSessionOwner(sessionId: string | null | undefined): boolean | null {
  const { session } = useSession(sessionId);
  return session?.sideChatAllowed ?? null;
}

export type { GrantList, Permission, ShareGrant, ShareTarget };

/**
 * Who may change a session's pull requests: on a shared session only its owner, since
 * every change runs as the owner's code-host account or speaks for the session. "unknown"
 * while the session loads, so neither the controls nor the owner-only note flash.
 */
export type PullRequestChangeAccess = "owner" | "not_owner" | "unknown";
/**
 * The server's answer for whether the viewer owns the session's root (its owner grant, never
 * an admin flag; everyone where permissions are off), sent as `side_chat_allowed`. Older
 * servers fall back to the viewer's own level.
 */
export function pullRequestChangeAccess(
  session: Pick<Session, "sideChatAllowed" | "permissionLevel"> | null | undefined,
  isLoading: boolean,
): PullRequestChangeAccess {
  if (!session) return isLoading ? "unknown" : "not_owner";
  const owner = session.sideChatAllowed ?? isOwnerLevel(session.permissionLevel ?? null);
  return owner ? "owner" : "not_owner";
}
/** {@link pullRequestChangeAccess} for `sessionId`. */
export function usePullRequestChangeAccess(sessionId: string): PullRequestChangeAccess {
  const { session, isLoading } = useSession(sessionId);
  return pullRequestChangeAccess(session, isLoading);
}
/**
 * The one gate for changing a session's pull requests here, through its machine or this
 * Mac alike: edit access, held by the session's owner.
 */
export function useCanChangePullRequests(sessionId: string): boolean {
  const canEdit = useCanEdit(sessionId);
  const access = usePullRequestChangeAccess(sessionId);
  return canEdit && access === "owner";
}
