// A live tile's chat and terminal, composed like a grid cell: sends queue or
// steer while the agent works, the draft is shared with the single agent view,
// the composer carries the host, permission and model pickers, and a
// terminal-first session switches between Chat and Terminal.

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { SideChatPane } from "@/components/chat/SideChatPane";
import { HostBadge } from "@/components/HostBadge";
import { GridPermissionPicker, GridSessionHarnessPicker } from "@/grid/GridSessionPane";
import { useSharedSessionDraft } from "@/grid/sharedSessionDraft";
import { useGridPermissionControl } from "@/grid/useGridPermissionControl";
import { useBrowserAgentRelay } from "@/hooks/useBrowserAgentRelay";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import { useCanEdit } from "@/hooks/usePermissions";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { useRunnerHealthRegistration, useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useSession } from "@/hooks/useSession";
import { livenessRowFromSession, useSessionLiveness } from "@/hooks/useSessionLiveness";
import { findAgentTerminal, useTerminals } from "@/hooks/useTerminals";
import { isNativeWrapper } from "@/lib/nativeCodingAgents";
import { readTranscriptViewDefault } from "@/lib/transcriptViewPreferences";
import { retrySession } from "@/lib/sessionsApi";
import { MainTerminalView } from "@/shell/MainTerminalView";
import {
  TerminalFirstContextProvider,
  useTerminalFirst,
  type TerminalFirstView,
} from "@/shell/TerminalFirstContext";
import { releaseConversation } from "@/store/chatStore";

// The single agent view and grid cells keep each session's Chat/Terminal choice here.
const viewKey = (sessionId: string) => `omnigent.web.panel-key:${sessionId}`;

function readView(sessionId: string): TerminalFirstView {
  try {
    const saved = sessionStorage.getItem(viewKey(sessionId));
    if (saved !== null) return saved === "__chat__" ? "chat" : "terminal";
  } catch {
    /* Storage can be unavailable. */
  }
  return readTranscriptViewDefault();
}

/** The Chat/Terminal state a live tile's header toggle and body share. */
export function TileTerminalProvider({
  sessionId,
  children,
}: {
  sessionId: string;
  children: ReactNode;
}) {
  const { session } = useSession(sessionId);
  const state = useConversationEntryState(sessionId);
  const wrapper = session?.labels?.["omnigent.wrapper"];
  const terminalFirst = session?.labels?.["omnigent.ui"] === "terminal";
  const { terminals } = useTerminals(terminalFirst ? sessionId : null, {
    reconcileWhilePending: state.terminalPending,
  });
  const agentTerminal = useMemo(() => findAgentTerminal(terminals), [terminals]);
  const [view, setViewState] = useState<TerminalFirstView>(() => readView(sessionId));
  const setView = useCallback(
    (next: TerminalFirstView) => {
      setViewState(next);
      try {
        sessionStorage.setItem(viewKey(sessionId), next === "chat" ? "__chat__" : "");
      } catch {
        /* The view still works without persistence. */
      }
    },
    [sessionId],
  );
  const value = useMemo(
    () => ({
      isClaudeNative: wrapper === "claude-code-native-ui",
      isNativeWrapper: isNativeWrapper(wrapper),
      isTerminalFirst: terminalFirst,
      isShellView: false,
      view,
      terminalViewKey: null,
      setView,
      terminalsAvailable: agentTerminal !== null,
      terminalStartingUp:
        agentTerminal === null && Boolean(state.terminalPending || session?.terminalPending),
    }),
    [
      wrapper,
      terminalFirst,
      view,
      setView,
      agentTerminal,
      state.terminalPending,
      session?.terminalPending,
    ],
  );
  return <TerminalFirstContextProvider value={value}>{children}</TerminalFirstContextProvider>;
}

/** A live tile's body: the grid cell composer, or its terminal. */
export function TileChat({ sessionId, active }: { sessionId: string; active: boolean }) {
  const { session } = useSession(sessionId);
  const state = useConversationEntryState(sessionId);
  const canEdit = useCanEdit(sessionId);
  const permissionLevel = session?.permissionLevel ?? (canEdit ? null : 1);
  useSharedSessionDraft(sessionId);
  const healthSessions = useMemo(() => [{ id: sessionId }], [sessionId]);
  useRunnerHealthRegistration(healthSessions);
  useBrowserAgentRelay(sessionId);
  const runnerOnline = useSessionRunnerOnline(sessionId);
  const liveness = useSessionLiveness(sessionId, livenessRowFromSession(session), {
    turnActive: state.status === "streaming",
    launchedAt: state.runnerLaunchedAt,
  });
  const unreachable = liveness.kind === "host_offline" || liveness.kind === "local_stranded";
  const permission = useGridPermissionControl(session, permissionLevel, unreachable);
  const terminal = useTerminalFirst();
  const showTerminal = Boolean(terminal?.isTerminalFirst && terminal.view === "terminal");
  // A terminal holds its own connection, so the chat stream it replaces closes.
  useEffect(() => {
    if (showTerminal) releaseConversation(sessionId);
  }, [showTerminal, sessionId]);
  const resume = useCallback(async () => {
    const result = await retrySession(sessionId);
    if (!result.recovered)
      throw new Error("The session is already connected; no recovery was performed");
  }, [sessionId]);

  if (showTerminal) {
    return (
      <MainTerminalView
        conversationId={sessionId}
        visible
        active={active}
        className="pt-0"
        readOnly={!session || !isOwnerLevel(permissionLevel)}
        runnerOnline={runnerOnline}
        onResume={canEdit ? resume : undefined}
        sandboxOffline={liveness.kind === "host_asleep"}
      />
    );
  }
  return (
    <SideChatPane
      childId={sessionId}
      fullSession
      readOnly={!canEdit}
      gridComposer={{
        leading: session && (
          <>
            <HostBadge
              sessionId={sessionId}
              appearance="composer"
              readOnly={!canEdit}
              launch={state.sandboxStatus}
            />
            <GridPermissionPicker permission={permission} />
          </>
        ),
        trailing: session && (
          <GridSessionHarnessPicker
            sessionId={sessionId}
            session={session}
            canEdit={canEdit}
            unreachable={unreachable}
            runnerOnline={runnerOnline}
          />
        ),
        cyclePermissionMode: permission.cycle,
      }}
    />
  );
}
