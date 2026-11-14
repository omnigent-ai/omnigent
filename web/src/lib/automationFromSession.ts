// "Create automation" from a session. The server copies the session as the
// copy its runs start from (labelled with the automation's id), and every
// run starts a new session from that copy.

import { useCallback, useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useActiveConversationId } from "@/hooks/useActiveConversationId";
import type { Conversation } from "@/hooks/useConversations";
import { forgetScheduledTasks, refreshAutomationSourceSession } from "@/hooks/useScheduledTasks";
import { isFeatureEnabled, type ServerInfo } from "@/lib/capabilities";
import { useLocation, useNavigate } from "@/lib/routing";
import {
  deleteScheduledTask,
  ScheduledTaskApiError,
  type ScheduledTask,
} from "@/lib/scheduledTasksApi";

/** Label the server stamps on an automation's session copy; its value is the automation's id. */
export const SCHEDULED_TASK_SOURCE_LABEL_KEY = "omnigent.scheduled_task_source";

const SIDE_CHAT_LABEL_KEY = "omnigent.side_chat";

/** The session fields the New automation dialog starts from. */
export interface AutomationSourceSession {
  id: string;
  title: string | null;
  agentId: string | null;
  /** Shown when the agent isn't in the selectable agent list. */
  agentName: string | null;
  hostId: string | null;
  workspace: string | null;
  labels: Record<string, string>;
}

export function automationSourceFromConversation(
  conversation: Conversation,
): AutomationSourceSession {
  return {
    id: conversation.id,
    title: conversation.title,
    agentId: conversation.agent_id ?? null,
    agentName: conversation.agent_name ?? null,
    hostId: conversation.host_id ?? null,
    workspace: conversation.workspace ?? null,
    labels: conversation.labels ?? {},
  };
}

/** The automation that runs from a session, or `null`. */
export function automationSourceTaskId(
  conversation: Pick<Conversation, "labels"> | null | undefined,
): string | null {
  const taskId = conversation?.labels?.[SCHEDULED_TASK_SOURCE_LABEL_KEY];
  return typeof taskId === "string" && taskId !== "" ? taskId : null;
}

/**
 * Whether a session's menu offers "Create automation": automations are on for
 * the caller, the server can start runs from a copied session, the viewer owns
 * the session, and it is a real agent session (not a side chat, a client-only
 * placeholder row, or itself a session an automation runs from). The server
 * re-checks all of this when the automation is saved.
 */
export function canCreateAutomationFromSession({
  info,
  automationsEnabled,
  conversation,
  isOwner,
}: {
  info: ServerInfo | "loading";
  automationsEnabled: boolean;
  conversation: Conversation;
  isOwner: boolean;
}): boolean {
  return (
    automationsEnabled &&
    info !== "loading" &&
    info.scheduled_task_source_sessions === true &&
    isOwner &&
    Boolean(conversation.agent_id) &&
    conversation.provisional !== true &&
    conversation.labels?.[SIDE_CHAT_LABEL_KEY] !== "1" &&
    automationSourceTaskId(conversation) === null
  );
}

/** An automation's page, or the automations list on servers without automation pages. */
export function automationPagePath(info: ServerInfo | "loading", task: ScheduledTask): string {
  return isFeatureEnabled(info, "automations_v2")
    ? `/tasks/${encodeURIComponent(task.id)}`
    : "/tasks";
}

/**
 * The automation that deleting *conversation* deletes too, or `null`. With
 * automations off for the caller, the session is deleted alone; its
 * automation keeps its schedule, fails its next run and pauses.
 */
export function automationDeletedWith(
  info: ServerInfo | "loading",
  conversation: Pick<Conversation, "labels"> | null | undefined,
): string | null {
  if (info !== "loading" && info.automations_enabled === false) return null;
  return automationSourceTaskId(conversation);
}

/**
 * Delete the automations that run from sessions about to be deleted, first.
 * Only a `not_found` error counts as already deleted; any other failure
 * resolves `false` so the caller keeps the sessions. Once the deletes finish,
 * it leaves the page the user is on then if that is one of *sessionIds* (all
 * deleted) or a deleted automation's.
 */
export function useDeleteSourceAutomations(): (
  taskIds: string[],
  sessionIds: string[],
) => Promise<boolean> {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const openSessionId = useActiveConversationId();
  // Where the user is when the deletes finish; unknown once this caller unmounts.
  const routeRef = useRef<{ pathname: string; openSessionId: string | undefined } | null>(null);
  useEffect(() => {
    routeRef.current = { pathname, openSessionId };
    return () => {
      routeRef.current = null;
    };
  }, [pathname, openSessionId]);
  return useCallback(
    async (taskIds: string[], sessionIds: string[]) => {
      const results = await Promise.allSettled(
        taskIds.map(async (taskId) => {
          try {
            await deleteScheduledTask(taskId);
          } catch (err) {
            // Only the handler's not_found means the automation is gone; a plain 404 from
            // the automations gate deletes nothing, so the session must stay.
            const gone =
              err instanceof ScheduledTaskApiError &&
              err.status === 404 &&
              err.code === "not_found";
            if (!gone) throw err;
          }
        }),
      );
      const deleted = taskIds.filter((_, index) => results[index]?.status === "fulfilled");
      const allDeleted = deleted.length === taskIds.length;
      const route = routeRef.current;
      if (allDeleted && route?.openSessionId && sessionIds.includes(route.openSessionId)) {
        // The caller deletes this session next, so leave it now, as any session delete does.
        navigate("/", { replace: true });
      } else if (
        route &&
        deleted.some((taskId) => route.pathname.endsWith(`/tasks/${encodeURIComponent(taskId)}`))
      ) {
        // Leave the page of an automation deleted with its session, as deleting it there does.
        navigate("/tasks");
      }
      if (deleted.length > 0) {
        forgetScheduledTasks(queryClient, deleted);
        // Sessions kept after a partial failure lost their automation label too,
        // in the lists and in their own entries.
        refreshAutomationSourceSession(queryClient, allDeleted ? [] : sessionIds);
      }
      return allDeleted;
    },
    [queryClient, navigate],
  );
}
