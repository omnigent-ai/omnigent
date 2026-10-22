import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ComponentProps,
  type DragEvent,
  type MouseEventHandler,
} from "react";
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  MouseSensor,
  TouchSensor,
  closestCenter,
  pointerWithin,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import { sortableKeyboardCoordinates } from "@dnd-kit/sortable";
import {
  Grid2X2Icon,
  MoreHorizontalIcon,
  PanelRightCloseIcon,
  PanelRightIcon,
  PencilIcon,
  Trash2Icon,
} from "lucide-react";
import { CanvasWorkspace } from "@/canvas/CanvasWorkspace";
import { useLoadedConversations } from "@/hooks/useSidebarData";
import { Link, useParams } from "@/lib/routing";
import { SidebarOpenButton } from "@/shell/SidebarOpenButton";
import { Button } from "@/components/ui/button";
import { useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { cn } from "@/lib/utils";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { readDefaultWorkspacePanelOpen } from "@/lib/workspacePanelPreferences";
import { SidebarEditRow } from "@/shell/SidebarEditRow";
import {
  SIDEBAR_ROW,
  SIDEBAR_ACTIVE_HIGHLIGHT,
  SIDEBAR_HOVER_HIGHLIGHT,
} from "@/shell/sidebarStyles";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "@/components/ui/dropdown-menu";
import {
  ContextMenu,
  ContextMenuTrigger,
  ContextMenuContent,
  ContextMenuItem,
} from "@/components/ui/context-menu";
import { gridPanes, gridSessionIds, type SessionGrid } from "./gridLayout";
import { GridSessionPane, type GridWorkspacePanelProps } from "./GridSessionPane";
import { GridDropOverlay, type GridDropTarget } from "./GridDropTargets";
import { gridTitle, useGridSessions } from "./GridSessionsContext";

export function GridSidebar({ onNavigate }: { onNavigate?: MouseEventHandler<HTMLAnchorElement> }) {
  const grids = useGridSessions();
  if (!grids?.enabled || grids.grids.length === 0) return null;
  return <SavedGridSidebar onNavigate={onNavigate} />;
}

function SavedGridSidebar({ onNavigate }: { onNavigate?: MouseEventHandler<HTMLAnchorElement> }) {
  const grids = useGridSessions()!;
  const { data } = useLoadedConversations();
  const titles = new Map(
    data?.pages.flatMap((page) => page.data.map((session) => [session.id, session.title] as const)),
  );
  return (
    <section aria-label="Grids" className="mb-6">
      <div className="px-2 pb-2 text-xs font-medium text-muted-foreground">Grids</div>
      <ul>
        {grids.grids.map((grid) => (
          <GridSidebarRow
            key={grid.id}
            grid={grid}
            title={
              grid.name ||
              gridSessionIds(grid.layout)
                .map((id) => titles.get(id) || "Session")
                .join(" + ")
            }
            onNavigate={onNavigate}
          />
        ))}
      </ul>
    </section>
  );
}

function GridSidebarRow({
  grid,
  title,
  onNavigate,
}: {
  grid: SessionGrid;
  title: string;
  onNavigate?: MouseEventHandler<HTMLAnchorElement>;
}) {
  const grids = useGridSessions()!;
  const [editing, setEditing] = useState(false);
  const active = grids.activeGrid?.id === grid.id;
  if (editing)
    return (
      <li>
        <SidebarEditRow
          initialTitle={grid.name ?? ""}
          label="Rename grid"
          onCancel={() => setEditing(false)}
          onCommit={(name) => {
            grids.renameGrid(grid.id, name);
            setEditing(false);
          }}
        />
      </li>
    );
  return (
    <li className="group relative">
      <ContextMenu>
        <ContextMenuTrigger asChild>
          <Link
            to={`/grid/${grid.id}`}
            onClick={onNavigate}
            onDoubleClick={(event) => {
              event.preventDefault();
              setEditing(true);
            }}
            aria-current={active ? "page" : undefined}
            title={title}
            className={cn(
              SIDEBAR_ROW,
              "relative flex w-full items-center text-left text-foreground transition-colors pr-8",
              SIDEBAR_HOVER_HIGHLIGHT,
              active && SIDEBAR_ACTIVE_HIGHLIGHT,
            )}
          >
            <Grid2X2Icon className="size-3.5 shrink-0" />
            <span className="min-w-0 truncate">{title}</span>
          </Link>
        </ContextMenuTrigger>
        <ContextMenuContent>
          <ContextMenuItem onSelect={() => setEditing(true)}>
            <PencilIcon className="size-4" />
            Rename
          </ContextMenuItem>
          <ContextMenuItem onSelect={() => grids.deleteGrid(grid.id)}>
            <Trash2Icon className="size-4" />
            Delete grid
          </ContextMenuItem>
        </ContextMenuContent>
      </ContextMenu>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`Actions for ${title}`}
            className="absolute right-1 top-1/2 -translate-y-1/2 text-muted-foreground md:opacity-0 md:group-hover:opacity-100 md:group-has-[:focus-visible]:opacity-100 data-[state=open]:opacity-100"
          >
            <MoreHorizontalIcon className="size-3.5" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          <DropdownMenuItem onSelect={() => setEditing(true)}>
            <PencilIcon className="size-4" />
            Rename
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => grids.deleteGrid(grid.id)}>
            <Trash2Icon className="size-4" />
            Delete grid
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </li>
  );
}

export function RearrangeableGrid({
  grid,
  onOpenSidebar,
}: {
  grid: SessionGrid;
  onOpenSidebar?: () => void;
}) {
  const { layout } = grid;
  const sessionIds = gridSessionIds(layout);
  const [selectedSessionId, setSelectedSessionId] = useState(sessionIds[0]);
  const activeSessionId = sessionIds.includes(selectedSessionId)
    ? selectedSessionId
    : sessionIds[0];
  const workspaceKey = `grid:${grid.id}`;
  const [workspace, setWorkspace] = useState(() => {
    const saved = readSessionWorkspaceState(workspaceKey);
    return { open: saved.open ?? readDefaultWorkspacePanelOpen(), widthPx: saved.widthPx };
  });
  useEffect(() => writeSessionWorkspaceState(workspaceKey, workspace), [workspaceKey, workspace]);
  const [maximized, setMaximized] = useState(false);
  const [workspaceTarget, setWorkspaceTarget] = useState<HTMLDivElement | null>(null);
  const openWorkspace = useCallback((sessionId: string) => {
    setSelectedSessionId(sessionId);
    setWorkspace((previous) => ({ ...previous, open: true }));
  }, []);
  const container = useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = useState(0);
  useEffect(() => {
    const node = container.current;
    if (!node) return;
    const observer = new ResizeObserver(() => setContainerWidth(node.clientWidth));
    observer.observe(node);
    setContainerWidth(node.clientWidth);
    return () => observer.disconnect();
  }, []);
  const maximumWidth = Math.max(0, containerWidth - 280);
  const width = Math.min(workspace.widthPx ?? Math.min(600, containerWidth * 0.4), maximumWidth);
  const resize = (next: number) =>
    setWorkspace((previous) => ({
      ...previous,
      widthPx: Math.max(Math.min(240, maximumWidth), Math.min(maximumWidth, next)),
    }));
  const workspacePanel: GridWorkspacePanelProps = {
    open: workspace.open,
    width,
    maximized,
    onToggleMaximized: () => setMaximized((previous) => !previous),
    handleProps: {
      tabIndex: 0,
      role: "separator",
      "aria-label": "Resize grid workspace",
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
    },
  };
  const grids = useGridSessions()!;
  const [dragged, setDragged] = useState<{ id: string; title: string } | null>(null);
  const rearrangeTargetId =
    dragged && sessionIds.length === 2 && sessionIds.includes(dragged.id)
      ? sessionIds.find((id) => id !== dragged.id)
      : undefined;
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  return (
    <DndContext
      sensors={sensors}
      autoScroll={false}
      collisionDetection={(args) => {
        if (!args.pointerCoordinates) return closestCenter(args);
        const edges = pointerWithin({
          ...args,
          droppableContainers: args.droppableContainers.filter(
            (target) => target.data.current?.type === "session-grid",
          ),
        });
        return edges.length ? edges : pointerWithin(args);
      }}
      accessibility={{
        screenReaderInstructions: {
          draggable:
            "Press Space to pick up this conversation, then use the arrow keys to choose another cell. Press Space again to swap positions, or Escape to cancel.",
        },
      }}
      onDragStart={({ active, activatorEvent }) => {
        // Keep Escape on the drag handle instead of a cell's autofocused terminal.
        if (activatorEvent.target instanceof HTMLElement)
          activatorEvent.target.closest("button")?.focus();
        setDragged({ id: String(active.id), title: active.data.current?.title ?? "Session" });
      }}
      onDragCancel={() => setDragged(null)}
      onDragEnd={({ active, over }) => {
        setDragged(null);
        if (!over) return;
        const target = over.data.current as GridDropTarget | undefined;
        if (target?.type === "session-grid")
          grids.swapPanes(String(active.id), target.targetId, target.edge);
        else grids.swapPanes(String(active.id), String(over.id));
      }}
    >
      <div
        ref={container}
        className="relative flex min-h-0 min-w-0 flex-1"
        data-testid="grid-workspace"
      >
        <div
          ref={(node) => {
            node?.toggleAttribute("inert", maximized);
          }}
          className="flex min-h-0 min-w-0 flex-1 flex-col pt-[var(--mac-titlebar-height,0px)]"
        >
          <header className="flex h-12 shrink-0 items-center gap-2 px-4 py-3">
            {onOpenSidebar && (
              <SidebarOpenButton
                size="icon-sm"
                onOpenSidebar={onOpenSidebar}
                componentId="grid.open_sidebar"
              />
            )}
            <Grid2X2Icon className="size-4 shrink-0" />
            <h1 className="truncate text-ui">{gridTitle(grid)}</h1>
            <span className="flex-1" />
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label={`${workspace.open ? "Collapse" : "Expand"} grid workspace`}
              // aria-expanded={workspace.open}
              onClick={() => {
                setMaximized(false);
                setWorkspace((previous) => ({ ...previous, open: !previous.open }));
              }}
              className="text-muted-foreground hover:text-foreground border-none"
            >
              {workspace.open ? <PanelRightCloseIcon /> : <PanelRightIcon />}
            </Button>
          </header>
          <div
            className="relative grid min-h-0 min-w-0 flex-1 grid-cols-2 grid-rows-2 gap-2 overflow-auto p-2 pt-0"
            data-testid="session-grid"
          >
            {gridPanes(layout).map(({ sessionId, area }) => (
              <GridSessionPane
                key={sessionId}
                sessionId={sessionId}
                area={area}
                selected={sessionId === activeSessionId}
                onActivate={() => setSelectedSessionId(sessionId)}
                workspaceTarget={workspaceTarget}
                workspacePanel={workspacePanel}
                onOpenWorkspace={() => openWorkspace(sessionId)}
              />
            ))}
            {rearrangeTargetId && typeof layout !== "string" && (
              <GridDropOverlay
                targetId={rearrangeTargetId}
                edges={layout.axis === "columns" ? ["top", "bottom"] : ["left", "right"]}
                edgeOnly
              />
            )}
          </div>
        </div>
        <div ref={setWorkspaceTarget} className="contents" data-testid="grid-workspace-panel" />
      </div>
      <DragOverlay dropAnimation={null}>
        {dragged && (
          <div className="w-fit max-w-72 truncate rounded-md border bg-background px-2 py-1 text-ui shadow-md">
            {dragged.title}
          </div>
        )}
      </DragOverlay>
    </DndContext>
  );
}

// Links use dnd-kit, but native URL drops must never navigate away from a grid.
function preventLinkDrop(event: DragEvent<HTMLElement>) {
  if (event.dataTransfer.types.includes("text/uri-list")) event.preventDefault();
}

export function GridWorkspace({
  children,
  onOpenSidebar,
  ...canvasProps
}: ComponentProps<typeof CanvasWorkspace>) {
  const content = (
    <CanvasWorkspace {...canvasProps} onOpenSidebar={onOpenSidebar}>
      {children}
    </CanvasWorkspace>
  );
  const grids = useGridSessions();
  const { gridId } = useParams<{ gridId: string }>();
  const layout = grids?.activeGrid?.layout;
  // All visible panes must survive LRU eviction when the fourth stream opens.
  useEffect(() => {
    if (!grids?.enabled || !gridId || !layout) return;
    void useChatStore.getState().switchTo(null);
    const release = gridPanes(layout).map(({ sessionId }) =>
      conversationRegistry.retain(sessionId),
    );
    return () => release.forEach((unretain) => unretain());
  }, [grids?.enabled, gridId, layout]);
  if (!grids) return content;
  if (!gridId)
    return (
      <div
        className="relative flex min-h-0 min-w-0 flex-1"
        onDragOver={preventLinkDrop}
        onDropCapture={preventLinkDrop}
        data-grid-session={grids.enabled ? grids.conversationId : undefined}
      >
        {content}
      </div>
    );
  const loading = grids.desktop && !grids.enabled && !grids.identityReady;
  const canReload = grids.desktop && !grids.enabled && grids.identityReady;
  let message = "Grids are available on desktop only. Open a session from the sidebar.";
  if (loading) message = "Waiting for your account identity.";
  else if (canReload) message = "Your account identity is unavailable. Reload to retry.";
  else if (grids.desktop)
    message =
      "This grid isn’t saved on this device. Open a session and drag another session into it to create a grid.";
  return (
    <section
      className="flex min-h-0 min-w-0 flex-1 flex-col"
      aria-label="Session grid"
      onDragOver={preventLinkDrop}
      onDropCapture={preventLinkDrop}
    >
      {grids.activeGrid ? (
        <RearrangeableGrid key={gridId} grid={grids.activeGrid} onOpenSidebar={onOpenSidebar} />
      ) : (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
          {onOpenSidebar && (
            <SidebarOpenButton
              size="icon-sm"
              onOpenSidebar={onOpenSidebar}
              componentId="grid.open_sidebar"
            />
          )}
          <h1>{loading ? "Loading grid…" : "Grid unavailable"}</h1>
          <p>{message}</p>
          {canReload && <Button onClick={() => window.location.reload()}>Reload</Button>}
          <Button asChild variant="outline">
            <Link to="/">Back to sessions</Link>
          </Button>
        </div>
      )}
    </section>
  );
}
