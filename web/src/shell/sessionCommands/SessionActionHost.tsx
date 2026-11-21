// Carries out the session action ⌘K asked for, with the same dialogs and handlers as the
// sidebar row's menu. It checks the action against the session's current facts first, and
// stays mounted after a dialog closes so a write it started still reports how it went.

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
import { type SessionActionRequest, useSessionActionStore } from "./sessionActionStore";
import { useSessionCommandFacts } from "./useSessionCommandFacts";

/** Mount once, beside the palette. */
export function SessionActionHost() {
  const request = useSessionActionStore((state) => state.request);
  return request ? <SessionActionResolver key={request.key} request={request} /> : null;
}

/** Looks the session up as the action starts; one gone since does nothing. */
function SessionActionResolver({ request }: { request: SessionActionRequest }) {
  const queryClient = useQueryClient();
  const [conversation] = useState(() =>
    currentSessionRow(queryClient, request.conversation, request.listed),
  );
  return conversation ? (
    <SessionActionRunner conversation={conversation} command={request.command} />
  ) : null;
}

function SessionActionRunner({
  conversation,
  command,
}: {
  conversation: Conversation;
  command: SessionActionRequest["command"];
}) {
  const facts = useSessionCommandFacts(conversation);
  const viewerId = useViewerId();
  const serverInfo = useServerInfo();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const togglePinned = useTogglePinnedConversation();
  const setMuted = useSetConversationMuted();
  const moveToProject = useMoveToProject();
  // Removing the open session, or one of its sub-agents' root, leaves its view first.
  const viewing =
    useActiveRootSessionId(useCommandContext().session?.id ?? null) === conversation.id;
  const actions = useSessionActions({
    conversation,
    label: conversationDisplayLabel(conversation),
    viewerId,
    serverInfo,
    onLeave: viewing ? () => navigate("/", { replace: true }) : undefined,
    componentIdPrefix: "palette.conversation",
  });
  const [picker, setPicker] = useState<"rename" | "move-to-project" | null>(null);
  const started = useRef(false);
  const latest = useRef({ actions, facts });
  latest.current = { actions, facts };

  useEffect(() => {
    const current = latest.current.facts;
    if (started.current || !current) return;
    started.current = true;
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
  }, [command, conversation, facts, navigate, queryClient, togglePinned, setMuted]);

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
