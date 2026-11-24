// One session's action facts, read the way its sidebar row reads them, for a session ⌘K
// lists whether or not its row is on screen.

import { useContext, useMemo } from "react";
import { useIsMutating } from "@tanstack/react-query";

import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { type Conversation, useProjects } from "@/hooks/useConversations";
import { useOpenSessionWindow } from "@/hooks/useOpenSessionWindow";
import { useActiveRootSessionId } from "@/hooks/useSession";
import { useSessionActionRestrictions } from "@/hooks/useSessionActionRestrictions";
import { SidebarDataContext } from "@/hooks/useSidebarData";
import { useConversationReadState } from "@/hooks/useUnseenConversations";
import { useViewerId } from "@/hooks/useViewerId";
import { canCreateAutomationFromSession } from "@/lib/automationFromSession";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isSingleUserMode } from "@/lib/capabilities";
import { isIndentedSibling, isSessionPinned } from "@/lib/sessionFamilies";
import { PIN_WRITE_MUTATION_KEY, PROJECT_LABEL_KEY } from "@/lib/sessionListCache";
import { isInMutedProject, isSessionMuted, useMutedProjects } from "@/lib/sessionMute";
import { isSessionStoppable } from "@/lib/sessionStop";
import { isOwnSessionWindow } from "@/lib/sessionWindow";
import { PinCapacityContext } from "@/lib/sidebarConfig";

import { useCommandContext } from "../commandContext";
import { type SessionCommandFacts, sessionOwnership } from "./model";

/** The facts for `conversation`, or null without one. */
export function useSessionCommandFacts(
  conversation: Conversation | null,
): SessionCommandFacts | null {
  const sessionId = conversation?.id ?? null;
  const viewerId = useViewerId();
  const serverInfo = useServerInfo();
  const { data: projects } = useProjects();
  const atPinCapacity = useContext(PinCapacityContext);
  const pinnedRows = useContext(SidebarDataContext)?.pinned.data?.conversations;
  const pinSaving = useIsMutating({ mutationKey: PIN_WRITE_MUTATION_KEY }) > 0;
  const runnerOnline = useSessionRunnerOnline(sessionId ?? undefined);
  const { forkDisabledReason } = useSessionActionRestrictions(sessionId, conversation);
  // A project mute changes no session row, so its own store re-renders the facts.
  const mutedProjects = useMutedProjects();
  const routedId = useCommandContext().session?.id ?? null;
  const routedRoot = useActiveRootSessionId(routedId);
  const canOpenWindows = useOpenSessionWindow() !== null;
  const readState = useConversationReadState(
    sessionId ?? "",
    conversation?.updated_at ?? 0,
    conversation?.status,
  );

  return useMemo(() => {
    if (!conversation) return null;
    const ownership = sessionOwnership(conversation, viewerId, isSingleUserMode(serverInfo));
    const owner = ownership === "mine";
    const currentProject =
      (conversation.project_id != null
        ? projects?.find((project) => project.id === conversation.project_id)?.name
        : undefined) ??
      conversation.labels?.[PROJECT_LABEL_KEY] ??
      null;
    // The open session's dot shows only once it's explicitly marked unread, as on its row.
    const active = routedRoot === conversation.id;
    return {
      provisional: conversation.provisional === true,
      owner,
      ownerUnknown: ownership === "unknown",
      archived: conversation.archived === true,
      // As the row decides it, so a family pinned through its leader reads as pinned.
      pinned: isSessionPinned(conversation, new Set(pinnedRows?.map((row) => row.id))),
      indentedSibling: isIndentedSibling(conversation),
      atPinCapacity,
      pinSaving,
      unread: readState.unseen && (!active || readState.explicitlyUnread),
      muted: isSessionMuted(conversation),
      mutedByProject: mutedProjects.length > 0 && isInMutedProject(conversation),
      // As the row decides it: never the session this window was opened for.
      canOpenWindow: canOpenWindows && !isOwnSessionWindow(conversation.id, routedId ?? undefined),
      canCreateAutomation: canCreateAutomationFromSession({
        info: serverInfo,
        automationsEnabled: serverInfo !== "loading" && serverInfo.automations_enabled !== false,
        conversation,
        isOwner: owner,
      }),
      currentProject,
      stoppable:
        isSessionStoppable({
          labels: conversation.labels,
          hostId: conversation.host_id,
          runnerId: conversation.runner_id,
        }) && runnerOnline !== false,
      forkDisabledReason,
      server: {
        singleUser: isSingleUserMode(serverInfo),
        sharingOff: serverInfo !== "loading" && serverInfo.sharing_mode === "off",
        siblingSessions: serverInfo !== "loading" && serverInfo.sibling_sessions === true,
        sessionMute: serverInfo !== "loading" && serverInfo.session_mute === true,
      },
    };
  }, [
    conversation,
    viewerId,
    projects,
    routedRoot,
    routedId,
    canOpenWindows,
    pinnedRows,
    atPinCapacity,
    pinSaving,
    readState.unseen,
    readState.explicitlyUnread,
    serverInfo,
    runnerOnline,
    forkDisabledReason,
    mutedProjects,
  ]);
}
