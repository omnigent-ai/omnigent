import { useCallback, useEffect, useLayoutEffect, useMemo, useState, type RefObject } from "react";
import { useChildSessions } from "@/hooks/useChildSessions";
import { useConversations } from "@/hooks/useConversations";
import { useGithubInfo } from "@/hooks/useGithub";
import { useResizableInlinePanel } from "@/hooks/useResizableInlinePanel";
import { useRootSessionId, useSession } from "@/hooks/useSession";
import { terminalTabKey, useDeleteTerminal, useTerminals } from "@/hooks/useTerminals";
import {
  useWorkspaceChangedFiles,
  useWorkspaceEnvironment,
} from "@/hooks/useWorkspaceChangedFiles";
import { readFilesPanelPreferences } from "@/lib/filesPanelPreferences";
import { supportsBrowser } from "@/lib/nativeBridge";
import { derivePermissionLevel, isEditorLevel } from "@/lib/permissionsApi";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { CloseShellDialog } from "./CloseShellDialog";
import type { ChangedSort } from "./FlatFileList";
import type { RightRailTab } from "./railTabs";
import { SessionDockSplitter, SESSION_WORKSPACE_BASIS_VAR } from "./SessionDockSplitter";
import { WorkspacePanel } from "./WorkspacePanel";

interface SessionWorkspaceDockProps {
  conversationId: string;
  dockRef: RefObject<HTMLDivElement | null>;
  label: string;
  onCollapse: () => void;
}

export function SessionWorkspaceDock({
  conversationId,
  dockRef,
  label,
  onCollapse,
}: SessionWorkspaceDockProps) {
  const persisted = useMemo(() => readSessionWorkspaceState(conversationId), [conversationId]);
  const [rightRailTab, setRightRailTab] = useState<RightRailTab>(
    () => persisted.rightRailTab ?? "files",
  );
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(
    () => persisted.selectedFilePath ?? null,
  );
  const [openFiles, setOpenFiles] = useState<string[]>(() => persisted.openFiles ?? []);
  const [selectedTerminalKey, setSelectedTerminalKey] = useState<string | null>(
    () => persisted.selectedTerminalKey ?? null,
  );
  const [filesPanelShowHidden, setFilesPanelShowHidden] = useState(true);
  const [filesPanelSort, setFilesPanelSort] = useState<ChangedSort>(
    () => readFilesPanelPreferences().sort,
  );
  const [commentsOpen, setCommentsOpen] = useState(false);
  const [maximized, setMaximized] = useState(false);
  const [terminalPendingClose, setTerminalPendingClose] = useState<string | null>(null);
  const [paneSizePct, setPaneSizePct] = useState(() => persisted.paneSizePct ?? 45);

  const { data: conversationsData } = useConversations("", true);
  const listedConversation = useMemo(
    () =>
      conversationsData?.pages
        .flatMap((page) => page.data)
        .find((conversation) => conversation.id === conversationId) ?? null,
    [conversationId, conversationsData],
  );
  const { session, isLoading: sessionLoading } = useSession(conversationId);
  const permissionLevel = derivePermissionLevel(
    session,
    sessionLoading,
    listedConversation,
    conversationId,
    conversationsData !== undefined,
  );
  const canBrowseWorkspace =
    isEditorLevel(permissionLevel) || session?.shareWorkspaceFiles === true;
  const rootSessionId = useRootSessionId(
    conversationId,
    sessionLoading ? undefined : (session?.parentSessionId ?? null),
  );
  const { children: childSessions } = useChildSessions(rootSessionId);
  const environmentQuery = useWorkspaceEnvironment(conversationId, {
    enabled: canBrowseWorkspace,
  });
  const changedFilesQuery = useWorkspaceChangedFiles(conversationId);
  const showFilesPanel = canBrowseWorkspace && environmentQuery.data?.available !== false;
  const githubInfoQuery = useGithubInfo(conversationId);
  const showGithubTab = showFilesPanel && githubInfoQuery.data?.reason !== "not_a_git_repo";
  const showBrowserTab = supportsBrowser();
  const changedCount = changedFilesQuery.data?.data.length ?? 0;
  const subagentsWorking = childSessions.filter((child) => child.busy).length;
  const agentCount = childSessions.length + 1;

  const { terminals } = useTerminals(conversationId);
  const openTerminals = useMemo(() => terminals.map(terminalTabKey), [terminals]);
  const deleteTerminal = useDeleteTerminal(conversationId);
  const closingTerminalKey =
    deleteTerminal.isPending && deleteTerminal.variables
      ? `terminal:${deleteTerminal.variables}`
      : null;

  const inlinePanelMinWidth =
    (rightRailTab === "files" || rightRailTab === "changes") && commentsOpen ? 720 : undefined;
  const { panelWidth, handleProps } = useResizableInlinePanel(
    conversationId,
    inlinePanelMinWidth,
    0,
    true,
  );

  useEffect(() => {
    writeSessionWorkspaceState(conversationId, {
      rightRailTab,
      selectedFilePath,
      openFiles,
      selectedTerminalKey,
    });
  }, [conversationId, openFiles, rightRailTab, selectedFilePath, selectedTerminalKey]);

  useLayoutEffect(() => {
    dockRef.current?.style.setProperty(SESSION_WORKSPACE_BASIS_VAR, `${paneSizePct}%`);
  }, [dockRef, paneSizePct]);

  const railTabsAvailable = useMemo(
    () => ({
      files: showFilesPanel,
      changes: showFilesPanel,
      github: showGithubTab,
      subagents: true,
      browser: showBrowserTab,
    }),
    [showBrowserTab, showFilesPanel, showGithubTab],
  );

  useEffect(() => {
    if (railTabsAvailable[rightRailTab]) return;
    const next = (["files", "changes", "github", "subagents", "browser"] as const).find(
      (tab) => railTabsAvailable[tab],
    );
    if (next) setRightRailTab(next);
  }, [railTabsAvailable, rightRailTab]);

  const openFileViewer = useCallback((path: string) => {
    setOpenFiles((current) => (current.includes(path) ? current : [...current, path]));
    setSelectedFilePath(path);
    setSelectedTerminalKey(null);
    setRightRailTab("files");
  }, []);

  const closeFile = useCallback(
    (path: string) => {
      setOpenFiles((current) => {
        const index = current.indexOf(path);
        const next = current.filter((candidate) => candidate !== path);
        if (selectedFilePath === path) {
          setSelectedFilePath(next[index - 1] ?? next[index] ?? null);
        }
        return next;
      });
    },
    [selectedFilePath],
  );

  const showScopeView = useCallback(() => {
    setSelectedFilePath(null);
    setCommentsOpen(false);
  }, []);

  const changeRailTab = useCallback((next: RightRailTab) => {
    setRightRailTab(next);
    setSelectedFilePath(null);
    setSelectedTerminalKey(null);
    setCommentsOpen(false);
  }, []);

  const openTerminalTab = useCallback((key: string) => {
    setSelectedTerminalKey(key);
    setSelectedFilePath(null);
    setCommentsOpen(false);
  }, []);

  const confirmCloseTerminal = useCallback(() => {
    const key = terminalPendingClose;
    if (key === null) return;
    setTerminalPendingClose(null);
    const terminal = terminals.find((candidate) => terminalTabKey(candidate) === key);
    if (terminal) deleteTerminal.mutate(terminal.id);
    if (selectedTerminalKey === key) setSelectedTerminalKey(null);
  }, [deleteTerminal, selectedTerminalKey, terminalPendingClose, terminals]);

  const pendingTerminal = terminals.find(
    (terminal) => terminalTabKey(terminal) === terminalPendingClose,
  );
  const pendingTerminalLabel = pendingTerminal
    ? pendingTerminal.session
      ? `${pendingTerminal.name} · ${pendingTerminal.session}`
      : pendingTerminal.name
    : null;

  const resizePane = useCallback(
    (nextSizePct: number) => {
      setPaneSizePct(nextSizePct);
      writeSessionWorkspaceState(conversationId, { paneSizePct: nextSizePct });
    },
    [conversationId],
  );

  return (
    <>
      <SessionDockSplitter
        dockRef={dockRef}
        label={label}
        sizePct={paneSizePct}
        onResize={resizePane}
        onCollapse={onCollapse}
      />
      <WorkspacePanel
        conversationId={conversationId}
        ariaLabel={`Workspace for ${label}`}
        variant="session-column"
        width={panelWidth}
        handleProps={handleProps}
        rightRailTab={rightRailTab}
        onRightRailTabChange={changeRailTab}
        showFilesPanel={showFilesPanel}
        showGithubTab={showGithubTab}
        showBrowserTab={showBrowserTab}
        changedCount={changedCount}
        subagentsWorking={subagentsWorking}
        agentCount={agentCount}
        rootSessionId={rootSessionId}
        selectedFilePath={selectedFilePath}
        openFiles={openFiles}
        openFileViewer={openFileViewer}
        onCloseFile={closeFile}
        onShowScopeView={showScopeView}
        onCommentsOpenChange={setCommentsOpen}
        openTerminalTab={openTerminalTab}
        openTerminals={openTerminals}
        selectedTerminalKey={selectedTerminalKey}
        closingTerminalKey={closingTerminalKey}
        onCloseTerminal={setTerminalPendingClose}
        maximized={maximized}
        onToggleMaximized={() => setMaximized((current) => !current)}
        onCollapse={onCollapse}
        permissionLevel={permissionLevel}
        filesPanelSort={filesPanelSort}
        onSortChange={setFilesPanelSort}
        filesPanelShowHidden={filesPanelShowHidden}
        onShowHiddenChange={setFilesPanelShowHidden}
      />
      <CloseShellDialog
        open={terminalPendingClose !== null}
        shellLabel={pendingTerminalLabel}
        onConfirm={confirmCloseTerminal}
        onCancel={() => setTerminalPendingClose(null)}
      />
    </>
  );
}
