import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import type {
  ExtensionContext,
  ExtensionProjectSummary,
  ExtensionPullRequest,
  ExtensionSessionSummary,
} from "@omnigent/extension-sdk";
import {
  applyNodeChanges,
  Background,
  ControlButton,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Node,
  type NodeChange,
} from "@xyflow/react";
import {
  MAIN_CANVAS_ID,
  mergeCanvasPositions,
  mergeSessionPositions,
  prunePositions,
  sessionsOnCanvas,
  type CanvasPositions,
} from "./canvasLayout";
import {
  positionBucket,
  readCanvasLayout,
  readCanvasViewport,
  resetCanvasLayout,
  upsertPosition,
  writeCanvasViewport,
  writePositionBucket,
  type CanvasViewport,
} from "./canvasStorage";
import {
  canCreateProjects,
  canReadProjects,
  loadProjects,
  loadSessions,
  type SessionLoadProgress,
} from "./sessionData";
import { SessionCardNode, type SessionCardData } from "./SessionCardNode";

const nodeTypes = { session: SessionCardNode };
const proOptions = { hideAttribution: true };
const FIT_VIEW = { padding: 0.2, maxZoom: 1 };
const MIN_ZOOM = 0.2;
const MAX_ZOOM = 2.5;
const LARGE_CANVAS_SESSION_COUNT = 40;
const READABLE_VIEWPORT = { x: 24, y: 24, zoom: 0.9 };
const RESIZE_REFIT_DELAY_MS = 100;
export const SESSION_POLL_INTERVAL_MS = 30_000;
export const PULL_REQUEST_REFRESH_MS = 300_000;
export const PULL_REQUEST_CONCURRENCY = 4;
const PROJECT_NAME_MAX_LENGTH = 100;
type SessionNode = Node<SessionCardData, "session">;

interface PullRequestTask {
  key: string;
  run: () => Promise<void>;
}

class PullRequestQueue {
  private readonly active = new Set<string>();
  private readonly pending = new Map<string, () => Promise<void>>();

  constructor(private readonly concurrency: number) {}

  replace(tasks: PullRequestTask[]): void {
    this.pending.clear();
    for (const task of tasks) {
      if (!this.active.has(task.key)) this.pending.set(task.key, task.run);
    }
    this.drain();
  }

  clear(): void {
    this.pending.clear();
  }

  private drain(): void {
    while (this.active.size < this.concurrency && this.pending.size > 0) {
      const entry = this.pending.entries().next().value as
        [string, () => Promise<void>] | undefined;
      if (!entry) return;
      const [key, run] = entry;
      this.pending.delete(key);
      this.active.add(key);
      void run()
        .catch(() => undefined)
        .finally(() => {
          this.active.delete(key);
          this.drain();
        });
    }
  }
}

function sessionCountLabel(count: number): string {
  return count === 1 ? "1 session" : `${count} sessions`;
}

function mergePartialSessions(
  existing: ExtensionSessionSummary[],
  loaded: ExtensionSessionSummary[],
): ExtensionSessionSummary[] {
  const loadedIds = new Set(loaded.map((session) => session.id));
  return [
    ...loaded,
    ...existing.filter((session) => !loadedIds.has(session.id)),
  ];
}

function CanvasSurface({
  context,
  onReady,
}: {
  context: ExtensionContext;
  onReady?: () => void;
}) {
  const { fitView, getViewport, setViewport } = useReactFlow();
  const [nodes, setNodes] = useState<SessionNode[]>([]);
  const [sessions, setSessions] = useState<ExtensionSessionSummary[]>([]);
  const [projects, setProjects] = useState<ExtensionProjectSummary[]>([]);
  const [pullRequests, setPullRequests] = useState<
    Record<string, ExtensionPullRequest | null>
  >({});
  const pullRequestCheckedAtRef = useRef<Record<string, number>>({});
  const pullRequestQueueRef = useRef<PullRequestQueue | null>(null);
  if (pullRequestQueueRef.current === null) {
    pullRequestQueueRef.current = new PullRequestQueue(
      PULL_REQUEST_CONCURRENCY,
    );
  }
  const [activeCanvas, setActiveCanvas] = useState(MAIN_CANVAS_ID);
  const [loading, setLoading] = useState(true);
  const [loadingSessions, setLoadingSessions] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [storageWarning, setStorageWarning] = useState<string | null>(null);
  const flowContainerRef = useRef<HTMLDivElement>(null);
  const tabsRef = useRef<HTMLElement>(null);
  const [tabsScrollable, setTabsScrollable] = useState(false);
  // True once the user pans or zooms by hand; auto-fits then stop overriding them.
  const viewportDirtyRef = useRef(false);
  // null while the new-project form is closed.
  const [newProjectName, setNewProjectName] = useState<string | null>(null);
  const [savingProject, setSavingProject] = useState(false);
  const [projectError, setProjectError] = useState<string | null>(null);
  const positionsRef = useRef<CanvasPositions>({});
  const persistedPositionsRef = useRef<CanvasPositions>({});
  const sessionsRef = useRef<ExtensionSessionSummary[]>([]);
  const activeCanvasRef = useRef(MAIN_CANVAS_ID);
  const openingRef = useRef(false);
  const initializedRef = useRef(false);
  const hasLoadedAllSessionsRef = useRef(false);
  const refreshInFlightRef = useRef<Promise<void> | null>(null);
  const viewportTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const aliveRef = useRef(true);
  const readyRef = useRef(false);

  const projectIds = useMemo(
    () => new Set(projects.map((project) => project.id)),
    [projects],
  );
  const visibleSessions = useMemo(
    () => sessionsOnCanvas(sessions, activeCanvas, projectIds),
    [activeCanvas, projectIds, sessions],
  );
  const activeProject =
    projects.find((project) => project.id === activeCanvas) ?? null;

  useEffect(() => {
    return () => {
      aliveRef.current = false;
      pullRequestQueueRef.current?.clear();
    };
  }, []);

  useEffect(() => {
    if (loading || readyRef.current) return;
    readyRef.current = true;
    onReady?.();
  }, [loading, onReady]);

  const applyDefaultViewport = useCallback(
    (sessionCount: number, duration = 0) => {
      viewportDirtyRef.current = false;
      if (sessionCount > LARGE_CANVAS_SESSION_COUNT) {
        void setViewport(READABLE_VIEWPORT, { duration });
      } else {
        void fitView({ ...FIT_VIEW, duration });
      }
    },
    [fitView, setViewport],
  );

  const containerSize = useCallback(() => {
    const rect = flowContainerRef.current?.getBoundingClientRect();
    return {
      width: Math.round(rect?.width ?? 0),
      height: Math.round(rect?.height ?? 0),
    };
  }, []);

  // Restore the same canvas point at the center even if the container resized.
  const applyViewport = useCallback(
    (saved: CanvasViewport | null, sessionCount: number) => {
      requestAnimationFrame(() => {
        if (!aliveRef.current) return;
        const usable =
          saved !== null && saved.zoom >= MIN_ZOOM && saved.zoom <= MAX_ZOOM;
        if (usable) {
          viewportDirtyRef.current = true;
          const size = containerSize();
          void setViewport(
            {
              x:
                saved.x +
                (size.width > 0 && saved.width
                  ? (size.width - saved.width) / 2
                  : 0),
              y:
                saved.y +
                (size.height > 0 && saved.height
                  ? (size.height - saved.height) / 2
                  : 0),
              zoom: saved.zoom,
            },
            { duration: 0 },
          );
        } else {
          applyDefaultViewport(sessionCount);
        }
      });
    },
    [applyDefaultViewport, containerSize, setViewport],
  );

  const openSession = useCallback(
    (sessionId: string) => {
      if (openingRef.current) return;
      openingRef.current = true;
      void context.navigation
        .openSession(sessionId)
        .catch((reason: unknown) => {
          if (aliveRef.current) {
            setError(
              reason instanceof Error
                ? reason.message
                : "Could not open session",
            );
          }
        })
        .finally(() => {
          openingRef.current = false;
        });
    },
    [context],
  );

  const openExternal = useCallback(
    (url: string) => {
      void context.navigation.openExternal(url).catch(() => undefined);
    },
    [context],
  );

  const nodesFor = useCallback(
    (
      items: ExtensionSessionSummary[],
      positions: CanvasPositions,
    ): SessionNode[] =>
      items.map((session) => ({
        id: session.id,
        type: "session",
        position: positions[session.id],
        data: {
          session,
          pullRequest: pullRequests[session.id] ?? null,
          onOpen: openSession,
          onOpenExternal: openExternal,
        },
        selectable: true,
        focusable: false,
      })),
    [openExternal, openSession, pullRequests],
  );

  // Only enrich branch-bearing cards on the active canvas. A bounded queue
  // prevents large workspaces from exhausting the host's request budget.
  useEffect(() => {
    const queue = pullRequestQueueRef.current;
    if (!context.capabilities.includes("sessions.pullRequest")) {
      queue?.clear();
      return;
    }
    const now = Date.now();
    const due = visibleSessions.filter(
      (session) =>
        Boolean(session.gitBranch?.trim()) &&
        now - (pullRequestCheckedAtRef.current[session.id] ?? 0) >=
          PULL_REQUEST_REFRESH_MS,
    );
    queue?.replace(
      due.map((session) => ({
        key: session.id,
        run: async () => {
          pullRequestCheckedAtRef.current[session.id] = Date.now();
          const pullRequest = await context.sessions.pullRequest(session.id);
          if (!aliveRef.current) return;
          setPullRequests((current) =>
            current[session.id] === pullRequest
              ? current
              : { ...current, [session.id]: pullRequest },
          );
        },
      })),
    );
    return () => queue?.clear();
  }, [context, visibleSessions]);

  // Cards follow the active canvas; drags update the node state directly and
  // land in positionsRef on drop, so rebuilding here never loses a move.
  useEffect(() => {
    setNodes(nodesFor(visibleSessions, positionsRef.current));
  }, [nodesFor, visibleSessions]);

  const applyData = useCallback(
    async (
      items: ExtensionSessionSummary[],
      projectList: ExtensionProjectSummary[],
      complete: boolean,
    ) => {
      const previousPersisted = persistedPositionsRef.current;
      const persisted = complete
        ? prunePositions(
            previousPersisted,
            items.map((session) => session.id),
          )
        : previousPersisted;
      const ids = new Set(projectList.map((project) => project.id));
      persistedPositionsRef.current = persisted;
      positionsRef.current = mergeCanvasPositions(items, ids, {
        ...persisted,
        ...positionsRef.current,
      });
      if (
        activeCanvasRef.current !== MAIN_CANVAS_ID &&
        !ids.has(activeCanvasRef.current)
      ) {
        activeCanvasRef.current = MAIN_CANVAS_ID;
        setActiveCanvas(MAIN_CANVAS_ID);
      }
      setProjects(projectList);
      sessionsRef.current = items;
      setSessions(items);
      if (complete) {
        const dirtyBuckets = new Set(
          Object.keys(previousPersisted)
            .filter((id) => !(id in persisted))
            .map(positionBucket),
        );
        try {
          await Promise.all(
            [...dirtyBuckets].map((bucket) =>
              writePositionBucket(context.storage.user, persisted, bucket),
            ),
          );
          if (aliveRef.current && dirtyBuckets.size > 0) {
            setStorageWarning(null);
          }
        } catch {
          if (aliveRef.current) {
            setStorageWarning("Canvas layout could not be saved.");
          }
        }
      }
    },
    [context.storage.user],
  );

  const loadData = useCallback(
    async (
      onProgress: (
        progress: SessionLoadProgress,
        projectList: ExtensionProjectSummary[],
      ) => void | Promise<void>,
    ) => {
      setLoadingSessions(!hasLoadedAllSessionsRef.current);
      try {
        const projectListPromise = loadProjects(context);
        await loadSessions(context, async (progress) => {
          await onProgress(progress, await projectListPromise);
        });
        // Once the full list is known, routine refreshes stay quiet.
        hasLoadedAllSessionsRef.current = true;
      } finally {
        if (aliveRef.current) setLoadingSessions(false);
      }
    },
    [context],
  );

  const refresh = useCallback(
    (initial = false): Promise<void> => {
      if (refreshInFlightRef.current) return refreshInFlightRef.current;
      if (initial) setLoading(true);
      const existing = sessionsRef.current;
      let firstPage = true;
      const request = (async () => {
        try {
          await loadData(async (progress, projectList) => {
            if (!aliveRef.current) return;
            const items = progress.hasMore
              ? mergePartialSessions(existing, progress.sessions)
              : progress.sessions;
            const applied = applyData(items, projectList, !progress.hasMore);
            if (firstPage) {
              firstPage = false;
              setError(null);
              setLoading(false);
              if (initial) {
                const ids = new Set(projectList.map((project) => project.id));
                applyDefaultViewport(
                  progress.hasMore
                    ? LARGE_CANVAS_SESSION_COUNT + 1
                    : sessionsOnCanvas(items, activeCanvasRef.current, ids)
                        .length,
                );
              }
            }
            await applied;
          });
        } catch (reason) {
          if (aliveRef.current) {
            setError(
              reason instanceof Error
                ? reason.message
                : "Could not load sessions",
            );
          }
        } finally {
          if (aliveRef.current) setLoading(false);
        }
      })();
      refreshInFlightRef.current = request;
      void request.finally(() => {
        if (refreshInFlightRef.current === request) {
          refreshInFlightRef.current = null;
        }
      });
      return request;
    },
    [applyData, applyDefaultViewport, loadData],
  );

  useEffect(() => {
    if (initializedRef.current) return;
    let cancelled = false;
    let firstPage = true;
    const layoutPromise = readCanvasLayout(context.storage.user).catch(() => ({
      positions: {},
      viewport: null,
    }));
    const request = (async () => {
      try {
        await loadData(async (progress, projectList) => {
          const layout = firstPage ? await layoutPromise : null;
          if (cancelled || !aliveRef.current) return;
          if (layout) {
            persistedPositionsRef.current = layout.positions;
            positionsRef.current = layout.positions;
          }
          const applied = applyData(
            progress.sessions,
            projectList,
            !progress.hasMore,
          );
          if (firstPage && layout) {
            firstPage = false;
            initializedRef.current = true;
            setError(null);
            setLoading(false);
            const ids = new Set(projectList.map((project) => project.id));
            applyViewport(
              layout.viewport,
              progress.hasMore
                ? LARGE_CANVAS_SESSION_COUNT + 1
                : sessionsOnCanvas(progress.sessions, MAIN_CANVAS_ID, ids)
                    .length,
            );
          }
          await applied;
        });
      } catch (reason) {
        if (cancelled) return;
        setError(
          reason instanceof Error ? reason.message : "Could not load sessions",
        );
        setLoading(false);
      }
    })();
    refreshInFlightRef.current = request;
    void request.finally(() => {
      if (refreshInFlightRef.current === request) {
        refreshInFlightRef.current = null;
      }
    });
    return () => {
      cancelled = true;
      if (viewportTimerRef.current) clearTimeout(viewportTimerRef.current);
    };
  }, [applyData, applyViewport, context.storage.user, loadData]);

  const selectCanvas = useCallback(
    async (canvasId: string) => {
      if (activeCanvasRef.current === canvasId) return;
      activeCanvasRef.current = canvasId;
      setActiveCanvas(canvasId);
      const viewport = await readCanvasViewport(
        context.storage.user,
        canvasId,
      ).catch(() => null);
      if (!aliveRef.current || activeCanvasRef.current !== canvasId) return;
      applyViewport(
        viewport,
        sessionsOnCanvas(sessions, canvasId, projectIds).length,
      );
    },
    [applyViewport, context.storage.user, projectIds, sessions],
  );

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
        if (initializedRef.current && !viewportDirtyRef.current) {
          applyDefaultViewport(visibleSessions.length);
        }
      }, RESIZE_REFIT_DELAY_MS);
    });
    observer.observe(container);
    return () => {
      if (timer) clearTimeout(timer);
      observer.disconnect();
    };
  }, [applyDefaultViewport, loading, visibleSessions.length]);

  // The tab strip only advertises a scrollbar when it actually overflows.
  useEffect(() => {
    const strip = tabsRef.current;
    if (!strip) return;
    const measure = () =>
      setTabsScrollable(strip.scrollWidth > strip.clientWidth + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(strip);
    return () => observer.disconnect();
  }, [loading, projects, newProjectName]);

  // No live feed yet: poll like the sidebar does so status, titles, and new
  // sessions keep up while the canvas is open, and catch up on window focus.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const refreshIfReady = () => {
      if (initializedRef.current && !document.hidden) void refresh();
    };
    const schedule = () => {
      timer = setTimeout(async () => {
        if (initializedRef.current && !document.hidden) await refresh();
        if (!cancelled) schedule();
      }, SESSION_POLL_INTERVAL_MS);
    };
    schedule();
    window.addEventListener("focus", refreshIfReady);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      window.removeEventListener("focus", refreshIfReady);
    };
  }, [refresh]);

  const onNodesChange = useCallback((changes: NodeChange<SessionNode>[]) => {
    setNodes((current) => applyNodeChanges(changes, current));
  }, []);

  const onNodeDragStop = useCallback(
    (_event: MouseEvent | TouchEvent, node: SessionNode) => {
      const next = {
        ...positionsRef.current,
        [node.id]: {
          x: Math.round(node.position.x),
          y: Math.round(node.position.y),
        },
      };
      positionsRef.current = next;
      persistedPositionsRef.current = upsertPosition(
        persistedPositionsRef.current,
        node.id,
        next[node.id],
      );
      void Promise.all([
        writePositionBucket(
          context.storage.user,
          persistedPositionsRef.current,
          positionBucket(node.id),
        ),
        writeCanvasViewport(
          context.storage.user,
          { ...getViewport(), ...containerSize() },
          activeCanvasRef.current,
        ),
      ])
        .then(() => {
          if (aliveRef.current) setStorageWarning(null);
        })
        .catch(() => {
          if (aliveRef.current) {
            setStorageWarning("Canvas layout could not be saved.");
          }
        });
    },
    [containerSize, context.storage.user, getViewport],
  );

  const onMoveEnd = useCallback(
    (event: MouseEvent | TouchEvent | null, viewport: CanvasViewport) => {
      if (!initializedRef.current || event === null) return;
      viewportDirtyRef.current = true;
      if (viewportTimerRef.current) clearTimeout(viewportTimerRef.current);
      const canvasId = activeCanvasRef.current;
      viewportTimerRef.current = setTimeout(() => {
        void writeCanvasViewport(
          context.storage.user,
          { ...viewport, ...containerSize() },
          canvasId,
        )
          .then(() => {
            if (aliveRef.current) setStorageWarning(null);
          })
          .catch(() => {
            if (aliveRef.current) {
              setStorageWarning("Canvas viewport could not be saved.");
            }
          });
      }, 250);
    },
    [containerSize, context.storage.user],
  );

  const resetLayout = useCallback(async () => {
    const ids = visibleSessions.map((session) => session.id);
    const removed = new Set(ids);
    const kept = Object.fromEntries(
      Object.entries(positionsRef.current).filter(([id]) => !removed.has(id)),
    ) as CanvasPositions;
    positionsRef.current = {
      ...kept,
      ...mergeSessionPositions(visibleSessions, {}),
    };
    setNodes(nodesFor(visibleSessions, positionsRef.current));
    try {
      persistedPositionsRef.current = await resetCanvasLayout(
        context.storage.user,
        activeCanvas,
        persistedPositionsRef.current,
        ids,
      );
      if (aliveRef.current) setStorageWarning(null);
    } catch {
      if (aliveRef.current) {
        setStorageWarning("Stored canvas layout could not be reset.");
      }
    }
    if (aliveRef.current) {
      requestAnimationFrame(() => applyDefaultViewport(visibleSessions.length));
    }
  }, [
    activeCanvas,
    context.storage.user,
    applyDefaultViewport,
    nodesFor,
    visibleSessions,
  ]);

  const submitProject = useCallback(async () => {
    const name = (newProjectName ?? "").trim();
    if (!name || savingProject) return;
    setSavingProject(true);
    setProjectError(null);
    try {
      const project = await context.projects.create({ name });
      if (!aliveRef.current) return;
      setProjects((current) => [
        ...current.filter((item) => item.id !== project.id),
        project,
      ]);
      setNewProjectName(null);
      await selectCanvas(project.id);
    } catch (reason) {
      if (aliveRef.current) {
        setProjectError(
          reason instanceof Error ? reason.message : "Could not create project",
        );
      }
    } finally {
      if (aliveRef.current) setSavingProject(false);
    }
  }, [context.projects, newProjectName, savingProject, selectCanvas]);

  const closeProjectForm = () => {
    setNewProjectName(null);
    setProjectError(null);
  };

  // Plain controls instead of a <form>: the sandboxed frame has no
  // `allow-forms`, so the browser would block a form submission outright.
  const onProjectNameKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") closeProjectForm();
    if (event.key === "Enter") {
      event.preventDefault();
      void submitProject();
    }
  };

  if (loading) {
    return (
      <div className="canvas-state">
        <svg
          className="canvas-spinner"
          role="status"
          aria-label="Loading Canvas"
          viewBox="0 0 24 24"
        >
          <path d="M21 12a9 9 0 1 1-6.22-8.56" />
        </svg>
      </div>
    );
  }
  if (error && sessions.length === 0) {
    return (
      <div className="canvas-state" role="alert">
        <strong>Canvas could not load</strong>
        <span>{error}</span>
        <button type="button" onClick={() => void refresh(true)}>
          Retry
        </button>
      </div>
    );
  }

  const canvasTabs = canReadProjects(context) && (
    <nav
      ref={tabsRef}
      className="canvas-tabs"
      aria-label="Canvases"
      data-scrollable={tabsScrollable}
    >
      <div role="tablist" className="canvas-tablist">
        <button
          type="button"
          role="tab"
          className="canvas-tab"
          aria-selected={activeCanvas === MAIN_CANVAS_ID}
          onClick={() => void selectCanvas(MAIN_CANVAS_ID)}
        >
          Main
        </button>
        {projects.map((project) => (
          <button
            key={project.id}
            type="button"
            role="tab"
            className="canvas-tab"
            aria-selected={activeCanvas === project.id}
            title={project.name}
            onClick={() => void selectCanvas(project.id)}
          >
            {project.icon && <span aria-hidden>{project.icon}</span>}
            <span className="canvas-tab-label">{project.name}</span>
          </button>
        ))}
      </div>
      {canCreateProjects(context) &&
        (newProjectName === null ? (
          <button
            type="button"
            className="canvas-tab canvas-tab-add"
            aria-label="New project"
            title="New project"
            onClick={() => {
              setProjectError(null);
              setNewProjectName("");
            }}
          >
            +
          </button>
        ) : (
          <div className="canvas-new-project">
            <input
              aria-label="Project name"
              placeholder="Project name"
              autoFocus
              maxLength={PROJECT_NAME_MAX_LENGTH}
              value={newProjectName}
              onChange={(event) => setNewProjectName(event.target.value)}
              onKeyDown={onProjectNameKeyDown}
            />
            <button
              type="button"
              disabled={savingProject || newProjectName.trim() === ""}
              onClick={() => void submitProject()}
            >
              {savingProject ? "Creating…" : "Create"}
            </button>
            <button type="button" onClick={closeProjectForm}>
              Cancel
            </button>
          </div>
        ))}
    </nav>
  );

  const emptyState = visibleSessions.length === 0 && (
    <div className="canvas-state canvas-empty">
      {activeProject ? (
        <>
          <strong>No sessions in {activeProject.name}</strong>
        </>
      ) : projects.length > 0 ? (
        <>
          <strong>No sessions outside projects</strong>
        </>
      ) : (
        <>
          <strong>No sessions</strong>
        </>
      )}
    </div>
  );

  return (
    <div
      className="canvas-shell"
      style={{ display: "flex", flexDirection: "column" }}
    >
      <header className="canvas-toolbar">
        <div>
          <h1>Canvas</h1>
          <div className="canvas-session-count">
            <span>{sessionCountLabel(visibleSessions.length)}</span>
            {loadingSessions && (
              <svg
                className="canvas-spinner"
                role="status"
                aria-label="Loading sessions"
                viewBox="0 0 24 24"
              >
                <path d="M21 12a9 9 0 1 1-6.22-8.56" />
              </svg>
            )}
          </div>
        </div>
      </header>
      {canvasTabs}
      {error && (
        <div className="canvas-banner" role="alert">
          Refresh failed: {error}
        </div>
      )}
      {projectError && (
        <div className="canvas-banner" role="alert">
          Project could not be created: {projectError}
        </div>
      )}
      {storageWarning && (
        <div className="canvas-banner" role="status">
          {storageWarning}
        </div>
      )}
      <div
        ref={flowContainerRef}
        className="canvas-flow"
        style={{ flex: 1, minHeight: 0 }}
      >
        <ReactFlow<SessionNode>
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
          {visibleSessions.length > 0 && <Background />}
          <Controls showInteractive={false}>
            <ControlButton
              onClick={() => void resetLayout()}
              title="Reset layout"
              aria-label="Reset layout"
            >
              {/* Inline fill: xyflow's controls CSS fills button SVGs. */}
              <svg
                viewBox="0 0 24 24"
                style={{ fill: "none" }}
                stroke="currentColor"
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden
              >
                <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
                <path d="M3 3v5h5" />
              </svg>
            </ControlButton>
          </Controls>
        </ReactFlow>
        {emptyState}
        <button
          type="button"
          className="canvas-fab"
          aria-label="New session"
          title="New session"
          onClick={() =>
            void context.navigation.openNewSession(
              activeProject ? { projectId: activeProject.id } : undefined,
            )
          }
        >
          <svg
            viewBox="0 0 24 24"
            width="22"
            height="22"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.25"
            strokeLinecap="round"
            aria-hidden
          >
            <path d="M5 12h14M12 5v14" />
          </svg>
        </button>
      </div>
    </div>
  );
}

export function CanvasApp({
  context,
  onReady,
}: {
  context: ExtensionContext;
  onReady?: () => void;
}) {
  return (
    <ReactFlowProvider>
      <CanvasSurface context={context} onReady={onReady} />
    </ReactFlowProvider>
  );
}
