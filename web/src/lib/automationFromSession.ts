// "Create automation" from a session. The server copies the session as the
// copy its runs start from (labelled with the automation's id), and every
// run starts a new session from that copy.

import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { Conversation } from "@/hooks/useConversations";
import {
  SCHEDULED_TASKS_KEY,
  scheduledTaskKey,
  scheduledTaskRunsKey,
} from "@/hooks/useScheduledTasks";
import { isFeatureEnabled, type ServerInfo } from "@/lib/capabilities";
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

/** Where to land after creating an automation: its page, or the list on servers without one. */
export function automationCreatedPath(info: ServerInfo | "loading", task: ScheduledTask): string {
  return isFeatureEnabled(info, "automations_v2")
    ? `/tasks/${encodeURIComponent(task.id)}`
    : "/tasks";
}

/**
 * Delete the automations that run from sessions about to be deleted. An
 * automation that is already gone (404) counts as deleted. Resolves `false`
 * when any other delete fails, so the caller can keep the sessions; deleting a
 * session first would leave its automation failing every run.
 */
export function useDeleteSourceAutomations(): (taskIds: string[]) => Promise<boolean> {
  const queryClient = useQueryClient();
  return useCallback(
    async (taskIds: string[]) => {
      const results = await Promise.allSettled(
        taskIds.map(async (taskId) => {
          try {
            await deleteScheduledTask(taskId);
          } catch (err) {
            if (!(err instanceof ScheduledTaskApiError && err.status === 404)) throw err;
          }
        }),
      );
      const deleted = taskIds.filter((_, index) => results[index]?.status === "fulfilled");
      if (deleted.length > 0) {
        // Mirror useDeleteScheduledTask: drop the tasks from every cache.
        queryClient.setQueryData<ScheduledTask[]>(SCHEDULED_TASKS_KEY, (tasks) =>
          tasks?.filter((task) => !deleted.includes(task.id)),
        );
        for (const taskId of deleted) {
          queryClient.removeQueries({ queryKey: scheduledTaskKey(taskId), exact: true });
          queryClient.removeQueries({ queryKey: scheduledTaskRunsKey(taskId), exact: true });
        }
        void queryClient.invalidateQueries({ queryKey: SCHEDULED_TASKS_KEY });
      }
      return deleted.length === taskIds.length;
    },
    [queryClient],
  );
}
