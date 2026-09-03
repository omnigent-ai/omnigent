import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ExtensionContext,
  ExtensionSessionSummary,
} from "@omnigent/extension-sdk";
import {
  applyNodeChanges,
  Background,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Node,
  type NodeChange,
} from "@xyflow/react";
import {
  mergeSessionPositions,
  prunePositions,
  type CanvasPositions,
} from "./canvasLayout";
import {
  positionBucket,
  readCanvasLayout,
  resetCanvasLayout,
  upsertPosition,
  writeCanvasViewport,
  writePositionBucket,
  type CanvasViewport,
} from "./canvasStorage";
import { loadSessions } from "./sessionData";
import { SessionCardNode, type SessionCardData } from "./SessionCardNode";

const nodeTypes = { session: SessionCardNode };
const proOptions = { hideAttribution: true };
const defaultViewport: CanvasViewport = { x: 0, y: 0, zoom: 1 };
const RECONCILE_INTERVAL_MS = 30_000;
type SessionNode = Node<SessionCardData, "session">;

function isMobileViewport(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    !window.matchMedia("(min-width: 768px)").matches
  );
}

function CanvasSurface({ context }: { context: ExtensionContext }) {
  const flow = useReactFlow();
  const [nodes, setNodes] = useState<SessionNode[]>([]);
  const [sessions, setSessions] = useState<ExtensionSessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [storageWarning, setStorageWarning] = useState<string | null>(null);
  const [liveWarning, setLiveWarning] = useState<string | null>(null);
  const [savedViewport, setSavedViewport] = useState<CanvasViewport | null>(
    null,
  );
  const [mobile, setMobile] = useState(isMobileViewport);
  const [arrangeMode, setArrangeMode] = useState(() => !isMobileViewport());
  const positionsRef = useRef<CanvasPositions>({});
  const persistedPositionsRef = useRef<CanvasPositions>({});
  const openingRef = useRef(false);
  const initializedRef = useRef(false);
  const viewportTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const aliveRef = useRef(true);
  const liveDirtyRef = useRef(false);
  const refreshQueuedRef = useRef(false);
  const refreshInFlightRef = useRef<Promise<void> | null>(null);
  const refreshRef = useRef<(() => Promise<void>) | null>(null);
  const liveCooldownTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );

  useEffect(() => {
    return () => {
      aliveRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(min-width: 768px)");
    const update = () => {
      const nextMobile = !media.matches;
      setMobile(nextMobile);
      setArrangeMode(!nextMobile);
    };
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

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

  const nodesFor = useCallback(
    (
      items: ExtensionSessionSummary[],
      positions: CanvasPositions,
    ): SessionNode[] =>
      items.map((session) => ({
        id: session.id,
        type: "session",
        position: positions[session.id],
        data: { session, onOpen: openSession },
        selectable: true,
        focusable: false,
      })),
    [openSession],
  );

  const applySessions = useCallback(
    async (items: ExtensionSessionSummary[], persistPruned: boolean) => {
      const previousPersisted = persistedPositionsRef.current;
      const persisted = prunePositions(
        previousPersisted,
        items.map((session) => session.id),
      );
      const positions = mergeSessionPositions(items, positionsRef.current);
      persistedPositionsRef.current = persisted;
      positionsRef.current = positions;
      setSessions(items);
      setNodes(nodesFor(items, positions));
      if (persistPruned) {
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
        } catch {
          if (aliveRef.current) {
            setStorageWarning(
              "Canvas layout could not be saved in this browser.",
            );
          }
        }
      }
    },
    [context.storage.user, nodesFor],
  );

  const performRefresh = useCallback(async () => {
    try {
      const items = await loadSessions(context);
      if (!aliveRef.current) return;
      await applySessions(items, true);
      if (aliveRef.current) setError(null);
    } catch (reason) {
      if (aliveRef.current) {
        setError(
          reason instanceof Error ? reason.message : "Could not load sessions",
        );
      }
    }
  }, [applySessions, context]);

  const refresh = useCallback((): Promise<void> => {
    if (refreshInFlightRef.current) {
      refreshQueuedRef.current = true;
      return refreshInFlightRef.current;
    }
    if (aliveRef.current) setRefreshing(true);
    const scheduleCooldownRefresh = () => {
      if (liveCooldownTimerRef.current) return;
      liveCooldownTimerRef.current = setTimeout(() => {
        liveCooldownTimerRef.current = null;
        if (
          aliveRef.current &&
          liveDirtyRef.current &&
          document.visibilityState === "visible"
        ) {
          liveDirtyRef.current = false;
          void refreshRef.current?.();
        }
      }, 1_000);
    };
    const drain = async () => {
      refreshQueuedRef.current = false;
      await performRefresh();
      if (!aliveRef.current || !refreshQueuedRef.current) return;
      if (document.visibilityState !== "visible") {
        liveDirtyRef.current = true;
        refreshQueuedRef.current = false;
        return;
      }
      refreshQueuedRef.current = false;
      await performRefresh();
      if (refreshQueuedRef.current) {
        liveDirtyRef.current = true;
        refreshQueuedRef.current = false;
        scheduleCooldownRefresh();
      }
    };
    const current = drain().finally(() => {
      if (refreshInFlightRef.current === current) {
        refreshInFlightRef.current = null;
      }
      if (aliveRef.current) setRefreshing(false);
    });
    refreshInFlightRef.current = current;
    return current;
  }, [performRefresh]);
  refreshRef.current = refresh;

  useEffect(() => {
    if (initializedRef.current) return;
    let cancelled = false;
    void Promise.all([
      readCanvasLayout(context.storage.user).catch(() => ({
        positions: {},
        viewport: null,
      })),
      loadSessions(context),
    ]).then(
      async ([layout, items]) => {
        if (cancelled) return;
        persistedPositionsRef.current = layout.positions;
        positionsRef.current = layout.positions;
        setSavedViewport(layout.viewport);
        await applySessions(items, true);
        if (cancelled || !aliveRef.current) return;
        initializedRef.current = true;
        setLoading(false);
        if (!layout.viewport) {
          requestAnimationFrame(() =>
            flow.fitView({ padding: 0.2, duration: 0 }),
          );
        }
      },
      (reason: unknown) => {
        if (cancelled) return;
        setError(
          reason instanceof Error ? reason.message : "Could not load sessions",
        );
        setLoading(false);
      },
    );
    return () => {
      cancelled = true;
      if (viewportTimerRef.current) clearTimeout(viewportTimerRef.current);
      if (liveCooldownTimerRef.current)
        clearTimeout(liveCooldownTimerRef.current);
    };
  }, [applySessions, context, flow]);

  useEffect(() => {
    if (loading || !context.capabilities.includes("sessions.subscribe")) return;
    let disposed = false;
    let subscription: { dispose(): void } | null = null;
    const consumeDirty = () => {
      if (
        document.visibilityState !== "visible" ||
        liveCooldownTimerRef.current
      ) {
        return;
      }
      liveDirtyRef.current = false;
      void refresh();
    };
    const onSessionChange = () => {
      liveDirtyRef.current = true;
      consumeDirty();
    };
    const onVisibilityChange = () => {
      if (document.visibilityState !== "visible") return;
      liveDirtyRef.current = true;
      consumeDirty();
    };
    const reconcileTimer = setInterval(() => {
      liveDirtyRef.current = true;
      consumeDirty();
    }, RECONCILE_INTERVAL_MS);
    document.addEventListener("visibilitychange", onVisibilityChange);
    void context.sessions.subscribe(onSessionChange).then(
      (nextSubscription) => {
        if (disposed) nextSubscription.dispose();
        else {
          subscription = nextSubscription;
          if (aliveRef.current) {
            setLiveWarning(null);
            liveDirtyRef.current = true;
            consumeDirty();
          }
        }
      },
      (reason: unknown) => {
        if (disposed || !aliveRef.current) return;
        setLiveWarning(
          reason instanceof Error
            ? `Live updates unavailable: ${reason.message}`
            : "Live updates are unavailable.",
        );
      },
    );
    return () => {
      disposed = true;
      subscription?.dispose();
      clearInterval(reconcileTimer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [context, loading, refresh]);

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
        writeCanvasViewport(context.storage.user, flow.getViewport()),
      ]).catch(() => {
        if (aliveRef.current) {
          setStorageWarning(
            "Canvas layout could not be saved in this browser.",
          );
        }
      });
    },
    [context.storage.user, flow],
  );

  const onMoveEnd = useCallback(
    (_event: MouseEvent | TouchEvent | null, viewport: CanvasViewport) => {
      if (!initializedRef.current) return;
      if (viewportTimerRef.current) clearTimeout(viewportTimerRef.current);
      viewportTimerRef.current = setTimeout(() => {
        void writeCanvasViewport(context.storage.user, viewport).catch(() => {
          if (aliveRef.current) {
            setStorageWarning(
              "Canvas viewport could not be saved in this browser.",
            );
          }
        });
      }, 250);
    },
    [context.storage.user],
  );

  const resetLayout = useCallback(async () => {
    const positions = mergeSessionPositions(sessions, {});
    positionsRef.current = positions;
    persistedPositionsRef.current = {};
    setNodes(nodesFor(sessions, positions));
    setSavedViewport(null);
    try {
      await resetCanvasLayout(context.storage.user);
    } catch {
      if (aliveRef.current) {
        setStorageWarning("Stored canvas layout could not be reset.");
      }
    }
    if (aliveRef.current) {
      requestAnimationFrame(() => flow.fitView({ padding: 0.2, duration: 0 }));
    }
  }, [context.storage.user, flow, nodesFor, sessions]);

  if (loading) {
    return (
      <div className="canvas-state" role="status">
        Loading sessions…
      </div>
    );
  }
  if (error && sessions.length === 0) {
    return (
      <div className="canvas-state" role="alert">
        <strong>Canvas could not load</strong>
        <span>{error}</span>
        <button type="button" onClick={() => void refresh()}>
          Retry
        </button>
      </div>
    );
  }
  if (sessions.length === 0) {
    return (
      <div className="canvas-state">
        <strong>No non-archived sessions</strong>
        <span>Start a session and it will appear here.</span>
        <button
          type="button"
          onClick={() => void context.navigation.openNewSession()}
        >
          New session
        </button>
      </div>
    );
  }

  return (
    <div
      className="canvas-shell"
      style={{ display: "flex", flexDirection: "column" }}
    >
      <header className="canvas-toolbar">
        <div>
          <h1>Canvas</h1>
          <span>{sessions.length} sessions</span>
        </div>
        <div className="canvas-toolbar-actions">
          {mobile && (
            <button
              type="button"
              aria-pressed={arrangeMode}
              onClick={() => setArrangeMode((value) => !value)}
            >
              Arrange
            </button>
          )}
          <button
            type="button"
            disabled={refreshing}
            onClick={() => void refresh()}
          >
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
          <button
            type="button"
            onClick={() => flow.fitView({ padding: 0.2, duration: 150 })}
          >
            Fit view
          </button>
          <button type="button" onClick={() => void resetLayout()}>
            Reset layout
          </button>
        </div>
      </header>
      {error && (
        <div className="canvas-banner" role="alert">
          Refresh failed: {error}
        </div>
      )}
      {storageWarning && (
        <div className="canvas-banner" role="status">
          {storageWarning}
        </div>
      )}
      {liveWarning && (
        <div className="canvas-banner" role="status">
          {liveWarning}
        </div>
      )}
      <div className="canvas-flow" style={{ flex: 1, minHeight: 0 }}>
        <ReactFlow<SessionNode>
          nodes={nodes}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onNodeDragStop={onNodeDragStop}
          onNodeDoubleClick={(_event, node) => openSession(node.id)}
          onMoveEnd={onMoveEnd}
          defaultViewport={savedViewport ?? defaultViewport}
          nodesDraggable={arrangeMode}
          nodesConnectable={false}
          elementsSelectable
          nodesFocusable={false}
          zoomOnDoubleClick={false}
          panOnScroll
          nodeDragThreshold={3}
          onlyRenderVisibleElements
          minZoom={0.2}
          maxZoom={2.5}
          proOptions={proOptions}
        >
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
    </div>
  );
}

export function CanvasApp({ context }: { context: ExtensionContext }) {
  return (
    <ReactFlowProvider>
      <CanvasSurface context={context} />
    </ReactFlowProvider>
  );
}
