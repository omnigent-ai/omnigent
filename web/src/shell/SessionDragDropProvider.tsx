import {
  closestCenter,
  DndContext,
  DragOverlay,
  type DragEndEvent,
  type DragOverEvent,
  type DragStartEvent,
  KeyboardSensor,
  MeasuringStrategy,
  MouseSensor,
  TouchSensor,
  pointerWithin,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import { sortableKeyboardCoordinates } from "@dnd-kit/sortable";
import { createPortal } from "react-dom";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
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
type ProjectOrderDropHandler = (from: string, to: string) => void;

interface SessionDragDropContextValue {
  activeDrag: SessionDragState | null;
  draggedProject: string | null;
  overProject: string | null;
  provided: boolean;
  registerProjectOrderDropHandler: (handler: ProjectOrderDropHandler) => () => void;
  registerSidebarDropHandler: (handler: SidebarDropHandler) => () => void;
}

const SessionDragDropContext = createContext<SessionDragDropContextValue>({
  activeDrag: null,
  draggedProject: null,
  overProject: null,
  provided: false,
  registerProjectOrderDropHandler: () => () => {},
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

function projectNameFromData(value: unknown): string | null {
  if (!value || typeof value !== "object") return null;
  const data = value as { type?: unknown; name?: unknown };
  return data.type === "project-order" && typeof data.name === "string" ? data.name : null;
}

function dragOriginFromEvent(event: DragStartEvent): CSSProperties | undefined {
  const target = event.activatorEvent?.target;
  if (!(target instanceof Element)) return undefined;
  const selector = projectNameFromData(event.active.data.current)
    ? "[data-project-order-name]"
    : "[data-sidebar-session-id]";
  const rect = target.closest(selector)?.getBoundingClientRect();
  return rect ? { left: rect.left, top: rect.top, width: rect.width } : undefined;
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
  const [draggedProject, setDraggedProject] = useState<string | null>(null);
  const [overProject, setOverProject] = useState<string | null>(null);
  const [dragOrigin, setDragOrigin] = useState<CSSProperties | undefined>(undefined);
  const activeDragRef = useRef<SessionDragState | null>(null);
  const draggedProjectRef = useRef<string | null>(null);
  const projectOrderDropHandlerRef = useRef<ProjectOrderDropHandler | null>(null);
  const sidebarDropHandlerRef = useRef<SidebarDropHandler | null>(null);
  const sensors = useSensors(
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
      keyboardCodes: { start: ["Space"], cancel: ["Escape"], end: ["Space"] },
    }),
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
  );

  const registerProjectOrderDropHandler = useCallback((handler: ProjectOrderDropHandler) => {
    projectOrderDropHandlerRef.current = handler;
    return () => {
      if (projectOrderDropHandlerRef.current === handler) {
        projectOrderDropHandlerRef.current = null;
      }
    };
  }, []);

  const registerSidebarDropHandler = useCallback((handler: SidebarDropHandler) => {
    sidebarDropHandlerRef.current = handler;
    return () => {
      if (sidebarDropHandlerRef.current === handler) sidebarDropHandlerRef.current = null;
    };
  }, []);

  const handleDragStart = useCallback((event: DragStartEvent) => {
    setDragOrigin(dragOriginFromEvent(event));
    const projectName = projectNameFromData(event.active.data.current);
    if (projectName) {
      draggedProjectRef.current = projectName;
      setDraggedProject(projectName);
      setOverProject(null);
      return;
    }
    const drag = dragStateFromEvent(event);
    activeDragRef.current = drag;
    setActiveDrag(drag);
  }, []);

  const clearDrag = useCallback(() => {
    activeDragRef.current = null;
    draggedProjectRef.current = null;
    setActiveDrag(null);
    setDraggedProject(null);
    setOverProject(null);
    setDragOrigin(undefined);
  }, []);

  const handleDragOver = useCallback((event: DragOverEvent) => {
    if (!draggedProjectRef.current) return;
    setOverProject(projectNameFromData(event.over?.data.current));
  }, []);

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const projectName = draggedProjectRef.current;
      if (projectName) {
        const targetProject = projectNameFromData(event.over?.data.current);
        clearDrag();
        if (targetProject && targetProject !== projectName) {
          projectOrderDropHandlerRef.current?.(projectName, targetProject);
        }
        return;
      }
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
    () => ({
      activeDrag,
      draggedProject,
      overProject,
      provided: true,
      registerProjectOrderDropHandler,
      registerSidebarDropHandler,
    }),
    [
      activeDrag,
      draggedProject,
      overProject,
      registerProjectOrderDropHandler,
      registerSidebarDropHandler,
    ],
  );

  return (
    <SessionDragDropContext.Provider value={value}>
      <DndContext
        sensors={sensors}
        collisionDetection={(args) => {
          const ordering = projectNameFromData(args.active.data.current) !== null;
          const droppableContainers = args.droppableContainers.filter(
            (container) => (projectNameFromData(container.data.current) !== null) === ordering,
          );
          if (!ordering) return pointerWithin({ ...args, droppableContainers });
          if (args.pointerCoordinates) {
            const rects = droppableContainers
              .map((container) => args.droppableRects.get(container.id))
              .filter((rect) => rect != null);
            const { x, y } = args.pointerCoordinates;
            if (
              rects.length === 0 ||
              x < Math.min(...rects.map((rect) => rect.left)) ||
              x > Math.max(...rects.map((rect) => rect.right)) ||
              y < Math.min(...rects.map((rect) => rect.top)) - 10 ||
              y > Math.max(...rects.map((rect) => rect.bottom)) + 10
            ) {
              return [];
            }
          }
          return closestCenter({ ...args, droppableContainers });
        }}
        measuring={{ droppable: { strategy: MeasuringStrategy.Always } }}
        onDragStart={handleDragStart}
        onDragOver={handleDragOver}
        onDragEnd={handleDragEnd}
        onDragCancel={clearDrag}
      >
        {children}
        {createPortal(
          <DragOverlay dropAnimation={null} className="pointer-events-none" style={dragOrigin}>
            {activeDrag || draggedProject ? (
              <div
                className="pointer-events-none max-w-[16rem] truncate rounded-md border bg-card-solid px-3 py-2 text-ui shadow-tooltip"
                style={dragOrigin ? { maxWidth: "none" } : undefined}
              >
                {draggedProject ?? activeDrag?.label}
              </div>
            ) : null}
          </DragOverlay>,
          getEmbedRoot() ?? document.body,
        )}
      </DndContext>
    </SessionDragDropContext.Provider>
  );
}
