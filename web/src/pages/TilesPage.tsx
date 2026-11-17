// Tile view: every active agent as a tile. Clicking a tile makes it the active
// agent (the only one with a composer). Gallery shows live tiles, Focus puts the
// active agent in a large pane beside a live strip, Cards shows status only.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useRebasePath, useSearchParams } from "@/lib/routing";
import { isElectronShell } from "@/lib/nativeBridge";
import {
  LayersIcon,
  LayoutGridIcon,
  Loader2Icon,
  PanelRightIcon,
  RectangleHorizontalIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Conversation } from "@/hooks/useConversations";
import { useSidebarData } from "@/hooks/useSidebarData";
import { cn } from "@/lib/utils";
import { getConnectionProtocol, maxLiveConversations } from "@/store/conversationRegistry";
import { AgentTile, type AgentTileVariant, type TileReorder } from "@/tiles/AgentTile";
import {
  applyTileOrder,
  moveTile,
  moveTileBy,
  pruneTileOrder,
  readTileOrder,
  useTileOrder,
  writeTileOrder,
} from "@/tiles/tileOrder";
import { useTilesShortcuts } from "@/tiles/useTilesShortcuts";
import { ColumnsPicker } from "@/tiles/ColumnsPicker";
import { TileGrid, type TileResize } from "@/tiles/TileGrid";
import { fittingColumns, setTileColumns, useTileColumns } from "@/tiles/tileLayout";
import {
  parseTilesView,
  partitionActive,
  pickLiveIds,
  resolveActiveId,
  tileStreamBudget,
  type TilesView,
} from "@/tiles/tileSelection";
import {
  readTilesView,
  useTileStatusFilter,
  writeTileStatusFilter,
  writeTilesView,
} from "@/tiles/tilesPreferences";
import { useReleaseIdleTileStreams } from "@/tiles/useTileStreams";
import {
  ActiveTileWorkspace,
  type TileFileContext,
  type TileSwitchGuard,
} from "@/tiles/ActiveTileWorkspace";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { useResizableWorkspacePanel } from "@/shell/useResizableWorkspacePanel";
import { TileStackDrawer } from "@/tiles/TileStackDrawer";
import {
  isQuiet,
  parkTile,
  partitionStack,
  pruneStack,
  unstackTile,
  useTileStack,
  writeTileStack,
} from "@/tiles/tileStack";
import { canFavorite, useTileFavorite } from "@/tiles/useTileFavorite";
import { TileFilterBar } from "@/tiles/TileFilterBar";
import { favoritesFirst, filterTiles, projectsOf, statusCounts } from "@/tiles/tileFilters";
import { isSessionPinned } from "@/lib/sessionFamilies";

const VIEW_OPTIONS: { view: TilesView; label: string; icon: typeof LayoutGridIcon }[] = [
  { view: "cards", label: "Cards", icon: RectangleHorizontalIcon },
  { view: "gallery", label: "Gallery", icon: LayoutGridIcon },
  { view: "focus", label: "Focus", icon: PanelRightIcon },
];

export function TilesPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const view = parseTilesView(searchParams.get("view")) ?? readTilesView();
  const chosenId = searchParams.get("active");

  // Read the sidebar's own "mine" rows: a second query on its cache key would
  // overwrite the sidebar's cache shape.
  const { mine } = useSidebarData();
  const rows = useMemo<Conversation[]>(
    () => mine.data?.pages.flatMap((page) => page.data ?? []) ?? [],
    [mine.data],
  );
  const isLoading = mine.data === undefined && mine.isLoading;
  const { active: activeTiles, dormant } = useMemo(() => partitionActive(rows, Date.now()), [rows]);
  const statusFilter = useTileStatusFilter();
  const projectFilter = searchParams.get("project");
  const [query, setQuery] = useState("");
  const visible = useMemo(
    () =>
      favoritesFirst(
        filterTiles(activeTiles, { status: statusFilter, project: projectFilter, query }),
      ),
    [activeTiles, statusFilter, projectFilter, query],
  );
  const stackState = useTileStack();
  const { onGrid: tiles, stacked } = useMemo(
    () => partitionStack(visible, stackState, Date.now()),
    [visible, stackState],
  );
  const quietIds = useMemo(
    () => new Set(stacked.filter((row) => isQuiet(row, Date.now())).map((row) => row.id)),
    [stacked],
  );
  const [stackOpen, setStackOpen] = useState(false);
  const park = useCallback(
    (sessionId: string) => writeTileStack(parkTile(stackState, sessionId)),
    [stackState],
  );
  const unstack = useCallback(
    (conversation: Conversation) =>
      writeTileStack(unstackTile(stackState, conversation.id, quietIds.has(conversation.id))),
    [stackState, quietIds],
  );
  // Forget stacked ids whose sessions are gone, once the list has loaded.
  useEffect(() => {
    if (rows.length === 0) return;
    const pruned = pruneStack(stackState, new Set(rows.map((row) => row.id)));
    if (
      pruned.parked.length !== stackState.parked.length ||
      pruned.kept.length !== stackState.kept.length
    ) {
      writeTileStack(pruned);
    }
  }, [rows, stackState]);
  useEffect(() => {
    if (rows.length === 0) return;
    const saved = readTileOrder();
    const pruned = pruneTileOrder(saved, new Set(rows.map((row) => row.id)));
    if (pruned.length !== saved.length) writeTileOrder(pruned);
  }, [rows]);
  const counts = useMemo(() => statusCounts(activeTiles), [activeTiles]);
  const projects = useMemo(() => projectsOf(activeTiles), [activeTiles]);
  const filtering = statusFilter !== "all" || projectFilter !== null || query !== "";
  const favorite = useTileFavorite();
  const tilesById = useMemo(() => new Map(tiles.map((tile) => [tile.id, tile])), [tiles]);
  const tileOrder = useTileOrder();
  const orderedIds = useMemo(
    () =>
      applyTileOrder(
        tiles.map((tile) => tile.id),
        tileOrder,
      ),
    [tiles, tileOrder],
  );
  const orderedIdsRef = useRef(orderedIds);
  orderedIdsRef.current = orderedIds;
  const reorder = useMemo<TileReorder>(
    () => ({
      onMove: (id, targetId) =>
        writeTileOrder(moveTile(readTileOrder(), orderedIdsRef.current, id, targetId)),
      onMoveBy: (id, delta) =>
        writeTileOrder(moveTileBy(readTileOrder(), orderedIdsRef.current, id, delta)),
    }),
    [],
  );
  const activeId = resolveActiveId(tiles, chosenId);
  const activeIdRef = useRef(activeId);
  activeIdRef.current = activeId;
  const liveIds = useMemo(
    () =>
      view === "cards"
        ? new Set<string>()
        : pickLiveIds(
            orderedIds,
            activeId,
            tileStreamBudget(getConnectionProtocol(), maxLiveConversations()),
          ),
    [view, orderedIds, activeId],
  );
  useReleaseIdleTileStreams(liveIds, activeId);
  // Keep the resolved active agent in the URL, so Single agent opens the same one.
  useEffect(() => {
    if (activeId && activeId !== chosenId) {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.set("active", activeId);
          return next;
        },
        { replace: true },
      );
    }
  }, [activeId, chosenId, setSearchParams]);
  useEffect(() => writeTilesView(view), [view]);

  const updateParams = useCallback(
    (patch: Record<string, string>) =>
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          for (const [key, value] of Object.entries(patch)) next.set(key, value);
          return next;
        },
        { replace: true },
      ),
    [setSearchParams],
  );
  // Cards are an overview: picking one opens it in Focus as the active agent.
  // The Workspace panel follows the active tile; switching asks it about unsaved edits.
  const workspaceUi = useResizableWorkspacePanel("tiles", "Resize tiles workspace");
  const switchGuard = useRef<TileSwitchGuard | null>(null);
  const setSwitchGuard = useCallback((guard: TileSwitchGuard | null) => {
    switchGuard.current = guard;
  }, []);
  const [tileFileContext, setTileFileContext] = useState<TileFileContext | null>(null);
  const activate = useCallback(
    (sessionId: string) => {
      const apply = () =>
        updateParams(
          view === "cards" ? { active: sessionId, view: "focus" } : { active: sessionId },
        );
      if (switchGuard.current && sessionId !== activeIdRef.current) switchGuard.current(apply);
      else apply();
    },
    [updateParams, view],
  );
  const openSingle = useCallback((sessionId: string) => navigate(`/c/${sessionId}`), [navigate]);
  const rebasePath = useRebasePath();
  // The desktop shell sends window.open to the system browser, so pop-out is browser-only.
  const popOut = useMemo(
    () =>
      isElectronShell()
        ? undefined
        : (sessionId: string) =>
            window.open(
              rebasePath(`/c/${sessionId}`),
              `omnigent-agent-${sessionId}`,
              "popup,width=960,height=820",
            ),
    [rebasePath],
  );
  const [fullScreenId, setFullScreenId] = useState<string | null>(null);
  const toggleFullScreen = useCallback(
    (sessionId: string) => {
      setFullScreenId((current) => (current === sessionId ? null : sessionId));
      activate(sessionId);
    },
    [activate],
  );

  const chosenColumns = useTileColumns();
  const [gridWidth, setGridWidth] = useState(0);

  const working = counts.working;
  const waiting = counts["needs-you"];
  const activeTile = activeId ? (tilesById.get(activeId) ?? null) : null;
  const showWorkspace = view !== "cards" && activeTile !== null;
  const fullScreenTile =
    fullScreenId && view !== "cards" ? (tilesById.get(fullScreenId) ?? null) : null;
  useTilesShortcuts({
    ids: orderedIds,
    activeId,
    onActivate: activate,
    fullScreen: fullScreenTile !== null,
    onToggleFullScreen: () => {
      if (fullScreenTile) setFullScreenId(null);
      else if (activeId && view !== "cards") toggleFullScreen(activeId);
    },
  });

  const renderTile = (id: string, variant: AgentTileVariant, resize?: TileResize) => {
    const conversation = tilesById.get(id);
    if (!conversation) return null;
    const tile = (
      <AgentTile
        conversation={conversation}
        active={id === activeId}
        live={liveIds.has(id)}
        variant={variant}
        onActivate={activate}
        onOpenSingle={openSingle}
        resize={resize}
        starred={isSessionPinned(conversation)}
        starDisabled={favorite.saving}
        onToggleStar={canFavorite(conversation) ? favorite.toggle : undefined}
        onPark={park}
        fullScreen={id === fullScreenTile?.id}
        onToggleFullScreen={variant === "card" ? undefined : toggleFullScreen}
        onPopOut={popOut}
        reorder={fullScreenTile ? undefined : reorder}
      />
    );
    return id === activeId && tileFileContext ? (
      <FileViewerContext.Provider value={tileFileContext}>{tile}</FileViewerContext.Provider>
    ) : (
      tile
    );
  };

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="tiles-page">
      <div className="flex shrink-0 flex-wrap items-center gap-3 border-b px-4 py-2.5">
        <h1 className="text-base font-semibold">Agents</h1>
        <p className="text-xs text-muted-foreground" data-testid="tiles-summary">
          {activeTiles.length} active · {working} working · {waiting} need you · {stacked.length}{" "}
          stacked · {dormant.length} dormant
          {filtering && ` · ${tiles.length} shown`}
          {view !== "cards" && liveIds.size < tiles.length && ` · ${liveIds.size} streaming`}
        </p>
        <div className="ml-auto flex items-center gap-2">
          {showWorkspace && (
            <Button
              variant="ghost"
              size="xs"
              className={cn("gap-1.5", workspaceUi.open && "bg-accent text-accent-foreground")}
              aria-pressed={workspaceUi.open}
              data-testid="tiles-workspace-toggle"
              title="Files, changes and terminals for the active agent"
              onClick={workspaceUi.toggleWorkspace}
            >
              <PanelRightIcon className="size-3.5" />
              Files
            </Button>
          )}
          <Button
            variant="ghost"
            size="xs"
            className={cn("gap-1.5", stackOpen && "bg-accent text-accent-foreground")}
            aria-expanded={stackOpen}
            aria-controls="tiles-stack"
            data-testid="tiles-stack-toggle"
            onClick={() => setStackOpen((open) => !open)}
          >
            <LayersIcon className="size-3.5" />
            Stack · {stacked.length}
          </Button>
          {view !== "focus" && (
            <ColumnsPicker
              columns={chosenColumns}
              fitting={gridWidth > 0 ? fittingColumns(gridWidth) : chosenColumns}
              onChange={setTileColumns}
            />
          )}
          <div
            role="radiogroup"
            aria-label="Tile layout"
            className="flex items-center gap-0.5 rounded-md border p-0.5"
          >
            {VIEW_OPTIONS.map(({ view: option, label, icon: Icon }) => (
              <Button
                key={option}
                role="radio"
                aria-checked={view === option}
                variant="ghost"
                size="xs"
                className={cn("gap-1.5", view === option && "bg-accent text-accent-foreground")}
                onClick={() => updateParams({ view: option })}
              >
                <Icon className="size-3.5" />
                {label}
              </Button>
            ))}
          </div>
        </div>
      </div>
      {activeTiles.length > 0 && (
        <TileFilterBar
          status={statusFilter}
          counts={counts}
          onStatusChange={writeTileStatusFilter}
          projects={projects}
          project={projectFilter}
          onProjectChange={(next) =>
            setSearchParams(
              (current) => {
                const params = new URLSearchParams(current);
                if (next) params.set("project", next);
                else params.delete("project");
                return params;
              },
              { replace: true },
            )
          }
          query={query}
          onQueryChange={setQuery}
        />
      )}

      <div ref={workspaceUi.container} className="flex min-h-0 min-w-0 flex-1">
        {isLoading ? (
          <div className="flex flex-1 items-center justify-center text-muted-foreground">
            <Loader2Icon className="size-5 animate-spin" aria-label="Loading agents" />
          </div>
        ) : tiles.length === 0 && filtering ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 text-sm text-muted-foreground">
            No agents match these filters.
            <Button
              variant="outline"
              size="xs"
              onClick={() => {
                writeTileStatusFilter("all");
                setQuery("");
                setSearchParams(
                  (current) => {
                    const params = new URLSearchParams(current);
                    params.delete("project");
                    return params;
                  },
                  { replace: true },
                );
              }}
            >
              Clear filters
            </Button>
          </div>
        ) : tiles.length === 0 && stacked.length > 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 text-sm text-muted-foreground">
            All your agents are in the Stack.
            <Button variant="outline" size="xs" onClick={() => setStackOpen(true)}>
              Show the Stack
            </Button>
          </div>
        ) : tiles.length === 0 ? (
          <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
            No active agents. Sessions that are running, waiting on you, or used in the last two
            days appear here.
          </div>
        ) : fullScreenTile ? (
          <div className="flex min-h-0 flex-1 p-3" data-testid="tiles-full-screen">
            {renderTile(fullScreenTile.id, "tile")}
          </div>
        ) : view === "focus" && activeTile ? (
          <div className="flex min-h-0 flex-1 gap-3 p-3">
            <div className="flex min-h-0 min-w-0 flex-1">{renderTile(activeTile.id, "tile")}</div>
            {tiles.length > 1 && (
              <div className="flex min-h-0 w-[380px] shrink-0 flex-col gap-3 overflow-y-auto">
                {orderedIds
                  .filter((id) => id !== activeTile.id)
                  .map((id) => (
                    <div key={id} className="flex h-[260px] shrink-0">
                      {renderTile(id, "strip")}
                    </div>
                  ))}
              </div>
            )}
          </div>
        ) : (
          <TileGrid
            ids={orderedIds}
            chosenColumns={chosenColumns}
            layout={view === "cards" ? "cards" : "gallery"}
            onMeasure={setGridWidth}
            renderTile={(id, resize) => renderTile(id, view === "cards" ? "card" : "tile", resize)}
          />
        )}
        {showWorkspace && activeTile && (
          <ActiveTileWorkspace
            key={activeTile.id}
            sessionId={activeTile.id}
            workspacePanel={workspaceUi.workspacePanel}
            onOpenWorkspace={workspaceUi.openWorkspace}
            onFileContext={setTileFileContext}
            onSwitchGuard={setSwitchGuard}
          />
        )}
      </div>
      {stackOpen && (
        <div id="tiles-stack">
          <TileStackDrawer
            stacked={stacked}
            quietIds={quietIds}
            onUnstack={unstack}
            onOpenSingle={openSingle}
          />
        </div>
      )}
    </div>
  );
}
