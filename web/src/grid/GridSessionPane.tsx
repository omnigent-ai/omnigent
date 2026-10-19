import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useDraggable, useDroppable } from "@dnd-kit/core";
import { cn } from "@/lib/utils";
import { Maximize2Icon, PanelRightCloseIcon, PanelRightOpenIcon, XIcon } from "lucide-react";
import { SideChatPane } from "@/components/chat/SideChatPane";
import { Button } from "@/components/ui/button";
import { useBrowserAgentRelay } from "@/hooks/useBrowserAgentRelay";
import { useChildSessions } from "@/hooks/useChildSessions";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import { useCanEdit } from "@/hooks/usePermissions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { useSession, useRootSessionId } from "@/hooks/useSession";
import { livenessRowFromSession, useSessionLiveness } from "@/hooks/useSessionLiveness";
import { terminalTabKey, useTerminals, useDeleteTerminal } from "@/hooks/useTerminals";
import {
  useWorkspaceChangedFiles,
  useWorkspaceEnvironment,
} from "@/hooks/useWorkspaceChangedFiles";
import { supportsBrowser } from "@/lib/nativeBridge";
import { Link } from "@/lib/routing";
import {
  readSessionWorkspaceState,
  writeSessionWorkspaceState,
  type SessionWorkspaceState,
} from "@/lib/sessionWorkspaceState";
import { readDefaultWorkspaceTab } from "@/lib/workspaceTabPreferences";
import { CloseShellDialog } from "@/shell/CloseShellDialog";
import {
  FileViewerContext,
  type FileNavigationGuard,
  type FilePosition,
  type OpenFileOptions,
} from "@/shell/FileViewerContext";
import type { ChangedSort } from "@/shell/FlatFileList";
import { WorkspacePanel } from "@/shell/WorkspacePanel";
import { useGridSessions } from "./GridSessionsContext";

export function GridSessionPane({ sessionId, area }: { sessionId: string; area?: string }) {
  const { session } = useSession(sessionId);
  const state = useConversationEntryState(sessionId);
  const canEdit = useCanEdit(sessionId);
  const permissionLevel = session?.permissionLevel ?? (canEdit ? null : 1);
  const canBrowse = canEdit || session?.shareWorkspaceFiles === true;
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
  const environment = useWorkspaceEnvironment(sessionId, { enabled: canBrowse });
  const changedFiles = useWorkspaceChangedFiles(sessionId, { enabled: canBrowse });
  const showFiles = canBrowse && environment.data?.available !== false;
  const { terminals } = useTerminals(sessionId);
  const deleteTerminal = useDeleteTerminal(sessionId);
  const closingTerminal = terminals.find((terminal) => terminal.id === deleteTerminal.variables);
  const [pendingClose, setPendingClose] = useState<string | null>(null);
  const terminalToClose = terminals.find((terminal) => terminalTabKey(terminal) === pendingClose);
  const [workspace, setWorkspace] = useState<SessionWorkspaceState>(() => {
    const saved = readSessionWorkspaceState(sessionId);
    return {
      open: saved.open ?? false,
      widthPx: saved.widthPx,
      rightRailTab: saved.rightRailTab ?? readDefaultWorkspaceTab(),
      openFiles: saved.openFiles ?? [],
      selectedFilePath: saved.selectedFilePath ?? null,
      selectedTerminalKey: saved.selectedTerminalKey ?? null,
    };
  });
  useEffect(() => writeSessionWorkspaceState(sessionId, workspace), [sessionId, workspace]);
  const updateWorkspace = useCallback((patch: SessionWorkspaceState) => {
    setWorkspace((previous) => ({ ...previous, ...patch }));
  }, []);
  const [maximized, setMaximized] = useState(false);
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
        setPosition(
          options?.line === undefined ? undefined : { line: options.line, column: options.column },
        );
        setWorkspace((previous) => ({
          ...previous,
          open: true,
          selectedFilePath: path,
          selectedTerminalKey: null,
          openFiles: previous.openFiles?.includes(path)
            ? previous.openFiles
            : [...(previous.openFiles ?? []), path],
        }));
      });
    },
    [navigateWorkspace],
  );
  const selectTab = useCallback(
    (rightRailTab: NonNullable<SessionWorkspaceState["rightRailTab"]>) => {
      navigateWorkspace("", undefined, () =>
        updateWorkspace({ rightRailTab, selectedFilePath: null, selectedTerminalKey: null }),
      );
    },
    [navigateWorkspace, updateWorkspace],
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
      navigateWorkspace("", undefined, () =>
        updateWorkspace({ open: true, selectedFilePath: null, selectedTerminalKey: key }),
      );
    },
    [navigateWorkspace, updateWorkspace],
  );
  const fileContext = useMemo(
    () => ({
      openFile,
      registerNavigationGuard,
      openGithubTab: () => {
        selectTab("github");
        updateWorkspace({ open: true });
      },
      conversationId: sessionId,
      workspaceRoot: environment.data?.root ?? null,
      workspaceHome: environment.data?.home ?? null,
      sessionHostId: session?.hostId,
      isChangedPath: (path: string) =>
        changedFiles.data?.data.some((file) => file.path === path) ?? false,
    }),
    [
      openFile,
      registerNavigationGuard,
      selectTab,
      updateWorkspace,
      sessionId,
      environment.data,
      session?.hostId,
      changedFiles.data,
    ],
  );
  const container = useRef<HTMLDivElement>(null);
  const [cellWidth, setCellWidth] = useState(0);
  useEffect(() => {
    const node = container.current;
    if (!node) return;
    const observer = new ResizeObserver(() => setCellWidth(node.clientWidth));
    observer.observe(node);
    setCellWidth(node.clientWidth);
    return () => observer.disconnect();
  }, []);
  const maximumWidth = Math.max(0, cellWidth - 140);
  const width = Math.min(workspace.widthPx ?? cellWidth * 0.45, maximumWidth);
  const resize = (next: number) =>
    updateWorkspace({
      widthPx: Math.max(Math.min(160, maximumWidth), Math.min(maximumWidth, next)),
    });
  const rightRailTab =
    !showFiles && ["files", "changes", "github"].includes(workspace.rightRailTab ?? "")
      ? "subagents"
      : (workspace.rightRailTab ?? "files");
  return (
    <FileViewerContext.Provider value={fileContext}>
      <section
        ref={setPaneRef}
        style={{ gridArea: area }}
        data-grid-session={sessionId}
        className={cn(
          "relative flex min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border bg-background",
          isDragging && "opacity-50",
        )}
      >
        <header className="flex h-8 shrink-0 items-center border-b px-1">
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
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`${workspace.open ? "Collapse" : "Expand"} workspace for ${title}`}
            aria-expanded={workspace.open}
            onClick={() => {
              setMaximized(false);
              updateWorkspace({ open: !workspace.open });
            }}
          >
            {workspace.open ? (
              <PanelRightCloseIcon className="size-4" />
            ) : (
              <PanelRightOpenIcon className="size-4" />
            )}
          </Button>
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
        <div ref={container} className="relative flex min-h-0 min-w-0 flex-1 overflow-hidden">
          <div className="min-h-0 min-w-0 flex-1 overflow-hidden" inert={maximized}>
            <SideChatPane childId={sessionId} fullSession readOnly={!canEdit} />
          </div>
          <WorkspacePanel
            contained
            conversationId={sessionId}
            sessionHarness={state.sessionHarness}
            agentId={state.boundAgentId ?? session?.agentId ?? null}
            open={workspace.open}
            inert={!workspace.open}
            width={width}
            handleProps={{
              tabIndex: 0,
              role: "separator",
              "aria-label": `Resize workspace for ${title}`,
              "aria-orientation": "vertical",
              "aria-valuenow": Math.round(width),
              "aria-valuemin": 0,
              "aria-valuemax": maximumWidth,
              onPointerDown: (event) => {
                event.preventDefault();
                event.currentTarget.setPointerCapture(event.pointerId);
              },
              onPointerMove: (event) => {
                if (event.currentTarget.hasPointerCapture(event.pointerId) && container.current)
                  resize(container.current.getBoundingClientRect().right - event.clientX);
              },
              onPointerUp: (event) => event.currentTarget.releasePointerCapture(event.pointerId),
              onKeyDown: (event) => {
                if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
                event.preventDefault();
                resize(width + (event.key === "ArrowLeft" ? 24 : -24));
              },
            }}
            rightRailTab={rightRailTab}
            onRightRailTabChange={selectTab}
            showFilesPanel={showFiles}
            showGithubTab={showFiles}
            showBrowserTab={supportsBrowser()}
            onBrowserTabOpened={() => updateWorkspace({ open: true })}
            changedCount={changedFiles.data?.data.length ?? 0}
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
            openTerminals={terminals.map(terminalTabKey)}
            selectedTerminalKey={workspace.selectedTerminalKey ?? null}
            closingTerminalKey={
              deleteTerminal.isPending && closingTerminal ? terminalTabKey(closingTerminal) : null
            }
            onCloseTerminal={setPendingClose}
            maximized={maximized}
            onToggleMaximized={() => setMaximized((previous) => !previous)}
            permissionLevel={permissionLevel}
            filesPanelSort={sort}
            onSortChange={setSort}
            filesPanelShowHidden={showHidden}
            onShowHiddenChange={setShowHidden}
            liveness={liveness}
          />
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
    </FileViewerContext.Provider>
  );
}
