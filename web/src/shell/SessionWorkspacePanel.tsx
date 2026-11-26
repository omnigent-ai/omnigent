// The Workspace panel for one session, wired to its `useSessionWorkspace` state.

import { Suspense, type ComponentProps } from "react";
import type { ConversationState } from "@/store/chatStore";
import type { Session } from "@/lib/types";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { supportsBrowser } from "@/lib/nativeBridge";
import type { useSessionLiveness } from "@/hooks/useSessionLiveness";
import { readInitialWorkspaceTab } from "@/lib/workspaceTabPreferences";
import { terminalTabKey } from "@/hooks/useTerminals";
import { CloseShellDialog } from "@/shell/CloseShellDialog";
import { WorkspacePanel } from "@/shell/WorkspacePanel";
import { LazyWorkspaceTabsHost } from "@/shell/workspaceTabsRouting";
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
    changedFiles,
    terminals,
    openTerminals,
    tabsSidebar,
    closingTerminalKeys,
    setPendingClose,
    workspace,
    setWorkspace,
    sort,
    setSort,
    showHidden,
    setShowHidden,
    position,
    openFile,
    selectTab,
    closeFile,
    openTerminal,
    rightRailTab,
  } = sessionWorkspace;
  const showTasksTab =
    (isFeatureEnabled(serverInfo, "workflows") &&
      serverInfo !== "loading" &&
      serverInfo.session_journal === true) ||
    (state.todos?.length ?? 0) > 0;
  if (tabsSidebar) {
    return (
      <Suspense fallback={null}>
        <LazyWorkspaceTabsHost
          contained
          keyboardTarget={selected}
          bindsHistory={selected}
          conversationId={sessionId}
          sessionHarness={state.sessionHarness}
          agentId={state.boundAgentId ?? session?.agentId ?? null}
          {...workspacePanel}
          open={selected && workspacePanel.open}
          inert={!selected || !workspacePanel.open}
          showFilesPanel={showFiles}
          showGithubTab={showFiles}
          showTasksTab={showTasksTab}
          showBrowserTab={supportsBrowser()}
          onReveal={onOpenWorkspace}
          changedCount={changedCount}
          changedFiles={changedFiles}
          subagentsWorking={childSessions.filter((child) => child.busy).length}
          agentCount={childSessions.length + 1}
          rootSessionId={rootSessionId}
          terminals={terminals}
          terminalKeys={session ? openTerminals : null}
          openTerminalTab={openTerminal}
          permissionLevel={permissionLevel}
          filesPanelSort={sort}
          onSortChange={setSort}
          filesPanelShowHidden={showHidden}
          onShowHiddenChange={setShowHidden}
          liveness={liveness}
        />
      </Suspense>
    );
  }
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
        });
      }}
      showFilesPanel={showFiles}
      showGithubTab={showFiles}
      showTasksTab={showTasksTab}
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
      closingTerminalKeys={closingTerminalKeys}
      onCloseTerminal={(key, callbacks) => setPendingClose({ keys: [key], callbacks })}
      onCloseTerminals={(keys, callbacks) => {
        if (keys.length > 0) setPendingClose({ keys, callbacks });
      }}
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

/** Confirms closing one or several of the session's shells, then kills them. */
export function SessionCloseShellDialog({
  sessionWorkspace,
}: {
  sessionWorkspace: SessionWorkspace;
}) {
  const {
    pendingClose,
    setPendingClose,
    terminalsToClose,
    setKillingTerminalIds,
    deleteTerminal,
    workspace,
    updateWorkspace,
  } = sessionWorkspace;
  return (
    <CloseShellDialog
      open={pendingClose !== null}
      shellCount={pendingClose?.keys.length}
      shellLabel={terminalsToClose.length === 1 ? terminalsToClose[0].name : null}
      onCancel={() => setPendingClose(null)}
      onConfirm={() => {
        pendingClose?.callbacks?.onConfirmed?.();
        for (const terminal of terminalsToClose) {
          const key = terminalTabKey(terminal);
          setKillingTerminalIds((ids) => [...ids, terminal.id]);
          // mutateAsync settles per call; mutate's own callbacks fire only for the last one.
          deleteTerminal
            .mutateAsync(terminal.id)
            .then(
              () => {
                if (workspace.selectedTerminalKey === key)
                  updateWorkspace({ selectedTerminalKey: null });
              },
              () => pendingClose?.callbacks?.onFailed?.(key),
            )
            .finally(() => setKillingTerminalIds((ids) => ids.filter((id) => id !== terminal.id)));
        }
        setPendingClose(null);
      }}
    />
  );
}
