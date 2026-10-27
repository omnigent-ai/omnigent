import { useCallback, useMemo } from "react";
import {
  findAgentTerminal,
  inventoryTerminals,
  terminalTabKey,
  useTerminals,
} from "@/hooks/useTerminals";
import {
  useWorkspaceChangedFiles,
  useWorkspaceEnvironment,
} from "@/hooks/useWorkspaceChangedFiles";
import { isEditorLevel } from "@/lib/permissionsApi";

/** Shared discovery for the full-session shell and grid workspaces. */
export function useWorkspaceResources({
  sessionId,
  permissionLevel,
  shareWorkspaceFiles,
  terminalFirst = false,
  terminalPending = false,
}: {
  sessionId: string | undefined;
  permissionLevel: number | null;
  shareWorkspaceFiles?: boolean;
  terminalFirst?: boolean;
  terminalPending?: boolean;
}) {
  // View grants don't include files. Unknown levels stay permissive; the server enforces access.
  const canBrowseWorkspace = isEditorLevel(permissionLevel) || shareWorkspaceFiles === true;
  const environmentQuery = useWorkspaceEnvironment(sessionId, { enabled: canBrowseWorkspace });
  const changedFiles = useWorkspaceChangedFiles(sessionId, { enabled: canBrowseWorkspace });
  const changedPaths = useMemo(
    () => new Set(changedFiles.data?.data.map((file) => file.path)),
    [changedFiles.data],
  );
  const isChangedPath = useCallback((path: string) => changedPaths.has(path), [changedPaths]);
  const { terminals } = useTerminals(sessionId ?? null, { reconcileWhilePending: terminalPending });
  const agentTerminal = useMemo(() => findAgentTerminal(terminals), [terminals]);
  // Hide the agent terminal from shells only when it has its own Terminal view.
  const openTerminals = useMemo(
    () => inventoryTerminals(terminals, terminalFirst).map(terminalTabKey),
    [terminals, terminalFirst],
  );
  return {
    canBrowseWorkspace,
    environmentQuery,
    showFilesPanel: canBrowseWorkspace && environmentQuery.data?.available !== false,
    changedCount: changedFiles.data?.data.length ?? 0,
    isChangedPath,
    terminals,
    agentTerminal,
    openTerminals,
  };
}
