// Per-session Workspace state (files, tabs, terminals, navigation guards) shared
// by Grid cells and the Tile view's drawer, so both behave the same.

import { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { ConversationState } from "@/store/chatStore";
import type { Session } from "@/lib/types";
import { useUiOpenFile } from "@/hooks/useUiOpenFile";
import { terminalTabKey, useDeleteTerminal } from "@/hooks/useTerminals";
import { useWorkspaceResources } from "@/shell/useWorkspaceResources";
import {
  readSessionWorkspaceState,
  writeSessionWorkspaceState,
  type SessionWorkspaceState,
} from "@/lib/sessionWorkspaceState";
import {
  readInitialWorkspaceTab,
  readPinnedWorkspaceTabs,
  isDefaultWorkspaceTab,
} from "@/lib/workspaceTabPreferences";
import {
  FileViewerContext,
  type FileNavigationGuard,
  type FilePosition,
  type OpenFileOptions,
} from "@/shell/FileViewerContext";
import type { RightRailTab } from "@/shell/railTabs";
import type { ChangedSort } from "@/shell/FlatFileList";
import type { ShellCloseCallbacks } from "@/shell/WorkspacePanel";
import { revealFolderInFilesPanel } from "@/shell/FilesPanel";
import { selectPullRequestInPanel } from "@/shell/PullRequestPanel";
import { citedFilePosition } from "@/shell/filePositionState";
import { routeToWorkspaceTabs, useTabsSidebar } from "@/shell/workspaceTabsRouting";
import { DEFAULT_FILES_PANEL_PREFERENCES } from "@/lib/filesPanelPreferences";

export interface UseSessionWorkspaceOptions {
  sessionId: string;
  session: Session | null | undefined;
  state: ConversationState;
  permissionLevel: number | null;
  terminalFirst: boolean;
  /** Whether this session owns the shared panel (and the UI's open-file requests). */
  selected: boolean;
  onOpenWorkspace: () => void;
}

export function useSessionWorkspace({
  sessionId,
  session,
  state,
  permissionLevel,
  terminalFirst,
  selected,
  onOpenWorkspace,
}: UseSessionWorkspaceOptions) {
  const queryClient = useQueryClient();
  const {
    canBrowseWorkspace: canBrowse,
    environmentQuery: environment,
    showFilesPanel: showFiles,
    changedCount,
    changedFiles,
    isChangedPath,
    terminals,
    agentTerminal,
    openTerminals,
  } = useWorkspaceResources({
    sessionId,
    permissionLevel,
    shareWorkspaceFiles: session?.shareWorkspaceFiles,
    terminalPending: state.terminalPending,
    terminalFirst,
  });
  const deleteTerminal = useDeleteTerminal(sessionId);
  // Shells whose kill is in flight; a bulk close kills several at once, and the
  // mutation's ``variables`` holds only the last.
  const [killingTerminalIds, setKillingTerminalIds] = useState<readonly string[]>([]);
  const deletingTerminalId = deleteTerminal.isPending ? deleteTerminal.variables : undefined;
  const closingTerminalKeys = useMemo(
    () =>
      terminals
        .filter(
          (terminal) =>
            killingTerminalIds.includes(terminal.id) || terminal.id === deletingTerminalId,
        )
        .map(terminalTabKey),
    [terminals, killingTerminalIds, deletingTerminalId],
  );
  const [pendingClose, setPendingClose] = useState<{
    keys: string[];
    callbacks?: ShellCloseCallbacks;
  } | null>(null);
  const terminalsToClose = terminals.filter((terminal) =>
    pendingClose?.keys.includes(terminalTabKey(terminal)),
  );
  const [workspace, setWorkspace] = useState<SessionWorkspaceState>(() => {
    const saved = readSessionWorkspaceState(sessionId);
    return {
      rightRailTab: saved.rightRailTab ?? readInitialWorkspaceTab(),
      openPanels: saved.openPanels ?? [],
      openFiles: saved.openFiles ?? [],
      selectedFilePath: saved.selectedFilePath ?? null,
      selectedTerminalKey: saved.selectedTerminalKey ?? null,
    };
  });
  useEffect(() => writeSessionWorkspaceState(sessionId, workspace), [sessionId, workspace]);
  const updateWorkspace = useCallback((patch: SessionWorkspaceState) => {
    setWorkspace((previous) => ({ ...previous, ...patch }));
  }, []);
  const [sort, setSort] = useState<ChangedSort>(DEFAULT_FILES_PANEL_PREFERENCES.sort);
  const [showHidden, setShowHidden] = useState(false);
  const [position, setPosition] = useState<FilePosition>();
  const navigationGuard = useRef<FileNavigationGuard | null>(null);
  const registerNavigationGuard = useCallback((guard: FileNavigationGuard) => {
    navigationGuard.current = guard;
    return () => {
      if (navigationGuard.current === guard) navigationGuard.current = null;
    };
  }, []);
  // With the workspace_sidebar_v2 flag, the sidebar shows as tabs, where its files,
  // pages and shells open.
  const tabsSidebar = useTabsSidebar();
  const navigateWorkspace = useCallback(
    (path: string | null, options: OpenFileOptions | undefined, apply: () => void) => {
      if (navigationGuard.current) navigationGuard.current(path, options, apply);
      else apply();
    },
    [],
  );
  // Leaving the grid (the shell's Close) asks this cell's viewer about unsaved edits too.
  const registerShellNavigationGuard = useContext(FileViewerContext)?.registerNavigationGuard;
  useEffect(
    () =>
      registerShellNavigationGuard?.((path, options, navigate) => {
        if (path === null) navigateWorkspace(null, options, navigate);
        else navigate();
      }),
    [registerShellNavigationGuard, navigateWorkspace],
  );
  const openFile = useCallback(
    (path: string, options?: OpenFileOptions) => {
      if (tabsSidebar) {
        onOpenWorkspace();
        void routeToWorkspaceTabs((tabs) =>
          tabs.openWorkspaceFileLink(sessionId, path, options, "user"),
        );
        return;
      }
      navigateWorkspace(path, options, () => {
        onOpenWorkspace();
        setPosition(citedFilePosition(options));
        setWorkspace((previous) => ({
          ...previous,
          selectedFilePath: path,
          selectedTerminalKey: null,
          openFiles: previous.openFiles?.includes(path)
            ? previous.openFiles
            : [...(previous.openFiles ?? []), path],
        }));
      });
    },
    [tabsSidebar, sessionId, navigateWorkspace, onOpenWorkspace],
  );
  useUiOpenFile(sessionId, selected && canBrowse, openFile);
  const showTab = useCallback(
    (rightRailTab: RightRailTab | null) => {
      if (tabsSidebar) {
        void routeToWorkspaceTabs(async (tabs) =>
          tabs.isWorkspacePage(rightRailTab)
            ? tabs.openWorkspacePage(sessionId, rightRailTab)
            : { ok: true },
        );
        return;
      }
      setWorkspace((previous) => ({
        ...previous,
        rightRailTab,
        selectedFilePath: null,
        selectedTerminalKey: null,
        openPanels:
          isDefaultWorkspaceTab(rightRailTab) && !readPinnedWorkspaceTabs().includes(rightRailTab)
            ? [...new Set([...(previous.openPanels ?? []), rightRailTab])]
            : previous.openPanels,
      }));
    },
    [tabsSidebar, sessionId],
  );
  const selectTab = useCallback(
    (rightRailTab: RightRailTab | null) => {
      navigateWorkspace("", undefined, () => showTab(rightRailTab));
    },
    [navigateWorkspace, showTab],
  );
  // The workspace panel already asks the file on screen about unsaved edits before closing
  // it, and background tabs have no viewer, so closing a tab drops it without asking again.
  const closeFile = useCallback((path: string) => {
    setWorkspace((previous) => ({
      ...previous,
      openFiles: previous.openFiles?.filter((file) => file !== path),
      selectedFilePath: previous.selectedFilePath === path ? null : previous.selectedFilePath,
    }));
  }, []);
  const openTerminal = useCallback(
    (key: string) => {
      if (tabsSidebar) {
        onOpenWorkspace();
        void routeToWorkspaceTabs((tabs) => tabs.openWorkspaceTerminal(sessionId, key));
        return;
      }
      navigateWorkspace("", undefined, () => {
        onOpenWorkspace();
        updateWorkspace({ selectedFilePath: null, selectedTerminalKey: key });
      });
    },
    [tabsSidebar, sessionId, navigateWorkspace, updateWorkspace, onOpenWorkspace],
  );
  const fileContext = useMemo(
    () => ({
      openFile,
      registerNavigationGuard,
      guardFileNavigation: navigateWorkspace,
      openGithubTab: (prUrl?: string) => {
        if (tabsSidebar) {
          onOpenWorkspace();
          void routeToWorkspaceTabs((tabs) =>
            tabs.showWorkspacePullRequests(
              sessionId,
              prUrl ? { kind: "pull", url: prUrl, overview: true } : { kind: "overview" },
            ),
          );
          return;
        }
        navigateWorkspace("", undefined, () => {
          if (prUrl) selectPullRequestInPanel(queryClient, sessionId, prUrl);
          showTab("github");
          onOpenWorkspace();
        });
      },
      openFolder: showFiles
        ? (absolutePath: string) =>
            navigateWorkspace(absolutePath, undefined, () => {
              revealFolderInFilesPanel(sessionId, absolutePath);
              showTab("files");
              onOpenWorkspace();
            })
        : undefined,
      conversationId: sessionId,
      workspaceRoot: environment.data?.root ?? null,
      workspaceHome: environment.data?.home ?? null,
      sessionHostId: session?.hostId,
      isChangedPath,
    }),
    [
      tabsSidebar,
      openFile,
      registerNavigationGuard,
      queryClient,
      navigateWorkspace,
      showTab,
      onOpenWorkspace,
      showFiles,
      sessionId,
      environment.data,
      session?.hostId,
      isChangedPath,
    ],
  );
  const rightRailTab =
    !showFiles && ["files", "changes", "github"].includes(workspace.rightRailTab ?? "")
      ? "subagents"
      : (workspace.rightRailTab ?? null);
  return {
    canBrowse,
    environment,
    showFiles,
    changedCount,
    changedFiles,
    isChangedPath,
    terminals,
    agentTerminal,
    openTerminals,
    tabsSidebar,
    deleteTerminal,
    closingTerminalKeys,
    setKillingTerminalIds,
    pendingClose,
    setPendingClose,
    terminalsToClose,
    workspace,
    setWorkspace,
    updateWorkspace,
    sort,
    setSort,
    showHidden,
    setShowHidden,
    position,
    navigateWorkspace,
    openFile,
    showTab,
    selectTab,
    closeFile,
    openTerminal,
    fileContext,
    rightRailTab,
    registerNavigationGuard,
  };
}

export type SessionWorkspace = ReturnType<typeof useSessionWorkspace>;
