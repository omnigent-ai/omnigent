import { type RefObject, useState } from "react";

import type { CornerCursor, DividerCorner } from "./paneDividerCorners";
import type { PointerDragEnd } from "./paneDividerDragEnd";
import { usePaneDividerPointer, type ViewPoint } from "./usePaneDividerPointer";

/**
 * Where a vertical and a horizontal divider meet: dragging there moves both at once. It's a
 * pointer shortcut over the dividers, which keep the keyboard and are what assistive tech reads.
 * Each pane corner there has its own part of the handle with that corner's diagonal cursor.
 */
export function TerminalSplitCorner({
  corner,
  containerRef,
  dragging,
  onStart,
  onDrag,
  onCommit,
  onAbort,
  inGap = false,
}: {
  corner: DividerCorner;
  /** Sits in the gap between cards, where the dividers draw no line: it draws no dot either. */
  inGap?: boolean;
  containerRef: RefObject<HTMLElement | null>;
  /** The drag took this corner: it stays the one dragged, whatever meets there, until release. */
  onStart: (corner: DividerCorner) => void;
  onDrag: (corner: DividerCorner, point: ViewPoint) => void;
  onCommit: () => void;
  onAbort: (reason: PointerDragEnd) => void;
  /** Whether the layout still has this corner's drag. */
  dragging: boolean;
}) {
  // The diagonal pressed, kept for the whole drag so the cursor doesn't flip between regions.
  const [pressed, setPressed] = useState<CornerCursor | null>(null);
  const pointer = usePaneDividerPointer({
    containerRef,
    dragging,
    onStart: () => onStart(corner),
    onMove: (point) => onDrag(corner, point),
    onCommit,
    onAbort,
  });
  const held = dragging ? pressed : null;
  return (
    <div
      aria-hidden
      data-testid="terminal-split-corner"
      {...pointer}
      onPointerDown={(event) => {
        const region = (event.target as HTMLElement).closest<HTMLElement>("[data-cursor]");
        setPressed((region?.dataset.cursor as CornerCursor | undefined) ?? null);
        pointer.onPointerDown(event);
      }}
      // A 13px square over the dividers' 7px hit areas where they cross.
      style={{
        left: `calc(${corner.left}% - 6px)`,
        top: `calc(${corner.top}% - 6px)`,
        width: 13,
        height: 13,
        cursor: held ?? undefined,
      }}
      className="group absolute z-20 flex touch-none items-center justify-center"
    >
      {corner.regions.map((region) => (
        <div
          key={region.quadrant}
          data-testid="terminal-split-corner-region"
          data-quadrant={region.quadrant}
          data-cursor={region.cursor}
          className="absolute"
          style={{
            left: `${region.left}%`,
            top: `${region.top}%`,
            width: `${region.width}%`,
            height: `${region.height}%`,
            cursor: held ?? region.cursor,
          }}
        />
      ))}
      {inGap ? null : (
        <div
          data-testid="terminal-split-corner-dot"
          className="pointer-events-none relative size-[5px] rounded-full bg-transparent group-hover:bg-ring"
        />
      )}
    </div>
  );
}
