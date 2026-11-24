import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { toast } from "sonner";
import { useIdentityReady, useViewerId } from "@/hooks/useViewerId";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { getOmnigentServerIdentity } from "@/lib/host";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { randomUUID } from "@/lib/randomUUID";
import { useNavigate, useParams } from "@/lib/routing";
import { runWithLeaveGuards } from "@/lib/routeLeaveGuards";
import { USER_SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitles";
import { isTempConvId } from "@/lib/tempConversationId";
import {
  gridSessionIds,
  parseSessionGrids,
  removeGridSession,
  splitGrid,
  swapGridSessions,
  type SessionGrid,
  type SplitEdge,
} from "./gridLayout";

interface GridSessionsContextValue {
  enabled: boolean;
  desktop: boolean;
  identityReady: boolean;
  grids: SessionGrid[];
  activeGrid?: SessionGrid;
  conversationId?: string;
  /** Focused pane of the displayed grid; defaults to its first session. */
  selectedSessionId?: string;
  selectSession: (sessionId: string) => void;
  split: (targetId: string, sessionId: string, edge: SplitEdge) => void;
  closePane: (sessionId: string) => void;
  deleteGrid: (id: string) => void;
  renameGrid: (id: string, name: string) => void;
  swapPanes: (firstId: string, secondId: string, edge?: SplitEdge) => void;
}

const GridSessionsContext = createContext<GridSessionsContextValue | null>(null);
const EMPTY_GRIDS: SessionGrid[] = [];
export const useGridSessions = () => useContext(GridSessionsContext);

export function GridSessionsProvider({ children }: { children: ReactNode }) {
  const enabled = isFeatureEnabled(useServerInfo(), "grid_sessions_display_ui");
  return enabled ? <EnabledGridSessionsProvider>{children}</EnabledGridSessionsProvider> : children;
}

function EnabledGridSessionsProvider({ children }: { children: ReactNode }) {
  const mobile = useIsMobileViewport();
  const [desktop, setDesktop] = useState(!mobile);
  const viewerId = useViewerId();
  const identityReady = useIdentityReady();
  const { conversationId: routeConversationId, gridId } = useParams<{
    conversationId: string;
    gridId: string;
  }>();
  const conversationId = isTempConvId(routeConversationId) ? undefined : routeConversationId;
  const navigate = useNavigate();
  useLayoutEffect(() => {
    if (desktop === !mobile) return;
    if (!mobile || !gridId) setDesktop(!mobile);
    else runWithLeaveGuards(() => setDesktop(false));
  }, [desktop, mobile, gridId]);
  const storageKey =
    viewerId === null || !desktop
      ? null
      : `omnigent:session-grids:${getOmnigentServerIdentity() ?? "default"}:${viewerId}`;
  const read = useCallback(() => {
    if (storageKey === null) return EMPTY_GRIDS;
    try {
      return parseSessionGrids(localStorage.getItem(storageKey));
    } catch {
      return [];
    }
  }, [storageKey]);
  const [saved, setSaved] = useState(() => ({ key: storageKey, grids: read() }));
  if (saved.key !== storageKey) setSaved({ key: storageKey, grids: read() });
  useEffect(() => {
    if (storageKey === null) return;
    const onStorage = (event: StorageEvent) => {
      if (event.key !== null && event.key !== storageKey) return;
      if (event.storageArea !== localStorage) return;
      const next = read();
      const currentGrid = saved.grids.find((grid) => grid.id === gridId);
      const nextGrid = next.find((grid) => grid.id === gridId);
      const currentIds = currentGrid ? gridSessionIds(currentGrid.layout) : [];
      const remainingIds = nextGrid
        ? gridSessionIds(nextGrid.layout).filter((id) => currentIds.includes(id))
        : [];
      const retainedIds = currentIds.filter((id) => remainingIds.includes(id));
      const affected = currentIds.filter(
        (id) => !remainingIds.includes(id) || remainingIds.indexOf(id) !== retainedIds.indexOf(id),
      );
      runWithLeaveGuards(() => setSaved({ key: storageKey, grids: next }), affected);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [storageKey, read, saved.grids, gridId]);
  const grids = saved.key === storageKey ? saved.grids : EMPTY_GRIDS;
  const activeGrid = grids.find((grid) => grid.id === gridId);
  const [selection, setSelection] = useState<{ gridId: string; sessionId: string } | null>(null);
  const activeSessionIds = activeGrid ? gridSessionIds(activeGrid.layout) : [];
  const selectedSessionId =
    selection !== null &&
    selection.gridId === gridId &&
    activeSessionIds.includes(selection.sessionId)
      ? selection.sessionId
      : activeSessionIds[0];
  const value = useMemo<GridSessionsContextValue>(() => {
    const save = (next: SessionGrid[]) => {
      if (storageKey === null) return;
      setSaved({ key: storageKey, grids: next });
      try {
        localStorage.setItem(storageKey, JSON.stringify(next));
      } catch {
        toast.warning("Grid changes could not be saved on this device.");
      }
    };
    const deleteGrid = (id: string) => {
      if (storageKey === null) return;
      const remove = () => save(grids.filter((grid) => grid.id !== id));
      if (id === gridId)
        runWithLeaveGuards(() => {
          remove();
          navigate("/");
        });
      else remove();
    };
    return {
      enabled: storageKey !== null,
      desktop,
      identityReady,
      grids,
      activeGrid,
      conversationId,
      selectedSessionId,
      selectSession: (sessionId) => {
        if (gridId) setSelection({ gridId, sessionId });
      },
      split: (targetId, sessionId, edge) => {
        if (storageKey === null || isTempConvId(sessionId)) return;
        const layout = activeGrid?.layout ?? conversationId;
        if (!layout) return;
        const next = splitGrid(layout, targetId, sessionId, edge);
        if (!next) return;
        const grid = { ...activeGrid, id: activeGrid?.id ?? randomUUID(), layout: next };
        const apply = () => {
          save(
            activeGrid
              ? grids.map((entry) => (entry.id === grid.id ? grid : entry))
              : [...grids, grid],
          );
          if (!activeGrid) navigate(`/grid/${grid.id}`);
        };
        if (activeGrid) apply();
        else runWithLeaveGuards(apply);
      },
      closePane: (sessionId) => {
        if (!activeGrid || !gridSessionIds(activeGrid.layout).includes(sessionId)) return;
        const layout = removeGridSession(activeGrid.layout, sessionId);
        if (layout === null || typeof layout === "string") {
          runWithLeaveGuards(() => {
            save(grids.filter((grid) => grid.id !== activeGrid.id));
            navigate(layout ? `/c/${layout}` : "/");
          });
        } else {
          runWithLeaveGuards(() => {
            save(grids.map((grid) => (grid.id === activeGrid.id ? { ...grid, layout } : grid)));
          }, [sessionId]);
        }
      },
      deleteGrid,
      swapPanes: (firstId, secondId, edge) => {
        if (!activeGrid) return;
        const layout = swapGridSessions(activeGrid.layout, firstId, secondId, edge);
        if (layout !== activeGrid.layout)
          runWithLeaveGuards(() => {
            save(grids.map((grid) => (grid.id === activeGrid.id ? { ...grid, layout } : grid)));
          }, [firstId, secondId]);
      },
      renameGrid: (id, name) => {
        const trimmed = name.trim().slice(0, USER_SESSION_TITLE_MAX_CHARS);
        if (trimmed)
          save(grids.map((grid) => (grid.id === id ? { ...grid, name: trimmed } : grid)));
      },
    };
  }, [
    grids,
    activeGrid,
    conversationId,
    selectedSessionId,
    gridId,
    navigate,
    storageKey,
    identityReady,
    desktop,
  ]);
  return <GridSessionsContext.Provider value={value}>{children}</GridSessionsContext.Provider>;
}

/** The grid's name, or its sessions' titles in pane order. */
export function gridTitle(
  grid: SessionGrid,
  sessionTitles: ReadonlyMap<string, string | null | undefined>,
): string {
  return (
    grid.name ||
    gridSessionIds(grid.layout)
      .map((id) => sessionTitles.get(id) || "Session")
      .join(" + ")
  );
}
