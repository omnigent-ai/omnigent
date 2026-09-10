import {
  DndContext,
  DragOverlay,
  type DragEndEvent,
  type DragStartEvent,
  MeasuringStrategy,
  MouseSensor,
  TouchSensor,
  pointerWithin,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import { createPortal } from "react-dom";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useNavigate } from "@/lib/routing";
import { getEmbedRoot } from "@/lib/host";
import { useWorkspaceLayoutStore, type WorkspaceDropEdge } from "@/store/workspaceLayout";
import type { SidebarDropTarget } from "./sidebarNav";

export interface SessionDragState {
  id: string;
  label: string;
  project: string | null;
  isPinned: boolean;
}

export interface WorkspacePaneDropTarget {
  type: "workspace-pane";
  paneId: string;
  edge: WorkspaceDropEdge | "center";
}

type SidebarDropHandler = (drag: SessionDragState, target: SidebarDropTarget) => void;

interface SessionDragDropContextValue {
  activeDrag: SessionDragState | null;
  provided: boolean;
  registerSidebarDropHandler: (handler: SidebarDropHandler) => () => void;
}

const SessionDragDropContext = createContext<SessionDragDropContextValue>({
  activeDrag: null,
  provided: false,
  registerSidebarDropHandler: () => () => {},
});

export function useSessionDragDrop(): SessionDragDropContextValue {
  return useContext(SessionDragDropContext);
}

export function SessionDragDropBoundary({ children }: { children: ReactNode }) {
  const { provided } = useSessionDragDrop();
  return provided ? children : <SessionDragDropProvider>{children}</SessionDragDropProvider>;
}

function dragStateFromEvent(event: DragStartEvent): SessionDragState {
  const data = event.active.data.current as
    { label?: string; project?: string | null; isPinned?: boolean } | undefined;
  return {
    id: String(event.active.id),
    label: data?.label ?? String(event.active.id),
    project: data?.project ?? null,
    isPinned: data?.isPinned ?? false,
  };
}

function isWorkspaceDropTarget(value: unknown): value is WorkspacePaneDropTarget {
  if (!value || typeof value !== "object") return false;
  const target = value as Partial<WorkspacePaneDropTarget>;
  return (
    target.type === "workspace-pane" &&
    typeof target.paneId === "string" &&
    (target.edge === "left" ||
      target.edge === "right" ||
      target.edge === "top" ||
      target.edge === "bottom" ||
      target.edge === "center")
  );
}

export function SessionDragDropProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [activeDrag, setActiveDrag] = useState<SessionDragState | null>(null);
  const activeDragRef = useRef<SessionDragState | null>(null);
  const sidebarDropHandlerRef = useRef<SidebarDropHandler | null>(null);
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
  );

  const registerSidebarDropHandler = useCallback((handler: SidebarDropHandler) => {
    sidebarDropHandlerRef.current = handler;
    return () => {
      if (sidebarDropHandlerRef.current === handler) sidebarDropHandlerRef.current = null;
    };
  }, []);

  const handleDragStart = useCallback((event: DragStartEvent) => {
    const drag = dragStateFromEvent(event);
    activeDragRef.current = drag;
    setActiveDrag(drag);
  }, []);

  const clearDrag = useCallback(() => {
    activeDragRef.current = null;
    setActiveDrag(null);
  }, []);

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const drag = activeDragRef.current;
      clearDrag();
      if (!drag) return;

      const target = event.over?.data.current;
      if (isWorkspaceDropTarget(target)) {
        // Center has no tabs to stack (unlike the SP2K reference), so it moves
        // the dragged session into the pane; edges split as before.
        if (target.edge === "center") {
          useWorkspaceLayoutStore.getState().setLeafSession(target.paneId, drag.id);
        } else {
          useWorkspaceLayoutStore.getState().splitPane(target.paneId, drag.id, target.edge);
        }
        navigate(`/c/${drag.id}`);
        return;
      }
      sidebarDropHandlerRef.current?.(drag, (target as SidebarDropTarget | undefined) ?? null);
    },
    [clearDrag, navigate],
  );

  const value = useMemo(
    () => ({ activeDrag, provided: true, registerSidebarDropHandler }),
    [activeDrag, registerSidebarDropHandler],
  );

  return (
    <SessionDragDropContext.Provider value={value}>
      <DndContext
        sensors={sensors}
        collisionDetection={pointerWithin}
        measuring={{ droppable: { strategy: MeasuringStrategy.Always } }}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
        onDragCancel={clearDrag}
      >
        {children}
        {createPortal(
          <DragOverlay dropAnimation={null}>
            {activeDrag ? (
              <div className="pointer-events-none max-w-[16rem] truncate rounded-md border bg-card-solid px-3 py-2 text-ui shadow-tooltip">
                {activeDrag.label}
              </div>
            ) : null}
          </DragOverlay>,
          getEmbedRoot() ?? document.body,
        )}
      </DndContext>
    </SessionDragDropContext.Provider>
  );
}
