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
  edgeOnly,
}: {
  targetId: string;
  edge: SplitEdge;
  fourEdges: boolean;
  edgeOnly: boolean;
}) {
  const data: GridDropTarget = { type: "session-grid", targetId, edge };
  const { setNodeRef } = useDroppable({ id: `grid:${targetId}:${edge}`, data });
  let style = half[edge];
  if (edgeOnly) {
    style = { ...style, [edge === "left" || edge === "right" ? "width" : "height"]: "20%" };
  } else if (fourEdges) {
    if (edge === "left" || edge === "right") style = { ...style, width: "25%" };
    else style = { ...style, left: "25%", width: "50%" };
  }
  return (
    <div
      ref={setNodeRef}
      style={style}
      className="pointer-events-auto absolute flex items-center justify-center"
      data-grid-drop-edge={edge}
    />
  );
}

export function GridDropOverlay({
  targetId,
  edges = SPLIT_EDGES,
  edgeOnly = false,
}: {
  targetId: string;
  edges?: SplitEdge[];
  edgeOnly?: boolean;
}) {
  const { over, activatorEvent } = useDndContext();
  // Keyboard rearrangement keeps its whole-cell swap targets.
  if (edgeOnly && activatorEvent instanceof KeyboardEvent) return null;
  const hovered = over?.data.current as GridDropTarget | undefined;
  return (
    <div
      className="pointer-events-none absolute inset-0 z-50 rounded-lg bg-background/20"
      data-testid="grid-drop-targets"
    >
      {hovered?.type === "session-grid" && hovered.targetId === targetId && (
        <div
          style={half[hovered.edge]}
          className="pointer-events-none absolute z-50 rounded-lg border-2 border-dashed border-brand-accent bg-brand-accent/25"
          data-testid="grid-drop-ghost"
        />
      )}
      {edges.map((edge) => (
        <DropZone
          key={edge}
          targetId={targetId}
          edge={edge}
          fourEdges={edges.length === 4}
          edgeOnly={edgeOnly}
        />
      ))}
    </div>
  );
}

/** Portals keep sidebar drags and main-view targets in the same dnd-kit context. */
export function GridDropTargets({ sessionId }: { sessionId: string }) {
  const grids = useGridSessions();
  const layout = grids?.activeGrid?.layout ?? grids?.conversationId;
  if (!grids?.enabled || !layout) return null;
  return [...document.querySelectorAll<HTMLElement>("[data-grid-session]")].map((pane) => {
    const targetId = pane.dataset.gridSession!;
    const edges = SPLIT_EDGES.filter(
      (edge) => splitGrid(layout, targetId, sessionId, edge) !== null,
    );
    return edges.length
      ? createPortal(<GridDropOverlay targetId={targetId} edges={edges} />, pane, targetId)
      : null;
  });
}
