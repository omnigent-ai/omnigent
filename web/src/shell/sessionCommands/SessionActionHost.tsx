// Carries out the session action ⌘K asked for, with the same dialogs and handlers as the
// sidebar row's menu. It checks the action against the session's current facts first.

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  type Conversation,
  useMoveToProject,
  useSetConversationMuted,
  useTogglePinnedConversation,
} from "@/hooks/useConversations";
import { useActiveRootSessionId } from "@/hooks/useSession";
import { markConversationRead, markConversationUnread } from "@/hooks/useUnseenConversations";
import { useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useNavigate } from "@/lib/routing";

import { useCommandContext } from "../commandContext";
import { RenameSessionDialog } from "../RenameSessionDialog";
import { conversationDisplayLabel } from "../sidebarNav";
import { unpinWithUndo } from "../unpinUndoToast";
import { useSessionActions } from "../useSessionActions";
import { currentSessionRow } from "./currentSession";
import { sessionCommand } from "./model";
import { MoveToProjectPicker } from "./MoveToProjectPicker";
import {
  type SessionActionRequest,
  startSessionAction,
  useSessionActionStore,
} from "./sessionActionStore";
import { useSessionCommandFacts } from "./useSessionCommandFacts";

// The open session, or its sub-agent's root, as the host last saw it. A Leave that settles
// later reads it then, so it leaves only the session that's still open.
let openRootSessionId: string | null = null;

/** Mount once, beside the palette. */
export function SessionActionHost() {
  const request = useSessionActionStore((state) => state.request);
  const openRoot = useActiveRootSessionId(useCommandContext().session?.id ?? null);
  useEffect(() => {
    openRootSessionId = openRoot;
  }, [openRoot]);
  return request ? (
    <SessionActionResolver key={request.key} request={request} openRoot={openRoot} />
  ) : null;
}

/** Looks the session up as the action starts; one gone since does nothing. */
function SessionActionResolver({
  request,
  openRoot,
}: {
  request: SessionActionRequest;
  openRoot: string | null;
}) {
  const queryClient = useQueryClient();
  const [conversation] = useState(() =>
    currentSessionRow(queryClient, request.conversation, request.listed),
  );
  return conversation ? (
    <SessionActionRunner conversation={conversation} request={request} openRoot={openRoot} />
  ) : null;
}

function SessionActionRunner({
  conversation,
  request: { command, key },
  openRoot,
}: {
  conversation: Conversation;
  request: SessionActionRequest;
  openRoot: string | null;
}) {
  // Child effects run before the host's, so an action started on first mount needs the
  // open root recorded here, ahead of the effect that starts it.
  useEffect(() => {
    openRootSessionId = openRoot;
  }, [openRoot]);
  const facts = useSessionCommandFacts(conversation);
  const viewerId = useViewerId();
  const serverInfo = useServerInfo();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const togglePinned = useTogglePinnedConversation();
  const setMuted = useSetConversationMuted();
  const moveToProject = useMoveToProject();
  const actions = useSessionActions({
    conversation,
    label: conversationDisplayLabel(conversation),
    viewerId,
    serverInfo,
    // Leaving the open session, or one of its sub-agents' root, leaves its view too.
    onLeave: () => {
      if (openRootSessionId === conversation.id) navigate("/", { replace: true });
    },
    componentIdPrefix: "palette.conversation",
  });
  const [picker, setPicker] = useState<"rename" | "move-to-project" | null>(null);
  const latest = useRef({ actions, facts });
  latest.current = { actions, facts };
  useEffect(() => {
    const current = latest.current.facts;
    // Once per request, even on a host mounted again.
    if (!current || !startSessionAction(key)) return;
    // Checked against the session's current state, which may have changed since its row showed.
    if (!sessionCommand(current, command).availability.available) return;
    const { id, updated_at: updatedAt } = conversation;
    const run = latest.current.actions;
    switch (command) {
      case "add-sibling":
        navigate(`/?sibling=${encodeURIComponent(id)}`);
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
  }, [command, key, conversation, facts, navigate, queryClient, togglePinned, setMuted]);

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
    </>
  );
}
