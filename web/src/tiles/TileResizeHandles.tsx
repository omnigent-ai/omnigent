// Drag a tile's right edge, bottom edge or corner to span more or fewer grid
// slots; it snaps to whole slots as you drag. Double-click a handle to reset to 1×1.

import type { PointerEvent } from "react";
import { cn } from "@/lib/utils";
import { DEFAULT_SPAN, setTileSpan, spanFromDrag, type TileSpan } from "./tileLayout";

type Edge = "right" | "bottom" | "corner";

const EDGE_CURSOR: Record<Edge, string> = {
  right: "ew-resize",
  bottom: "ns-resize",
  corner: "nwse-resize",
};

const EDGE_CLASS: Record<Edge, string> = {
  right: "inset-y-2 right-0 w-1.5 cursor-ew-resize",
  bottom: "inset-x-2 bottom-0 h-1.5 cursor-ns-resize",
  corner: "right-0 bottom-0 size-3.5 cursor-nwse-resize",
};

interface TileResizeHandlesProps {
  sessionId: string;
  span: TileSpan;
  slotWidthPx: number;
  visibleColumns: number;
}

export function TileResizeHandles({
  sessionId,
  span,
  slotWidthPx,
  visibleColumns,
}: TileResizeHandlesProps) {
  const startDrag = (edge: Edge) => (event: PointerEvent<HTMLDivElement>) => {
    const tile = event.currentTarget.closest<HTMLElement>("[data-testid='agent-tile']");
    if (!tile || slotWidthPx <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    const start = tile.getBoundingClientRect();
    const originX = event.clientX;
    const originY = event.clientY;
    const move = (pointer: globalThis.PointerEvent) => {
      const next = spanFromDrag(
        start.width,
        start.height,
        edge === "bottom" ? 0 : pointer.clientX - originX,
        edge === "right" ? 0 : pointer.clientY - originY,
        slotWidthPx,
        visibleColumns,
      );
      setTileSpan(sessionId, {
        columns: edge === "bottom" ? span.columns : next.columns,
        rows: edge === "right" ? span.rows : next.rows,
      });
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.body.style.cursor = EDGE_CURSOR[edge];
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  };

  return (
    <>
      {(Object.keys(EDGE_CLASS) as Edge[]).map((edge) => (
        <div
          key={edge}
          role="separator"
          aria-label={`Resize tile (${span.columns}×${span.rows})`}
          title={`${span.columns} wide × ${span.rows} tall. Drag to resize, double-click to reset.`}
          data-tile-no-activate
          onPointerDown={startDrag(edge)}
          onDoubleClick={(event) => {
            event.stopPropagation();
            setTileSpan(sessionId, DEFAULT_SPAN);
          }}
          className={cn(
            "absolute z-10 rounded-sm transition-colors hover:bg-brand-accent/40",
            edge === "corner" && "bg-muted-foreground/20",
            EDGE_CLASS[edge],
          )}
        />
      ))}
    </>
  );
}
