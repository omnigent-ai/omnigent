import { useCallback, useEffect, useMemo, useRef, useState, type ComponentProps } from "react";
import { createPortal } from "react-dom";
import { useDraggable, useDroppable } from "@dnd-kit/core";
import { cn } from "@/lib/utils";
import { Maximize2Icon, XIcon } from "lucide-react";
import { SideChatPane } from "@/components/chat/SideChatPane";
import { Button } from "@/components/ui/button";
import { useBrowserAgentRelay } from "@/hooks/useBrowserAgentRelay";
import { useChildSessions } from "@/hooks/useChildSessions";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import { useCanEdit } from "@/hooks/usePermissions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { useSession, useRootSessionId } from "@/hooks/useSession";
import { livenessRowFromSession, useSessionLiveness } from "@/hooks/useSessionLiveness";
import { terminalTabKey, useDeleteTerminal } from "@/hooks/useTerminals";
import { useWorkspaceResources } from "@/shell/useWorkspaceResources";
import { supportsBrowser } from "@/lib/nativeBridge";
import { Link } from "@/lib/routing";
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
import { CloseShellDialog } from "@/shell/CloseShellDialog";
import {
  FileViewerContext,
  type FileNavigationGuard,
  type FilePosition,
  type OpenFileOptions,
} from "@/shell/FileViewerContext";
import type { RightRailTab } from "@/shell/railTabs";
import type { ChangedSort } from "@/shell/FlatFileList";
import { WorkspacePanel } from "@/shell/WorkspacePanel";
import { useGridSessions } from "./GridSessionsContext";

export type GridWorkspacePanelProps = Pick<
  ComponentProps<typeof WorkspacePanel>,
  "open" | "width" | "handleProps" | "maximized" | "onToggleMaximized"
>;

export function GridSessionPane({
  sessionId,
  area,
  selected,
  onActivate,
  workspaceTarget,
  workspacePanel,
  onOpenWorkspace,
}: {
  sessionId: string;
  area?: string;
  selected: boolean;
  onActivate: () => void;
  workspaceTarget: HTMLDivElement | null;
  workspacePanel: GridWorkspacePanelProps;
  onOpenWorkspace: () => void;
}) {
  const { session } = useSession(sessionId);
  const state = useConversationEntryState(sessionId);
  const canEdit = useCanEdit(sessionId);
  const permissionLevel = session?.permissionLevel ?? (canEdit ? null : 1);
  const grids = useGridSessions()!;
  const title = session?.title || "Session";
  const {
    attributes,
    listeners,
    setNodeRef: setDragNodeRef,
    setActivatorNodeRef,
    isDragging,
  } = useDraggable({ id: sessionId, data: { title } });
  const {
    setNodeRef: setDropNodeRef,
    active,
    isOver,
  } = useDroppable({ id: sessionId, data: { title } });
  const setPaneRef = useCallback(
    (node: HTMLElement | null) => {
      setDragNodeRef(node);
      setDropNodeRef(node);
    },
    [setDragNodeRef, setDropNodeRef],
  );
  const healthSessions = useMemo(() => [{ id: sessionId }], [sessionId]);
  useRunnerHealthRegistration(healthSessions);
  useBrowserAgentRelay(sessionId);
  const liveness = useSessionLiveness(sessionId, livenessRowFromSession(session));
  const rootSessionId = useRootSessionId(sessionId, session?.parentSessionId);
  const { children: childSessions } = useChildSessions(rootSessionId);
  const {
    canBrowseWorkspace: canBrowse,
    environmentQuery: environment,
    showFilesPanel: showFiles,
    changedCount,
    isChangedPath,
    terminals,
    openTerminals,
  } = useWorkspaceResources({
    sessionId,
    permissionLevel,
    shareWorkspaceFiles: session?.shareWorkspaceFiles,
    terminalPending: state.terminalPending,
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
    (path: string, options: OpenFileOptions | undefined, apply: () => void) => {
      if (navigationGuard.current) navigationGuard.current(path, options, apply);
      else apply();
    },
    [],
  );
  const openFile = useCallback(
    (path: string, options?: OpenFileOptions) => {
      navigateWorkspace(path, options, () => {
        onOpenWorkspace();
        setPosition(
          options?.line === undefined ? undefined : { line: options.line, column: options.column },
        );
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
  const selectTab = useCallback(
    (rightRailTab: RightRailTab | null) => {
      navigateWorkspace("", undefined, () =>
        setWorkspace((previous) => ({
          ...previous,
          rightRailTab,
          selectedFilePath: null,
          selectedTerminalKey: null,
          openPanels:
            isDefaultWorkspaceTab(rightRailTab) && !readPinnedWorkspaceTabs().includes(rightRailTab)
              ? [...new Set([...(previous.openPanels ?? []), rightRailTab])]
              : previous.openPanels,
        })),
      );
    },
    [navigateWorkspace],
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
      openGithubTab: () => {
        selectTab("github");
        onOpenWorkspace();
      },
      conversationId: sessionId,
      workspaceRoot: environment.data?.root ?? null,
      workspaceHome: environment.data?.home ?? null,
      sessionHostId: session?.hostId,
      isChangedPath,
    }),
    [
      openFile,
      registerNavigationGuard,
      selectTab,
      onOpenWorkspace,
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
  return (
    <FileViewerContext.Provider value={fileContext}>
      <section
        ref={setPaneRef}
        style={{ gridArea: area }}
        data-grid-session={sessionId}
        data-active={selected}
        aria-label={`${title}${selected ? " (active)" : ""}`}
        onClickCapture={onActivate}
        onFocusCapture={onActivate}
        className={cn(
          "relative flex min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border bg-background",
          selected && "border-brand-accent/50 ring-1 ring-brand-accent/50",
          isDragging && "opacity-50",
        )}
      >
        <header
          className={cn(
            "flex h-8 shrink-0 items-center border-b px-1",
            selected && "bg-brand-accent/50",
          )}
        >
          <button
            type="button"
            ref={setActivatorNodeRef}
            {...attributes}
            {...listeners}
            aria-label={`Move ${title}`}
            title="Drag to another cell to swap positions"
            className="h-full min-w-0 flex-1 touch-none cursor-grab truncate px-1 text-left text-sm leading-normal font-medium select-none active:cursor-grabbing focus-visible:outline-2 focus-visible:outline-ring"
          >
            {title}
          </button>
          <Button asChild variant="ghost" size="icon-xs">
            <Link draggable={false} to={`/c/${sessionId}`} aria-label={`Open ${title} full screen`}>
              <Maximize2Icon className="size-3.5" />
            </Link>
          </Button>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`Remove ${title} from grid`}
            onClick={() => grids.closePane(sessionId)}
          >
            <XIcon className="size-4" />
          </Button>
        </header>
        <div className="min-h-0 min-w-0 flex-1 overflow-hidden">
          <SideChatPane childId={sessionId} fullSession readOnly={!canEdit} />
        </div>
        {isOver && active?.id !== sessionId && (
          <div
            data-testid="grid-move-ghost"
            className="pointer-events-none absolute inset-0 z-50 flex items-center justify-center rounded-lg border-2 border-dashed border-brand-accent bg-brand-accent/25"
          >
            {/* <span className="rounded-md border bg-background/95 px-3 py-2 text-sm shadow-sm"> */}
            {/* Swap with {title} */}
            {/* </span> */}
          </div>
        )}
        <CloseShellDialog
          open={pendingClose !== null}
          shellLabel={terminalToClose?.name ?? null}
          onCancel={() => setPendingClose(null)}
          onConfirm={() => {
            if (terminalToClose)
              deleteTerminal.mutate(terminalToClose.id, {
                onSuccess: () => {
                  if (workspace.selectedTerminalKey === pendingClose)
                    updateWorkspace({ selectedTerminalKey: null });
                },
              });
            setPendingClose(null);
          }}
        />
      </section>
      {/* Keep inactive views mounted so switching cells preserves unsaved edits. */}
      {workspaceTarget &&
        createPortal(
          <div className={selected && workspacePanel.open ? "contents" : "hidden"}>
            <WorkspacePanel
              contained
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
              selectedTerminalKey={workspace.selectedTerminalKey ?? null}
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
          </div>,
          workspaceTarget,
        )}
    </FileViewerContext.Provider>
  );
}
