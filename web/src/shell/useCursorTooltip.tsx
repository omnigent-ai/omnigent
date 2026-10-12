import { useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { getEmbedRoot } from "@/lib/host";

// Clearance kept from the viewport edges, and between the pointer and the box.
const EDGE_MARGIN = 8;
const OFFSET_BELOW = 14;
const OFFSET_ABOVE = 8;

interface Point {
  x: number;
  y: number;
}
interface Size {
  width: number;
  height: number;
}

// Below-right of the pointer by default; flips above / left when that would
// cross the bottom / right edge, and never starts past the top / left edge.
function placeTooltip(pointer: Point, size: Size) {
  // The root's client box is the viewport minus any classic scrollbar.
  const { clientWidth, clientHeight } = document.documentElement;
  let left = pointer.x;
  if (left + size.width > clientWidth - EDGE_MARGIN) left = pointer.x - size.width;
  let top = pointer.y + OFFSET_BELOW;
  if (top + size.height > clientHeight - EDGE_MARGIN) top = pointer.y - OFFSET_ABOVE - size.height;
  return {
    left: Math.max(EDGE_MARGIN, left),
    top: Math.max(EDGE_MARGIN, top),
    maxWidth: clientWidth - 2 * EDGE_MARGIN,
  };
}

/**
 * Returns mouse event handlers for a trigger element and a fixed-positioned
 * tooltip node that follows the cursor. The node is portalled to the embed root
 * (or `document.body` standalone), so a transformed ancestor such as a
 * virtualized row can't become its containing block.
 */
export function useCursorTooltip(text: string): {
  handlers: {
    onMouseMove: (e: React.MouseEvent) => void;
    onMouseLeave: () => void;
  };
  tooltip: React.ReactNode;
} {
  const [cursorPos, setCursorPos] = useState<Point | null>(null);
  const [size, setSize] = useState<Size>({ width: 0, height: 0 });
  const tooltipRef = useRef<HTMLDivElement>(null);

  // Measure before paint on every pointer move or text change while shown, so an
  // edge flip never shows at the unflipped spot and follows a re-wrap (resize,
  // zoom, font load) on the next move.
  useLayoutEffect(() => {
    if (!tooltipRef.current) return;
    const { width, height } = tooltipRef.current.getBoundingClientRect();
    setSize((prev) => (prev.width === width && prev.height === height ? prev : { width, height }));
  }, [cursorPos, text]);

  const handlers = {
    onMouseMove: (e: React.MouseEvent) => setCursorPos({ x: e.clientX, y: e.clientY }),
    onMouseLeave: () => setCursorPos(null),
  };

  const tooltip = cursorPos
    ? createPortal(
        <div
          ref={tooltipRef}
          style={{
            position: "fixed",
            ...placeTooltip(cursorPos, size),
            pointerEvents: "none",
          }}
          className="z-50 inline-flex w-max items-center wrap-anywhere rounded-md border border-border bg-popover px-3 py-1.5 text-sm text-popover-foreground shadow-tooltip"
        >
          {text}
        </div>,
        getEmbedRoot() ?? document.body,
      )
    : null;

  return { handlers, tooltip };
}
