/**
 * Canvas page (``/canvas``) — every top-level session as a draggable card on
 * a freeform React Flow surface, one canvas per project plus a **Main** canvas
 * for sessions outside any project.
 *
 * Built entirely from existing primitives, with no server surface of its own:
 *
 * - Sessions load through `useCanvasSessions`: the sidebar's cached rows paint
 *   at once, then the canonical list arrives in 1,000-row pages behind the
 *   header's "Loading sessions" spinner, and refreshes every 30 seconds and on
 *   window focus.
 * - Projects come from `useProjects`; the **+** tab reuses the sidebar's
 *   `NewProjectButton`, so creating a project here is the same
 *   `POST /v1/projects` the sidebar performs.
 * - Pull requests come through the GitHub panel's query cache
 *   (`fetchGithubInfo`), throttled per session by `usePullRequests`.
 * - Card positions live in localStorage (`canvasStorage.ts`), keyed by server
 *   identity; the server never learns the layout. The view itself is not
 *   saved: every canvas opens fitted to its cards and stays fitted until the
 *   user pans or zooms by hand.
 * - The selected canvas lives in `?canvas=<id>` so a reload keeps the tab.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  applyNodeChanges,
  Background,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type NodeChange,
  type Viewport,
} from "@xyflow/react";
import {
  Maximize2Icon,
  PlusIcon,
  RotateCcwIcon,
  TriangleAlertIcon,
  ZoomInIcon,
  ZoomOutIcon,
} from "lucide-react";
import {
  CARD_HEIGHT,
  CARD_WIDTH,
  MAIN_CANVAS_ID,
  mergeCanvasPositions,
  mergeSessionPositions,
  projectCanvasId,
  prunePositions,
  sessionsOnCanvas,
  type CanvasPositions,
} from "@/canvas/canvasLayout";
import { useCanvasSessions } from "@/canvas/canvasSessions";
import {
  EMPTY_CANVAS_LAYOUT,
  readCanvasLayout,
  withoutPositions,
  withPosition,
  withPositions,
  writeCanvasLayout,
  type CanvasLayout,
} from "@/canvas/canvasStorage";
import { usePullRequests, type CanvasPullRequests } from "@/canvas/pullRequests";
import { SessionCard, type SessionCardNode } from "@/canvas/SessionCard";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useProjects, type Conversation, type ProjectSummary } from "@/hooks/useConversations";
import { useViewerId } from "@/hooks/useViewerId";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { useNavigate, useSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { NewProjectButton } from "@/shell/NewProjectButton";
import { ProjectRowIcon } from "@/shell/ProjectPicker";

import "@xyflow/react/dist/style.css";

const nodeTypes = { session: SessionCard };
const proOptions = { hideAttribution: true };
const FIT_VIEW = { padding: 0.2, maxZoom: 1 };
const MIN_ZOOM = 0.05;
const MAX_ZOOM = 2.5;
const RESIZE_REFIT_DELAY_MS = 100;
const EMPTY_PROJECTS: ProjectSummary[] = [];
/** Query parameter carrying the selected canvas so a reload lands on the same tab. */
export const CANVAS_QUERY_PARAM = "canvas";

const TAB_CLASS =
  "flex h-7 max-w-[200px] shrink-0 items-center gap-1.5 rounded-md px-2.5 text-ui text-muted-foreground transition-colors hover:bg-muted hover:text-foreground aria-selected:bg-brand-accent/10 aria-selected:text-foreground";
const CONTROL_CLASS =
  "flex size-7 items-center justify-center rounded-md border bg-card text-muted-foreground shadow-sm transition-colors hover:bg-accent hover:text-accent-foreground";

function sessionCountLabel(count: number): string {
  return count === 1 ? "1 session" : `${count} sessions`;
}

function CanvasControls({
  onZoom,
  onFit,
  onReset,
}: {
  onZoom: () => void;
  onFit: () => void;
  onReset: () => void;
}) {
  const { zoomIn, zoomOut } = useReactFlow();
  return (
    <div className="absolute bottom-3 left-3 z-10 flex flex-col gap-1">
      <button
        type="button"
        className={CONTROL_CLASS}
        onClick={() => {
          onZoom();
          void zoomIn({ duration: 200 });
        }}
        aria-label="Zoom in"
      >
        <ZoomInIcon className="size-4" />
      </button>
      <button
        type="button"
        className={CONTROL_CLASS}
        onClick={() => {
          onZoom();
          void zoomOut({ duration: 200 });
        }}
        aria-label="Zoom out"
      >
        <ZoomOutIcon className="size-4" />
      </button>
      <button type="button" className={CONTROL_CLASS} onClick={onFit} aria-label="Fit view">
        <Maximize2Icon className="size-4" />
      </button>
      <button
        type="button"
        className={CONTROL_CLASS}
        onClick={onReset}
        aria-label="Reset layout"
        title="Reset layout"
      >
        <RotateCcwIcon className="size-4" />
      </button>
    </div>
  );
}

function CanvasSurface() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { trackClick } = useOmnigentAnalytics();
  const { fitView } = useReactFlow();
  const viewerId = useViewerId();
  const { sessions, loaded, loadingMore, complete, error, refresh } = useCanvasSessions();
  const projectsQuery = useProjects();
  const projects = projectsQuery.data ?? EMPTY_PROJECTS;

  const [nodes, setNodes] = useState<SessionCardNode[]>([]);
  const [activeCanvas, setActiveCanvas] = useState(
    () => searchParams.get(CANVAS_QUERY_PARAM) ?? MAIN_CANVAS_ID,
  );
  const [storageWarning, setStorageWarning] = useState<string | null>(null);
  const activeCanvasRef = useRef(activeCanvas);
  // Loaded per viewer (the store is keyed by server and user) in the positions
  // effect below, so identity resolving after mount swaps in the right layout.
  const layoutRef = useRef<CanvasLayout>(EMPTY_CANVAS_LAYOUT);
  const layoutViewerRef = useRef<string | null | undefined>(undefined);
  // Live positions for every session, including unsaved grid slots.
  const positionsRef = useRef<CanvasPositions>({});
  // True once the user pans or zooms by hand; auto-fits then leave the view
  // alone until the canvas changes.
  const viewportDirtyRef = useRef(false);
  // The card set the view was last fitted to; a different set refits.
  const fittedKeyRef = useRef<string | null>(null);
  const pendingProjectNameRef = useRef<string | null>(null);
  const flowContainerRef = useRef<HTMLDivElement>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const visibleSessions = useMemo(
    () => sessionsOnCanvas(sessions, activeCanvas, projects, viewerId),
    [sessions, activeCanvas, projects, viewerId],
  );
  const pullRequests = usePullRequests(visibleSessions);
  const activeProject = useMemo(
    () => projects.find((project) => projectCanvasId(project) === activeCanvas) ?? null,
    [projects, activeCanvas],
  );

  const persist = useCallback(
    (layout: CanvasLayout) => {
      layoutRef.current = layout;
      try {
        writeCanvasLayout(layout, viewerId);
        if (aliveRef.current) setStorageWarning(null);
      } catch {
        if (aliveRef.current) setStorageWarning("Canvas layout could not be saved.");
      }
    },
    [viewerId],
  );

  const fitCanvas = useCallback(
    (duration = 0) => {
      viewportDirtyRef.current = false;
      void fitView({ ...FIT_VIEW, duration });
    },
    [fitView],
  );

  const markViewportDirty = useCallback(() => {
    viewportDirtyRef.current = true;
  }, []);

  /** The next card set gets a fresh fit, even if the user had panned this one. */
  const scheduleFit = useCallback(() => {
    viewportDirtyRef.current = false;
    fittedKeyRef.current = null;
  }, []);

  const openSession = useCallback(
    (sessionId: string) => {
      trackClick("canvas.open-session");
      navigate(`/c/${encodeURIComponent(sessionId)}`);
    },
    [navigate, trackClick],
  );

  const nodesFor = useCallback(
    (
      items: readonly Conversation[],
      positions: CanvasPositions,
      requests: CanvasPullRequests,
    ): SessionCardNode[] =>
      items.map((session) => ({
        id: session.id,
        type: "session",
        position: positions[session.id],
        // Fixed card size so fit-to-view can measure cards that are not rendered
        // yet (onlyRenderVisibleElements draws only the ones in view).
        initialWidth: CARD_WIDTH,
        initialHeight: CARD_HEIGHT,
        data: {
          conversation: session,
          pullRequest: requests[session.id] ?? null,
          onOpen: openSession,
        },
        selectable: true,
        focusable: false,
      })),
    [openSession],
  );

  // Unplaced cards get grid slots whenever the session or project set changes.
  // When the viewer changes (identity resolving after mount), reload that
  // viewer's saved layout and lay everything out again from it. Declared before
  // the node rebuild below so it runs first in the same commit.
  useEffect(() => {
    if (layoutViewerRef.current !== viewerId) {
      layoutViewerRef.current = viewerId;
      layoutRef.current = readCanvasLayout(viewerId);
      positionsRef.current = {};
    }
    positionsRef.current = mergeCanvasPositions(
      sessions,
      projects,
      { ...layoutRef.current.positions, ...positionsRef.current },
      viewerId,
    );
  }, [sessions, projects, viewerId]);

  // Cards follow the active canvas; drags update the node state directly and
  // land in positionsRef on drop, so rebuilding here never loses a move.
  useEffect(() => {
    setNodes(nodesFor(visibleSessions, positionsRef.current, pullRequests));
  }, [nodesFor, visibleSessions, pullRequests]);

  // Keep every card in view until the user pans or zooms by hand: first paint,
  // tab switches, cards arriving while loading, and Reset layout. Keyed on the
  // card set so status-only updates never move the view. Runs after the flow
  // has adopted the new nodes (its store update is a child effect).
  useEffect(() => {
    // The first render carries an empty node list; wait for real cards.
    if (!loaded || nodes.length === 0) return;
    const key = nodes.map((node) => node.id).join("\n");
    if (key === fittedKeyRef.current) return;
    fittedKeyRef.current = key;
    if (!viewportDirtyRef.current) fitCanvas();
  }, [fitCanvas, loaded, nodes]);

  // Mirror the selected canvas into the URL; Main keeps the URL clean.
  const writeCanvasParam = useCallback(
    (canvasId: string) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          if (canvasId === MAIN_CANVAS_ID) next.delete(CANVAS_QUERY_PARAM);
          else next.set(CANVAS_QUERY_PARAM, canvasId);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const selectCanvas = useCallback(
    (canvasId: string) => {
      if (activeCanvasRef.current === canvasId) return;
      trackClick("canvas.tab");
      activeCanvasRef.current = canvasId;
      setActiveCanvas(canvasId);
      writeCanvasParam(canvasId);
      scheduleFit();
    },
    [scheduleFit, trackClick, writeCanvasParam],
  );

  // A project canvas whose project was deleted (or a stale URL) falls back to Main.
  useEffect(() => {
    if (activeCanvas === MAIN_CANVAS_ID || projectsQuery.data === undefined) return;
    if (projects.some((project) => projectCanvasId(project) === activeCanvas)) return;
    activeCanvasRef.current = MAIN_CANVAS_ID;
    setActiveCanvas(MAIN_CANVAS_ID);
    writeCanvasParam(MAIN_CANVAS_ID);
    scheduleFit();
  }, [activeCanvas, projects, projectsQuery.data, scheduleFit, writeCanvasParam]);

  // Once the full list is known, forget spots of sessions that no longer exist.
  useEffect(() => {
    if (!complete) return;
    const layout = layoutRef.current;
    const pruned = prunePositions(
      layout.positions,
      sessions.map((session) => session.id),
    );
    if (Object.keys(pruned).length !== Object.keys(layout.positions).length) {
      persist(withPositions(layout, pruned));
    }
  }, [complete, persist, sessions]);

  // A project created from the tab strip is selected once the list includes it.
  useEffect(() => {
    const name = pendingProjectNameRef.current;
    if (name === null) return;
    const project = projects.find((candidate) => candidate.name === name);
    if (!project) return;
    pendingProjectNameRef.current = null;
    selectCanvas(projectCanvasId(project));
  }, [projects, selectCanvas]);

  // Follow the window: while the view is an auto-fit, keep it fitted as the
  // container resizes. A hand-panned view is left alone.
  useEffect(() => {
    const container = flowContainerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;
    let first = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const observer = new ResizeObserver(() => {
      if (first) {
        first = false;
        return;
      }
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        if (loaded && !viewportDirtyRef.current) fitCanvas();
      }, RESIZE_REFIT_DELAY_MS);
    });
    observer.observe(container);
    return () => {
      if (timer) clearTimeout(timer);
      observer.disconnect();
    };
  }, [fitCanvas, loaded]);

  const onNodesChange = useCallback((changes: NodeChange<SessionCardNode>[]) => {
    setNodes((current) => applyNodeChanges(changes, current));
  }, []);

  const onNodeDragStop = useCallback(
    (_event: MouseEvent | TouchEvent, node: SessionCardNode) => {
      const position = { x: Math.round(node.position.x), y: Math.round(node.position.y) };
      positionsRef.current = { ...positionsRef.current, [node.id]: position };
      persist(withPosition(layoutRef.current, node.id, position));
    },
    [persist],
  );

  // A null event is a programmatic move (a fit), not the user's own view.
  const onMoveEnd = useCallback((event: MouseEvent | TouchEvent | null, _viewport: Viewport) => {
    if (event !== null) viewportDirtyRef.current = true;
  }, []);

  const resetLayout = useCallback(() => {
    trackClick("canvas.reset-layout");
    const ids = visibleSessions.map((session) => session.id);
    const removed = new Set(ids);
    const kept = Object.fromEntries(
      Object.entries(positionsRef.current).filter(([id]) => !removed.has(id)),
    ) as CanvasPositions;
    positionsRef.current = { ...kept, ...mergeSessionPositions(visibleSessions, {}) };
    scheduleFit();
    setNodes(nodesFor(visibleSessions, positionsRef.current, pullRequests));
    persist(withoutPositions(layoutRef.current, ids));
  }, [nodesFor, persist, pullRequests, scheduleFit, trackClick, visibleSessions]);

  const newSession = () => {
    trackClick("canvas.new-session");
    // The composer takes the project by name (`?project=`).
    navigate(
      activeProject
        ? { pathname: "/", search: `?project=${encodeURIComponent(activeProject.name)}` }
        : "/",
    );
  };

  if (!loaded) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center" data-testid="canvas-page">
        {error ? (
          <div role="alert" className="flex flex-col items-center gap-2 p-6 text-center">
            <TriangleAlertIcon aria-hidden className="size-5 text-muted-foreground" />
            <strong className="font-medium">Canvas could not load</strong>
            <span className="text-ui text-muted-foreground">{error}</span>
            <Button variant="outline" size="sm" className="mt-2" onClick={() => void refresh()}>
              Retry
            </Button>
          </div>
        ) : (
          <Spinner className="size-5 text-muted-foreground" aria-label="Loading Canvas" />
        )}
      </div>
    );
  }

  const emptyState = visibleSessions.length === 0 && (
    <div className="pointer-events-none absolute inset-0 z-[5] flex items-center justify-center p-6 text-center text-muted-foreground">
      <strong className="font-medium text-foreground">
        {activeProject
          ? `No sessions in ${activeProject.name}`
          : projects.length > 0
            ? "No sessions outside projects"
            : "No sessions"}
      </strong>
    </div>
  );

  return (
    <div
      className="flex min-h-0 flex-1 flex-col"
      data-testid="canvas-page"
      style={{
        paddingTop: "calc(var(--omnigent-header-height) + var(--omnigent-inset-top))",
        paddingBottom: "var(--omnigent-inset-bottom)",
      }}
    >
      <header className="flex items-start justify-between gap-4 px-6 pt-5">
        <div>
          <h1 className="text-2xl font-semibold">Canvas</h1>
          <div className="flex items-center gap-1.5 text-ui text-muted-foreground">
            <span>{sessionCountLabel(visibleSessions.length)}</span>
            {loadingMore && <Spinner className="size-3.5" aria-label="Loading sessions" />}
          </div>
        </div>
      </header>
      <nav
        aria-label="Canvases"
        className="flex items-center gap-2 overflow-x-auto px-6 py-3 [scrollbar-width:thin]"
      >
        <div role="tablist" className="flex gap-1">
          <button
            type="button"
            role="tab"
            className={TAB_CLASS}
            aria-selected={activeCanvas === MAIN_CANVAS_ID}
            onClick={() => selectCanvas(MAIN_CANVAS_ID)}
          >
            Main
          </button>
          {projects.map((project) => {
            const canvasId = projectCanvasId(project);
            return (
              <button
                key={canvasId}
                type="button"
                role="tab"
                className={TAB_CLASS}
                aria-selected={activeCanvas === canvasId}
                title={project.name}
                onClick={() => selectCanvas(canvasId)}
              >
                <ProjectRowIcon icon={project.icon} />
                <span className="truncate">{project.name}</span>
              </button>
            );
          })}
        </div>
        <NewProjectButton
          onCreated={(name) => {
            pendingProjectNameRef.current = name;
          }}
        />
      </nav>
      {error && (
        <div
          role="alert"
          className="mx-6 mb-3 rounded-md border px-3 py-2 text-ui text-muted-foreground"
        >
          Refresh failed: {error}
        </div>
      )}
      {storageWarning && (
        <div
          role="status"
          className="mx-6 mb-3 rounded-md border px-3 py-2 text-ui text-muted-foreground"
        >
          {storageWarning}
        </div>
      )}
      <div
        ref={flowContainerRef}
        className="canvas-flow relative min-h-0 min-w-0 flex-1 border-t"
        data-testid="canvas-flow"
      >
        <ReactFlow<SessionCardNode>
          nodes={nodes}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onNodeDragStop={onNodeDragStop}
          onNodeDoubleClick={(_event, node) => openSession(node.id)}
          onMoveEnd={onMoveEnd}
          nodesDraggable
          nodesConnectable={false}
          elementsSelectable
          nodesFocusable={false}
          zoomOnDoubleClick={false}
          panOnScroll
          nodeDragThreshold={3}
          onlyRenderVisibleElements
          minZoom={MIN_ZOOM}
          maxZoom={MAX_ZOOM}
          proOptions={proOptions}
        >
          {visibleSessions.length > 0 && (
            <Background color="var(--border)" bgColor="var(--background)" />
          )}
          <CanvasControls
            onZoom={markViewportDirty}
            onFit={() => fitCanvas(200)}
            onReset={resetLayout}
          />
        </ReactFlow>
        {emptyState}
        <Button
          type="button"
          size="icon-lg"
          aria-label="New session"
          title="New session"
          data-testid="canvas-new-session"
          className={cn(
            "absolute bottom-6 right-6 z-10 size-12 rounded-full bg-brand-accent text-white shadow-lg",
            "hover:bg-brand-accent/90",
          )}
          onClick={newSession}
        >
          <PlusIcon className="size-5" />
        </Button>
      </div>
    </div>
  );
}

export function CanvasPage() {
  return (
    <ReactFlowProvider>
      <CanvasSurface />
    </ReactFlowProvider>
  );
}
