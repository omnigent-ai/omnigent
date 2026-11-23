// A pointer drag on a split's divider or corner: it holds the pointer from press to release,
// reports the pointer as percent of the panes' view, and marks the window's divider drag.

import { type PointerEvent, type RefObject, useEffect, useRef } from "react";

import { paneDividerDrag } from "@/shell/paneDividerDrag";
import type { PointerDragEnd } from "@/shell/paneDividerDragEnd";

/** A point as percent of the panes' view, as the layout geometry places dividers. */
export interface ViewPoint {
  left: number;
  top: number;
}

export function usePaneDividerPointer({
  containerRef,
  dragging,
  onStart,
  onMove,
  onCommit,
  onAbort,
}: {
  containerRef: RefObject<HTMLElement | null>;
  /** Whether the layout still has this handle's drag; once it drops it, the pointer goes too. */
  dragging: boolean;
  onStart: () => void;
  onMove: (point: ViewPoint) => void;
  /** The pointer was released: the drag's sizes are saved. */
  onCommit: () => void;
  /** The pointer was taken away mid-drag: its sizes are dropped. */
  onAbort: (reason: PointerDragEnd) => void;
}) {
  // The pointer this handle holds, and the call that ends the window's drag marker, once.
  const held = useRef<{ element: HTMLElement; pointerId: number } | null>(null);
  const endDrag = useRef<(() => void) | null>(null);
  const finishDrag = () => {
    held.current = null;
    endDrag.current?.();
    endDrag.current = null;
  };
  useEffect(() => finishDrag, []);
  useEffect(() => {
    const pointer = held.current;
    if (dragging || pointer === null) return;
    // The layout ended the drag, as when a pane was maximized: let the pointer go.
    finishDrag();
    if (pointer.element.hasPointerCapture?.(pointer.pointerId)) {
      pointer.element.releasePointerCapture?.(pointer.pointerId);
    }
  }, [dragging]);
  // Only the handle holding the pointer ends a drag: a release's later lostpointercapture, or
  // another finger's events, find none held and do nothing.
  const end =
    (outcome: () => void) =>
    (event: PointerEvent<HTMLElement>): void => {
      const pointer = held.current;
      if (pointer === null || pointer.pointerId !== event.pointerId) return;
      finishDrag();
      outcome();
    };
  return {
    onPointerDown: (event: PointerEvent<HTMLElement>) => {
      // Only a primary press starts a drag, and a drag stays its own pointer's until that ends it.
      if (event.button !== 0 || held.current !== null) return;
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      finishDrag();
      held.current = { element: event.currentTarget, pointerId: event.pointerId };
      endDrag.current = paneDividerDrag.begin();
      onStart();
    },
    onPointerMove: (event: PointerEvent<HTMLElement>) => {
      if (!event.currentTarget.hasPointerCapture(event.pointerId) || !containerRef.current) return;
      const bounds = containerRef.current.getBoundingClientRect();
      if (bounds.width === 0 || bounds.height === 0) return;
      onMove({
        left: ((event.clientX - bounds.left) / bounds.width) * 100,
        top: ((event.clientY - bounds.top) / bounds.height) * 100,
      });
    },
    onPointerUp: end(onCommit),
    // After a release the drag is already over, so these drop nothing more.
    onPointerCancel: end(() => onAbort("pointer-cancelled")),
    onLostPointerCapture: end(() => onAbort("pointer-lost")),
  };
}
