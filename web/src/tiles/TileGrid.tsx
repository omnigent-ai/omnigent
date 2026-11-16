// The Gallery and Cards grids: a measured column count, and Gallery cells that
// span as many slots as the session's saved size.

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import {
  effectiveColumns,
  TILE_GAP_PX,
  TILE_ROW_HEIGHT_PX,
  useTileSpan,
  type TileSpan,
} from "./tileLayout";

export interface TileResize {
  span: TileSpan;
  slotWidthPx: number;
  visibleColumns: number;
}

/** Watches an element's content width, for the column math. */
export function useElementWidth<T extends HTMLElement>(): [(node: T | null) => void, number] {
  const [width, setWidth] = useState(0);
  const observer = useRef<ResizeObserver | null>(null);
  const ref = useCallback((node: T | null) => {
    observer.current?.disconnect();
    if (!node) return;
    observer.current = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.current.observe(node);
  }, []);
  return [ref, width];
}

function GalleryCell({
  sessionId,
  visibleColumns,
  slotWidthPx,
  render,
}: {
  sessionId: string;
  visibleColumns: number;
  slotWidthPx: number;
  render: (resize: TileResize) => ReactNode;
}) {
  const span = useTileSpan(sessionId);
  const columns = Math.min(span.columns, visibleColumns);
  const resize = useMemo(
    () => ({ span: { columns, rows: span.rows }, slotWidthPx, visibleColumns }),
    [columns, span.rows, slotWidthPx, visibleColumns],
  );
  return (
    <div
      className="flex min-h-0 min-w-0"
      style={{ gridColumn: `span ${columns}`, gridRow: `span ${span.rows}` }}
    >
      {render(resize)}
    </div>
  );
}

interface TileGridProps {
  ids: readonly string[];
  /** Chosen column count; 0 = Auto. */
  chosenColumns: number;
  /** Gallery cells are fixed-height rows that tiles can span; Cards size to content. */
  layout: "gallery" | "cards";
  onMeasure: (widthPx: number) => void;
  renderTile: (id: string, resize?: TileResize) => ReactNode;
}

export function TileGrid({ ids, chosenColumns, layout, onMeasure, renderTile }: TileGridProps) {
  const [measureRef, width] = useElementWidth<HTMLDivElement>();
  const visibleColumns = width > 0 ? effectiveColumns(chosenColumns, width) : 1;
  const slotWidthPx = width > 0 ? (width - TILE_GAP_PX * (visibleColumns - 1)) / visibleColumns : 0;
  useEffect(() => onMeasure(width), [width, onMeasure]);
  const gallery = layout === "gallery";

  return (
    <div className={cn("min-h-0 flex-1 overflow-y-auto", gallery ? "p-3" : "p-4")}>
      <div
        ref={measureRef}
        className={cn("grid gap-3", !gallery && "mx-auto max-w-[1400px]")}
        style={{
          gridTemplateColumns: `repeat(${visibleColumns}, minmax(0, 1fr))`,
          ...(gallery && {
            gridAutoRows: `${TILE_ROW_HEIGHT_PX}px`,
            // Big tiles leave no holes: smaller ones back-fill the gaps.
            gridAutoFlow: "row dense",
          }),
        }}
      >
        {ids.map((id) =>
          gallery ? (
            <GalleryCell
              key={id}
              sessionId={id}
              visibleColumns={visibleColumns}
              slotWidthPx={slotWidthPx}
              render={(resize) => renderTile(id, resize)}
            />
          ) : (
            <div key={id} className="flex min-h-[120px]">
              {renderTile(id)}
            </div>
          ),
        )}
      </div>
    </div>
  );
}
