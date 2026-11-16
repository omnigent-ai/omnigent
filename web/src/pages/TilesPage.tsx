// Tile view: every active agent as a tile. Clicking a tile makes it the active
// agent (the only one with a composer). Gallery shows live tiles, Focus puts the
// active agent in a large pane beside a live strip, Cards shows status only.

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "@/lib/routing";
import { LayoutGridIcon, Loader2Icon, PanelRightIcon, RectangleHorizontalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Conversation } from "@/hooks/useConversations";
import { useSidebarData } from "@/hooks/useSidebarData";
import { cn } from "@/lib/utils";
import { getConnectionProtocol, maxLiveConversations } from "@/store/conversationRegistry";
import { AgentTile, type AgentTileVariant } from "@/tiles/AgentTile";
import { ColumnsPicker } from "@/tiles/ColumnsPicker";
import { TileGrid, type TileResize } from "@/tiles/TileGrid";
import { fittingColumns, setTileColumns, useTileColumns } from "@/tiles/tileLayout";
import {
  needsYou,
  parseTilesView,
  partitionActive,
  pickLiveIds,
  resolveActiveId,
  tileStreamBudget,
  type TilesView,
} from "@/tiles/tileSelection";
import { readTilesView, writeTilesView } from "@/tiles/tilesPreferences";
import { useReleaseIdleTileStreams } from "@/tiles/useTileStreams";

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
  const { active: tiles, dormant } = useMemo(() => partitionActive(rows, Date.now()), [rows]);
  const tilesById = useMemo(() => new Map(tiles.map((tile) => [tile.id, tile])), [tiles]);
  const orderedIds = useMemo(() => tiles.map((tile) => tile.id), [tiles]);
  const activeId = resolveActiveId(tiles, chosenId);
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
  const activate = useCallback(
    (sessionId: string) =>
      updateParams(view === "cards" ? { active: sessionId, view: "focus" } : { active: sessionId }),
    [updateParams, view],
  );
  const openSingle = useCallback((sessionId: string) => navigate(`/c/${sessionId}`), [navigate]);

  const chosenColumns = useTileColumns();
  const [gridWidth, setGridWidth] = useState(0);

  const working = tiles.filter((tile) => tile.status === "running").length;
  const waiting = tiles.filter(needsYou).length;
  const activeTile = activeId ? (tilesById.get(activeId) ?? null) : null;

  const renderTile = (id: string, variant: AgentTileVariant, resize?: TileResize) => {
    const conversation = tilesById.get(id);
    if (!conversation) return null;
    return (
      <AgentTile
        conversation={conversation}
        active={id === activeId}
        live={liveIds.has(id)}
        variant={variant}
        onActivate={activate}
        onOpenSingle={openSingle}
        resize={resize}
      />
    );
  };

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="tiles-page">
      <div className="flex shrink-0 flex-wrap items-center gap-3 border-b px-4 py-2.5">
        <h1 className="text-base font-semibold">Agents</h1>
        <p className="text-xs text-muted-foreground" data-testid="tiles-summary">
          {tiles.length} active · {working} working · {waiting} need you · {dormant.length} dormant
          {view !== "cards" && liveIds.size < tiles.length && ` · ${liveIds.size} streaming`}
        </p>
        <div className="ml-auto flex items-center gap-2">
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

      {isLoading ? (
        <div className="flex flex-1 items-center justify-center text-muted-foreground">
          <Loader2Icon className="size-5 animate-spin" aria-label="Loading agents" />
        </div>
      ) : tiles.length === 0 ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          No active agents. Sessions that are running, waiting on you, or used in the last two days
          appear here.
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
    </div>
  );
}
