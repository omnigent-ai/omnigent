// The Workspace panel for one session, wired to its `useSessionWorkspace` state.

import type { ComponentProps } from "react";
import type { ConversationState } from "@/store/chatStore";
import type { Session } from "@/lib/types";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { supportsBrowser } from "@/lib/nativeBridge";
import { terminalTabKey } from "@/hooks/useTerminals";
import type { useSessionLiveness } from "@/hooks/useSessionLiveness";
import { readInitialWorkspaceTab } from "@/lib/workspaceTabPreferences";
import { WorkspacePanel } from "@/shell/WorkspacePanel";
import type { SessionWorkspace } from "@/shell/useSessionWorkspace";

export type SharedWorkspacePanelProps = Pick<
  ComponentProps<typeof WorkspacePanel>,
  "open" | "width" | "handleProps" | "maximized" | "onToggleMaximized"
>;

interface SessionWorkspacePanelProps {
  sessionId: string;
  selected: boolean;
  workspacePanel: SharedWorkspacePanelProps;
  onOpenWorkspace: () => void;
  sessionWorkspace: SessionWorkspace;
  state: ConversationState;
  session: Session | null | undefined;
  childSessions: readonly { busy?: boolean }[];
  rootSessionId: string | null;
  liveness: ReturnType<typeof useSessionLiveness>;
  permissionLevel: number | null;
}

export function SessionWorkspacePanel({
  sessionId,
  selected,
  workspacePanel,
  onOpenWorkspace,
  sessionWorkspace,
  state,
  session,
  childSessions,
  rootSessionId,
  liveness,
  permissionLevel,
}: SessionWorkspacePanelProps) {
  const serverInfo = useServerInfo();
  const {
    canBrowse,
    showFiles,
    changedCount,
    openTerminals,
    deleteTerminal,
    closingTerminal,
    setPendingClose,
    workspace,
    setWorkspace,
    sort,
    setSort,
    showHidden,
    setShowHidden,
    position,
    navigateWorkspace,
    openFile,
    selectTab,
    closeFile,
    openTerminal,
    rightRailTab,
  } = sessionWorkspace;
  return (
    <WorkspacePanel
      contained
      keyboardTarget={selected}
      conversationId={sessionId}
      sessionHarness={state.sessionHarness}
      agentId={state.boundAgentId ?? session?.agentId ?? null}
      {...workspacePanel}
      open={selected && workspacePanel.open}
      inert={!selected || !workspacePanel.open}
      rightRailTab={rightRailTab}
      onRightRailTabChange={selectTab}
      openPanels={workspace.openPanels ?? []}
      onClosePanel={(tab) => {
        navigateWorkspace("", undefined, () =>
          setWorkspace((previous) => {
            const remaining = previous.openPanels?.filter((panel) => panel !== tab) ?? [];
            return {
              ...previous,
              openPanels: remaining,
              rightRailTab:
                previous.rightRailTab === tab
                  ? (remaining.at(-1) ?? readInitialWorkspaceTab())
                  : previous.rightRailTab,
            };
          }),
        );
      }}
      showFilesPanel={showFiles}
      showGithubTab={showFiles}
      showTasksTab={
        (isFeatureEnabled(serverInfo, "workflows") &&
          serverInfo !== "loading" &&
          serverInfo.session_journal === true) ||
        (state.todos?.length ?? 0) > 0
      }
      showBrowserTab={supportsBrowser()}
      onReveal={onOpenWorkspace}
      changedCount={changedCount}
      subagentsWorking={childSessions.filter((child) => child.busy).length}
      agentCount={childSessions.length + 1}
      rootSessionId={rootSessionId}
      selectedFilePath={canBrowse ? (workspace.selectedFilePath ?? null) : null}
      filePosition={position}
      openFiles={canBrowse ? (workspace.openFiles ?? []) : []}
      openFileViewer={openFile}
      onCloseFile={closeFile}
      onShowScopeView={() => selectTab(rightRailTab)}
      onCommentsOpenChange={() => {}}
      openTerminalTab={openTerminal}
      openTerminals={openTerminals}
      selectedTerminalKey={
        openTerminals.includes(workspace.selectedTerminalKey ?? "")
          ? (workspace.selectedTerminalKey ?? null)
          : null
      }
      closingTerminalKey={
        deleteTerminal.isPending && closingTerminal ? terminalTabKey(closingTerminal) : null
      }
      onCloseTerminal={setPendingClose}
      maximized={selected && workspacePanel.maximized}
      permissionLevel={permissionLevel}
      filesPanelSort={sort}
      onSortChange={setSort}
      filesPanelShowHidden={showHidden}
      onShowHiddenChange={setShowHidden}
      liveness={liveness}
    />
  );
}
