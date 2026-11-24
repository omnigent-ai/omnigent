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
  // Removes the window's listeners that follow a pointer whose capture ended before its release.
  const unfollow = useRef<(() => void) | null>(null);
  // Removes the window's blur listener, which drops the drag from its press to its end.
  const unwatchBlur = useRef<(() => void) | null>(null);
  // The latest callbacks, for those listeners, which outlive the render that added them.
  const latest = useRef({ onMove, onCommit, onAbort });
  latest.current = { onMove, onCommit, onAbort };
  const finishDrag = () => {
    held.current = null;
    unfollow.current?.();
    unfollow.current = null;
    unwatchBlur.current?.();
    unwatchBlur.current = null;
    endDrag.current?.();
    endDrag.current = null;
  };
  const lose = (reason: PointerDragEnd) => {
    finishDrag();
    latest.current.onAbort(reason);
  };
  // The window losing focus drops the drag whether or not the handle still holds the pointer.
  const watchBlur = (element: HTMLElement, pointerId: number) => {
    const view = element.ownerDocument.defaultView;
    const onBlur = () => {
      if (element.hasPointerCapture?.(pointerId)) element.releasePointerCapture?.(pointerId);
      lose("pointer-lost");
    };
    view?.addEventListener("blur", onBlur);
    unwatchBlur.current = () => view?.removeEventListener("blur", onBlur);
  };
  const moveTo = (event: { clientX: number; clientY: number }) => {
    if (!containerRef.current) return;
    const bounds = containerRef.current.getBoundingClientRect();
    if (bounds.width === 0 || bounds.height === 0) return;
    latest.current.onMove({
      left: ((event.clientX - bounds.left) / bounds.width) * 100,
      top: ((event.clientY - bounds.top) / bounds.height) * 100,
    });
  };
  // The macOS app can drop the handle's capture just before the release, which then lands on
  // whatever is under the pointer. The drag follows its pointer through the document instead,
  // until its release, a cancel, or a move with the button already up.
  const follow = (element: HTMLElement, pointerId: number) => {
    const doc = element.ownerDocument;
    const ours = (event: globalThis.PointerEvent) => event.pointerId === pointerId;
    const onMoveEvent = (event: globalThis.PointerEvent) => {
      if (!ours(event)) return;
      if ((event.buttons & 1) === 0) lose("pointer-lost");
      else moveTo(event);
    };
    const onUpEvent = (event: globalThis.PointerEvent) => {
      if (!ours(event)) return;
      finishDrag();
      latest.current.onCommit();
    };
    const onCancelEvent = (event: globalThis.PointerEvent) => {
      if (ours(event)) lose("pointer-cancelled");
    };
    doc.addEventListener("pointermove", onMoveEvent, true);
    doc.addEventListener("pointerup", onUpEvent, true);
    doc.addEventListener("pointercancel", onCancelEvent, true);
    unfollow.current = () => {
      doc.removeEventListener("pointermove", onMoveEvent, true);
      doc.removeEventListener("pointerup", onUpEvent, true);
      doc.removeEventListener("pointercancel", onCancelEvent, true);
    };
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
      watchBlur(event.currentTarget, event.pointerId);
      endDrag.current = paneDividerDrag.begin();
      onStart();
    },
    onPointerMove: (event: PointerEvent<HTMLElement>) => {
      if (event.currentTarget.hasPointerCapture(event.pointerId)) moveTo(event);
    },
    onPointerUp: end(onCommit),
    // After a release the drag is already over, so these drop nothing more.
    onPointerCancel: end(() => onAbort("pointer-cancelled")),
    onLostPointerCapture: (event: PointerEvent<HTMLElement>) => {
      const pointer = held.current;
      if (pointer === null || pointer.pointerId !== event.pointerId || unfollow.current) return;
      follow(pointer.element, pointer.pointerId);
    },
  };
}
