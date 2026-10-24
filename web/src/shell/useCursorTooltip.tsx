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
  const [open, setOpen] = useState(false);
  const pointerRef = useRef<Point | null>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const positionRef = useRef<(() => void) | null>(null);
  const frameRef = useRef<number | null>(null);

  // Measure before the first paint, then only when the text or dimensions change.
  // Pointer frames reuse that size and move the box without rendering the row.
  useLayoutEffect(() => {
    const node = tooltipRef.current;
    if (!node) return;
    let size: Size;
    const position = () => {
      if (!pointerRef.current) return;
      const { left, top } = placeTooltip(pointerRef.current, size);
      node.style.transform = `translate(${left}px, ${top}px)`;
    };
    const measure = () => {
      node.style.maxWidth = `${document.documentElement.clientWidth - 2 * EDGE_MARGIN}px`;
      size = node.getBoundingClientRect();
      position();
    };
    positionRef.current = position;
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    window.addEventListener("resize", measure);
    return () => {
      positionRef.current = null;
      observer.disconnect();
      window.removeEventListener("resize", measure);
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    };
  }, [open, text]);

  const handlers = {
    onMouseMove: (e: React.MouseEvent) => {
      if (!pointerRef.current) setOpen(true);
      pointerRef.current = { x: e.clientX, y: e.clientY };
      if (positionRef.current && frameRef.current === null) {
        frameRef.current = requestAnimationFrame(() => {
          frameRef.current = null;
          positionRef.current?.();
        });
      }
    },
    onMouseLeave: () => {
      pointerRef.current = null;
      setOpen(false);
    },
  };

  const tooltip = open
    ? createPortal(
        <div
          ref={tooltipRef}
          style={{
            position: "fixed",
            left: 0,
            top: 0,
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
