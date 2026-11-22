// Carries out the session action ⌘K asked for, with the same dialogs and handlers as the
// sidebar row's menu. It checks the action against the session's current facts first, and
// stays mounted after a dialog closes so a write it started still reports how it went.

import { useEffect, useRef, useState } from "react";
import { useIsMutating, useQueryClient } from "@tanstack/react-query";

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
  settleSessionAction,
  startSessionAction,
  useSessionActionStore,
} from "./sessionActionStore";
import { useSessionCommandFacts } from "./useSessionCommandFacts";

/** Mount once, beside the palette. */
export function SessionActionHost() {
  const requests = useSessionActionStore((state) => state.requests);
  const shownKey = requests.at(-1)?.key;
  return requests.map((request) => (
    <SessionActionResolver key={request.key} request={request} shown={request.key === shownKey} />
  ));
}

interface SessionActionProps {
  request: SessionActionRequest;
  /** The newest request, whose dialogs show; an older one only finishes its writes. */
  shown: boolean;
}

/** Looks the session up as the action starts; one gone since does nothing. */
function SessionActionResolver({ request, shown }: SessionActionProps) {
  const queryClient = useQueryClient();
  const [conversation] = useState(() =>
    currentSessionRow(queryClient, request.conversation, request.listed),
  );
  useEffect(() => {
    if (!conversation && !shown) settleSessionAction(request.key);
  }, [conversation, shown, request.key]);
  return conversation ? (
    <SessionActionRunner conversation={conversation} request={request} shown={shown} />
  ) : null;
}

/** Whether a mutation's variables name session `id`, as the per-session writes' do. */
function namesSession(variables: unknown, id: string): boolean {
  if (variables === id) return true;
  return (
    typeof variables === "object" && variables !== null && "id" in variables && variables.id === id
  );
}

function SessionActionRunner({
  conversation,
  request,
  shown,
}: SessionActionProps & { conversation: Conversation }) {
  const { command, key } = request;
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
  const latest = useRef({ actions, facts });
  latest.current = { actions, facts };
  // A write it started, such as Stop or Leave, reports how it went through this runner's
  // own callbacks, so a replaced runner stays mounted until its writes settle.
  const writing =
    useIsMutating({
      predicate: (mutation) =>
        mutation.state.status === "pending" &&
        namesSession(mutation.state.variables, conversation.id),
    }) > 0;
  useEffect(() => {
    if (!shown && !writing) settleSessionAction(key);
  }, [shown, writing, key]);

  useEffect(() => {
    const current = latest.current.facts;
    // Once per request, even on a host mounted again, such as after a server info reload.
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

  // A replaced request's dialogs close; only the newest one's show.
  if (!shown) return null;
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
