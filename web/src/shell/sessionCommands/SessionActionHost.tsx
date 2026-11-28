// Carries out the session action ⌘K asked for, with the same dialogs and handlers as the
// sidebar row's menu. It checks the action against the session's current facts first.

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { showToast } from "@/components/ui/toast";
import {
  type Conversation,
  useMoveToNest,
  useMoveToProject,
  useSetConversationMuted,
  useTogglePinnedConversation,
} from "@/hooks/useConversations";
import { useOpenSessionWindow } from "@/hooks/useOpenSessionWindow";
import { markConversationRead, markConversationUnread } from "@/hooks/useUnseenConversations";
import { useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useNavigate } from "@/lib/routing";

import { isOpenSession } from "../openSession";
import { RenameSessionDialog } from "../RenameSessionDialog";
import { conversationDisplayLabel } from "../sidebarNav";
import { unpinWithUndo } from "../unpinUndoToast";
import { useSessionActions } from "../useSessionActions";
import { leaveSessionView } from "./archiveNavigation";
import { currentSessionRow } from "./currentSession";
import { sessionCommand } from "./model";
import { MoveToNestPicker } from "./MoveToNestPicker";
import { MoveToProjectPicker } from "./MoveToProjectPicker";
import {
  type SessionActionRequest,
  startSessionAction,
  useSessionActionStore,
} from "./sessionActionStore";
import { useSessionCommandFacts } from "./useSessionCommandFacts";
import { useSessionPickPaletteCommands } from "./useSessionPickCommands";

/** Mount once, beside the palette. */
export function SessionActionHost() {
  useSessionPickPaletteCommands();
  const request = useSessionActionStore((state) => state.request);
  return request ? <SessionActionResolver key={request.key} request={request} /> : null;
}

/** Looks the session up as the action starts; one gone since says so and does nothing. */
function SessionActionResolver({ request }: { request: SessionActionRequest }) {
  const queryClient = useQueryClient();
  const [conversation] = useState(() => {
    const row = currentSessionRow(queryClient, request.conversation, request.listed);
    // A request for a session gone by now is spent, so it can't run if the session returns.
    if (!row && startSessionAction(request.key)) {
      showToast(
        `Couldn't find “${conversationDisplayLabel(request.conversation)}”. It may have been deleted.`,
      );
    }
    return row;
  });
  return conversation ? (
    <SessionActionRunner conversation={conversation} request={request} />
  ) : null;
}

function SessionActionRunner({
  conversation,
  request: { command, key },
}: {
  conversation: Conversation;
  request: SessionActionRequest;
}) {
  const facts = useSessionCommandFacts(conversation);
  const viewerId = useViewerId();
  const serverInfo = useServerInfo();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const togglePinned = useTogglePinnedConversation();
  const setMuted = useSetConversationMuted();
  const moveToProject = useMoveToProject();
  const moveToNest = useMoveToNest();
  const openSessionWindow = useOpenSessionWindow();
  const actions = useSessionActions({
    conversation,
    label: conversationDisplayLabel(conversation),
    viewerId,
    serverInfo,
    // Leaving the open session, or one of its sub-agents' root, leaves its view too.
    onLeave: (cause) => {
      if (!isOpenSession(conversation.id)) return;
      leaveSessionView(cause, { conversation, source: "palette", navigate, queryClient });
    },
    componentIdPrefix: "palette.conversation",
    // A newer request can replace this runner before its Leave or Stop settles.
    reportsAfterUnmount: true,
  });
  const [picker, setPicker] = useState<"rename" | "move-to-project" | "move-to-nest" | null>(null);
  const latest = useRef({ actions, facts });
  latest.current = { actions, facts };
  useEffect(() => {
    const current = latest.current.facts;
    // Once per request, even on a host mounted again.
    if (!current || !startSessionAction(key)) return;
    // Checked against the session's current state, which may have changed since its row showed.
    const { availability, label } = sessionCommand(current, command);
    if (!availability.available) {
      showToast(
        availability.reason ??
          `${label} isn't available for “${conversationDisplayLabel(conversation)}” now.`,
      );
      return;
    }
    const { id, updated_at: updatedAt } = conversation;
    const run = latest.current.actions;
    switch (command) {
      case "add-sibling":
        navigate(`/?sibling=${encodeURIComponent(id)}`);
        return;
      case "open-in-new-window":
        openSessionWindow?.(id);
        return;
      case "pin":
        togglePinned.mutate({ id, pinned: true });
        return;
      case "unpin":
        unpinWithUndo(queryClient, togglePinned.mutateAsync, id, conversation);
        return;
      case "share":
        run.setShareOpen(true);
        return;
      case "fork":
        run.setForkOpen(true);
        return;
      case "rename":
      case "move-to-project":
      case "move-to-nest":
        setPicker(command);
        return;
      case "mute":
      case "unmute":
        setMuted.mutate({ id, muted: command === "mute" });
        return;
      case "mark-unread":
        markConversationUnread(id, updatedAt);
        return;
      case "mark-read":
        markConversationRead(id, updatedAt);
        return;
      case "create-automation":
        run.setAutomationOpen(true);
        return;
      case "stop":
        run.setStopOpen(true);
        return;
      case "archive":
        run.runArchive();
        return;
      case "unarchive":
        run.runUnarchive();
        return;
      case "delete":
        run.setDeleteOpen(true);
        return;
      case "leave":
        run.setLeaveOpen(true);
        return;
    }
  }, [
    command,
    key,
    conversation,
    facts,
    navigate,
    queryClient,
    togglePinned,
    setMuted,
    openSessionWindow,
  ]);

  return (
    <>
      {actions.dialogs}
      {picker === "rename" && (
        <RenameSessionDialog
          conversation={conversation}
          open
          onOpenChange={(open) => {
            if (!open) setPicker(null);
          }}
        />
      )}
      {picker === "move-to-project" && (
        <MoveToProjectPicker
          currentProject={facts?.currentProject ?? null}
          onPick={(project) => {
            moveToProject.mutate({ id: conversation.id, project });
            setPicker(null);
          }}
          onClose={() => setPicker(null)}
        />
      )}
      {picker === "move-to-nest" && (
        <MoveToNestPicker
          conversation={conversation}
          onPick={(destination) => {
            moveToNest.mutate({ id: conversation.id, destination });
            setPicker(null);
          }}
          onClose={() => setPicker(null)}
        />
      )}
    </>
  );
}
