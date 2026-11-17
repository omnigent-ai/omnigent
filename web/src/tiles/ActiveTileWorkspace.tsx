// The Workspace panel for the active tile. Mounted once, for the active agent
// only, so a single session loads workspace data at a time.

import { useEffect, type ContextType } from "react";
import { PauseCircleIcon } from "lucide-react";
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

export function ActiveTileWorkspace(props: ActiveTileWorkspaceProps) {
  const { session } = useSession(props.sessionId);
  if (!session) return null;
  // The server answers 409 for every workspace request until the session is
  // bound to a runner (an imported or dormant agent), so don't ask yet.
  if (!session.runnerId) {
    return props.workspacePanel.open ? (
      <NotRunningPanel width={props.workspacePanel.width} />
    ) : null;
  }
  return <BoundTileWorkspace {...props} />;
}

function NotRunningPanel({ width }: { width: number }) {
  return (
    <aside
      aria-label="Workspace"
      data-testid="tiles-workspace-not-running"
      style={{ width }}
      className="flex shrink-0 flex-col gap-1 border-l px-3 py-2 text-muted-foreground text-sm"
    >
      <span className="flex items-center gap-1.5 font-medium text-foreground">
        <PauseCircleIcon className="size-3.5 shrink-0" aria-hidden />
        Agent isn't running
      </span>
      <span>Send it a message to start it, then its files appear here.</span>
    </aside>
  );
}

function BoundTileWorkspace({
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
