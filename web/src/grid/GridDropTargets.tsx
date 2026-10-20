import { createPortal } from "react-dom";
import { useDndContext, useDroppable } from "@dnd-kit/core";
import type { CSSProperties } from "react";
import { useGridSessions } from "./GridSessionsContext";
import { splitGrid, type SplitEdge } from "./gridLayout";

export const SPLIT_EDGES: SplitEdge[] = ["left", "right", "top", "bottom"];

export interface GridDropTarget {
  type: "session-grid";
  targetId: string;
  edge: SplitEdge;
}

const half: Record<SplitEdge, CSSProperties> = {
  left: { left: 0, top: 0, width: "50%", height: "100%" },
  right: { right: 0, top: 0, width: "50%", height: "100%" },
  top: { left: 0, top: 0, width: "100%", height: "50%" },
  bottom: { left: 0, bottom: 0, width: "100%", height: "50%" },
};

function DropZone({
  targetId,
  edge,
  fourEdges,
}: {
  targetId: string;
  edge: SplitEdge;
  fourEdges: boolean;
}) {
  const data: GridDropTarget = { type: "session-grid", targetId, edge };
  const { setNodeRef } = useDroppable({ id: `grid:${targetId}:${edge}`, data });
  const style = fourEdges
    ? edge === "left" || edge === "right"
      ? { ...half[edge], width: "25%" }
      : { ...half[edge], left: "25%", width: "50%" }
    : half[edge];
  return (
    <div
      ref={setNodeRef}
      style={style}
      className="pointer-events-auto absolute flex items-center justify-center"
      data-grid-drop-edge={edge}
    />
  );
}

/** Portals keep sidebar drags and main-view targets in the same dnd-kit context. */
export function GridDropTargets({ sessionId }: { sessionId: string }) {
  const grids = useGridSessions();
  const { over } = useDndContext();
  const layout = grids?.activeGrid?.layout ?? grids?.conversationId;
  if (!grids?.enabled || !layout) return null;
  const hovered = over?.data.current as GridDropTarget | undefined;
  return [...document.querySelectorAll<HTMLElement>("[data-grid-session]")].map((pane) => {
    const targetId = pane.dataset.gridSession!;
    const edges = SPLIT_EDGES.filter(
      (edge) => splitGrid(layout, targetId, sessionId, edge) !== null,
    );
    if (!edges.length) return null;
    return createPortal(
      <div
        className="pointer-events-none absolute inset-0 z-50 rounded-lg bg-background/20"
        data-testid="grid-drop-targets"
      >
        {hovered?.type === "session-grid" && hovered.targetId === targetId && (
          <div
            style={half[hovered.edge]}
            className="absolute rounded-lg border-2 border-dashed border-brand-accent bg-brand-accent/25"
            data-testid="grid-drop-ghost"
          />
        )}
        {edges.map((edge) => (
          <DropZone key={edge} targetId={targetId} edge={edge} fourEdges={edges.length === 4} />
        ))}
      </div>,
      pane,
      targetId,
    );
  });
}
