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
import { revealFolderInFilesPanel } from "@/shell/FilesPanel";
import { selectPullRequestInPanel } from "@/shell/PullRequestPanel";
import { citedFilePosition } from "@/shell/filePositionState";

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
  const closingTerminal = terminals.find((terminal) => terminal.id === deleteTerminal.variables);
  const [pendingClose, setPendingClose] = useState<string | null>(null);
  const terminalToClose = terminals.find((terminal) => terminalTabKey(terminal) === pendingClose);
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
  const [sort, setSort] = useState<ChangedSort>("recent");
  const [showHidden, setShowHidden] = useState(false);
  const [position, setPosition] = useState<FilePosition>();
  const navigationGuard = useRef<FileNavigationGuard | null>(null);
  const registerNavigationGuard = useCallback((guard: FileNavigationGuard) => {
    navigationGuard.current = guard;
    return () => {
      if (navigationGuard.current === guard) navigationGuard.current = null;
    };
  }, []);
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
    [navigateWorkspace, onOpenWorkspace],
  );
  useUiOpenFile(sessionId, selected && canBrowse, openFile);
  const showTab = useCallback((rightRailTab: RightRailTab | null) => {
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
  }, []);
  const selectTab = useCallback(
    (rightRailTab: RightRailTab | null) => {
      navigateWorkspace("", undefined, () => showTab(rightRailTab));
    },
    [navigateWorkspace, showTab],
  );
  const closeFile = useCallback(
    (path: string) => {
      navigateWorkspace("", undefined, () =>
        setWorkspace((previous) => ({
          ...previous,
          openFiles: previous.openFiles?.filter((file) => file !== path),
          selectedFilePath: previous.selectedFilePath === path ? null : previous.selectedFilePath,
        })),
      );
    },
    [navigateWorkspace],
  );
  const openTerminal = useCallback(
    (key: string) => {
      navigateWorkspace("", undefined, () => {
        onOpenWorkspace();
        updateWorkspace({ selectedFilePath: null, selectedTerminalKey: key });
      });
    },
    [navigateWorkspace, updateWorkspace, onOpenWorkspace],
  );
  const fileContext = useMemo(
    () => ({
      openFile,
      registerNavigationGuard,
      guardFileNavigation: navigateWorkspace,
      openGithubTab: (prUrl?: string) => {
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
    isChangedPath,
    terminals,
    agentTerminal,
    openTerminals,
    deleteTerminal,
    closingTerminal,
    pendingClose,
    setPendingClose,
    terminalToClose,
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
