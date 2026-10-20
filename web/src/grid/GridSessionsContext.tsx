import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { toast } from "sonner";
import { useViewerId } from "@/hooks/useViewerId";
import { getOmnigentServerIdentity } from "@/lib/host";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { randomUUID } from "@/lib/randomUUID";
import { useNavigate, useParams } from "@/lib/routing";
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
  grids: SessionGrid[];
  activeGrid?: SessionGrid;
  conversationId?: string;
  split: (targetId: string, sessionId: string, edge: SplitEdge) => void;
  closePane: (sessionId: string) => void;
  deleteGrid: (id: string) => void;
  renameGrid: (id: string, name: string) => void;
  swapPanes: (firstId: string, secondId: string) => void;
}

const GridSessionsContext = createContext<GridSessionsContextValue | null>(null);
const EMPTY_GRIDS: SessionGrid[] = [];
export const useGridSessions = () => useContext(GridSessionsContext);

export function GridSessionsProvider({ children }: { children: ReactNode }) {
  const enabled = isFeatureEnabled(useServerInfo(), "grid_sessions_display_ui");
  return enabled ? <EnabledGridSessionsProvider>{children}</EnabledGridSessionsProvider> : children;
}

function EnabledGridSessionsProvider({ children }: { children: ReactNode }) {
  const viewerId = useViewerId();
  const { conversationId: routeConversationId, gridId } = useParams<{
    conversationId: string;
    gridId: string;
  }>();
  const conversationId = isTempConvId(routeConversationId) ? undefined : routeConversationId;
  const navigate = useNavigate();
  const storageKey = `omnigent:session-grids:${getOmnigentServerIdentity() ?? "default"}:${viewerId ?? "anonymous"}`;
  const read = useCallback(() => {
    try {
      return parseSessionGrids(localStorage.getItem(storageKey));
    } catch {
      return [];
    }
  }, [storageKey]);
  const [saved, setSaved] = useState(() => ({ key: storageKey, grids: read() }));
  if (saved.key !== storageKey) setSaved({ key: storageKey, grids: read() });
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== null && event.key !== storageKey) return;
      if (event.storageArea !== localStorage) return;
      setSaved({ key: storageKey, grids: read() });
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [storageKey, read]);
  const grids = saved.key === storageKey ? saved.grids : EMPTY_GRIDS;
  const activeGrid = grids.find((grid) => grid.id === gridId);
  const value = useMemo<GridSessionsContextValue>(() => {
    const save = (next: SessionGrid[]) => {
      setSaved({ key: storageKey, grids: next });
      try {
        localStorage.setItem(storageKey, JSON.stringify(next));
      } catch {
        toast.warning("Grid changes could not be saved on this device.");
      }
    };
    const deleteGrid = (id: string) => {
      save(grids.filter((grid) => grid.id !== id));
      if (id === gridId) navigate("/");
    };
    return {
      enabled: true,
      grids,
      activeGrid,
      conversationId,
      split: (targetId, sessionId, edge) => {
        if (isTempConvId(sessionId)) return;
        const layout = activeGrid?.layout ?? conversationId;
        if (!layout) return;
        const next = splitGrid(layout, targetId, sessionId, edge);
        if (!next) return;
        const grid = { ...activeGrid, id: activeGrid?.id ?? randomUUID(), layout: next };
        save(
          activeGrid
            ? grids.map((entry) => (entry.id === grid.id ? grid : entry))
            : [...grids, grid],
        );
        if (!activeGrid) navigate(`/grid/${grid.id}`);
      },
      closePane: (sessionId) => {
        if (!activeGrid) return;
        const layout = removeGridSession(activeGrid.layout, sessionId);
        if (layout === null || typeof layout === "string") {
          save(grids.filter((grid) => grid.id !== activeGrid.id));
          navigate(layout ? `/c/${layout}` : "/");
        } else {
          save(grids.map((grid) => (grid.id === activeGrid.id ? { ...grid, layout } : grid)));
        }
      },
      deleteGrid,
      swapPanes: (firstId, secondId) => {
        if (!activeGrid) return;
        const layout = swapGridSessions(activeGrid.layout, firstId, secondId);
        if (layout !== activeGrid.layout)
          save(grids.map((grid) => (grid.id === activeGrid.id ? { ...grid, layout } : grid)));
      },
      renameGrid: (id, name) => {
        const trimmed = name.trim().slice(0, USER_SESSION_TITLE_MAX_CHARS);
        if (trimmed)
          save(grids.map((grid) => (grid.id === id ? { ...grid, name: trimmed } : grid)));
      },
    };
  }, [grids, activeGrid, conversationId, gridId, navigate, storageKey]);
  return <GridSessionsContext.Provider value={value}>{children}</GridSessionsContext.Provider>;
}

export function gridTitle(grid: SessionGrid): string {
  return grid.name || `Grid · ${gridSessionIds(grid.layout).length} sessions`;
}
