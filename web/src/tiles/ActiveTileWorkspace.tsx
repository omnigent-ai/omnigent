// The Workspace panel for the active tile. Mounted once, for the active agent
// only, so a single session loads workspace data at a time.

import { useEffect, type ContextType } from "react";
import { useChildSessions } from "@/hooks/useChildSessions";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import { useCanEdit } from "@/hooks/usePermissions";
import { useRootSessionId, useSession } from "@/hooks/useSession";
import { livenessRowFromSession, useSessionLiveness } from "@/hooks/useSessionLiveness";
import type { FileViewerContext } from "@/shell/FileViewerContext";
import {
  SessionWorkspacePanel,
  type SharedWorkspacePanelProps,
} from "@/shell/SessionWorkspacePanel";
import { useSessionWorkspace } from "@/shell/useSessionWorkspace";

export type TileFileContext = NonNullable<ContextType<typeof FileViewerContext>>;
/** Runs `apply` once the panel has resolved any unsaved edits. */
export type TileSwitchGuard = (apply: () => void) => void;

interface ActiveTileWorkspaceProps {
  sessionId: string;
  workspacePanel: SharedWorkspacePanelProps;
  onOpenWorkspace: () => void;
  /** Receives the file context the active tile's chat should use, or null on unmount. */
  onFileContext: (context: TileFileContext | null) => void;
  /** Receives the guard to run before switching the active tile, or null on unmount. */
  onSwitchGuard: (guard: TileSwitchGuard | null) => void;
}

export function ActiveTileWorkspace({
  sessionId,
  workspacePanel,
  onOpenWorkspace,
  onFileContext,
  onSwitchGuard,
}: ActiveTileWorkspaceProps) {
  const { session } = useSession(sessionId);
  const state = useConversationEntryState(sessionId);
  const canEdit = useCanEdit(sessionId);
  const permissionLevel = session?.permissionLevel ?? (canEdit ? null : 1);
  const terminalFirst = session?.labels?.["omnigent.ui"] === "terminal";
  const rootSessionId = useRootSessionId(sessionId, session?.parentSessionId);
  const { children: childSessions } = useChildSessions(rootSessionId);
  const liveness = useSessionLiveness(sessionId, livenessRowFromSession(session), {
    turnActive: state.status === "streaming",
    launchedAt: state.runnerLaunchedAt,
  });
  const sessionWorkspace = useSessionWorkspace({
    sessionId,
    session,
    state,
    permissionLevel,
    terminalFirst,
    selected: true,
    onOpenWorkspace,
  });
  const { fileContext, navigateWorkspace } = sessionWorkspace;

  useEffect(() => {
    onFileContext(fileContext);
    return () => onFileContext(null);
  }, [fileContext, onFileContext]);
  useEffect(() => {
    // Leaving this session is a navigation away from its open file.
    onSwitchGuard((apply) => navigateWorkspace(null, undefined, apply));
    return () => onSwitchGuard(null);
  }, [navigateWorkspace, onSwitchGuard]);

  return (
    <SessionWorkspacePanel
      sessionId={sessionId}
      selected
      workspacePanel={workspacePanel}
      onOpenWorkspace={onOpenWorkspace}
      sessionWorkspace={sessionWorkspace}
      state={state}
      session={session}
      childSessions={childSessions}
      rootSessionId={rootSessionId}
      liveness={liveness}
      permissionLevel={permissionLevel}
    />
  );
}
