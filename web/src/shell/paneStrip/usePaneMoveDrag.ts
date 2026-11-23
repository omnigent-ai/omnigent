// Dragging a split's pane by its strip row onto another pane's side, past a few pixels so a click
// still focuses the pane. Any ending but a drop on an offered spot leaves the layout unchanged.

import {
  type PointerEvent as ReactPointerEvent,
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import type { TerminalLayout, TerminalPaneSide, TerminalRect } from "@/lib/paneSplitLayout";
import { terminalLayoutKeys } from "@/lib/paneSplitLayout";
import { type PaneDividerDrag, paneDividerDrag } from "@/shell/paneDividerDrag";
import {
  isPaneMoveDestination,
  type PaneDragOutside,
  type PaneMoveDestination,
  placedPaneArea,
  paneDropSpot,
} from "@/shell/paneMove";

export type { PaneDragOutside } from "@/shell/paneMove";

/** How far the pointer goes, in CSS pixels, before a press on the row is a drag. */
export const PANE_DRAG_THRESHOLD_PX = 4;

/** Where a drag would drop: a spot the pane may go, and the half it would take there. */
export interface PaneDrop {
  targetKey: string;
  side: TerminalPaneSide;
  area: TerminalRect;
}

export interface PaneMoveDrag {
  /** The pane being dragged, once past the threshold. */
  draggingKey: string | null;
  /** The pane held by its row, from the press until the drag ends: it shows lifted meanwhile. */
  heldKey: string | null;
  /** Where it would land now; null over itself, outside, or a spot the size rule refuses. */
  drop: PaneDrop | null;
  /** The row's pointerdown for `key`'s pane; undefined while its panes can't move. */
  pointerDownFor: (key: string) => ((event: ReactPointerEvent<HTMLElement>) => void) | undefined;
}

export interface PaneMoveDragOptions {
  panesRef: RefObject<HTMLElement | null>;
  /** The split as shown, or null while its panes can't move: one pane, or one maximized. */
  layout: TerminalLayout | null;
  /** Where `key`'s pane may go now, as ⌘K's Move Pane… offers it. */
  destinations: (key: string) => readonly PaneMoveDestination[];
  onMove: (move: { key: string; targetKey: string; side: TerminalPaneSide }) => void;
  /** Gives `key`'s pane the keyboard back after a drag that moved nothing. */
  refocus?: (key: string) => void;
  dividerDrag?: PaneDividerDrag;
  /** Where a pane may go off the split, such as an app pane back to the workspace sidebar. */
  outside?: PaneDragOutside;
}

/** A press this soon and this near after a click is a double-click's second press. */
const DOUBLE_CLICK_MS = 500;
const DOUBLE_CLICK_SLOP_PX = 8;

/** A row's controls: a press or click on one is the control's, never the row's. */
export const PANE_STRIP_CONTROL = "button, input, textarea, [data-pane-strip-view-toggle-host]";

/** Whether two drops show the same preview: the same spot, and the same area it would take. */
function sameDrop(a: PaneDrop | null, b: PaneDrop | null): boolean {
  if (a === null || b === null) return a === b;
  return (
    a.targetKey === b.targetKey &&
    a.side === b.side &&
    a.area.left === b.area.left &&
    a.area.top === b.area.top &&
    a.area.width === b.area.width &&
    a.area.height === b.area.height
  );
}

interface Gesture {
  key: string;
  pointerId: number;
  element: HTMLElement;
  startX: number;
  startY: number;
  dragging: boolean;
  /** Ends the frames' pause, once dragging. */
  endPause: (() => void) | null;
  /** The spot the preview shows now, kept until the pointer is clearly in another zone. */
  shown: PaneDrop | null;
  /** Spots offered as the drag began. */
  offered: readonly PaneMoveDestination[];
  /** The listeners this gesture added, removed as it ends. */
  detach: () => void;
}

export function usePaneMoveDrag({
  panesRef,
  layout,
  destinations,
  onMove,
  refocus,
  dividerDrag = paneDividerDrag,
  outside,
}: PaneMoveDragOptions): PaneMoveDrag {
  const [draggingKey, setDraggingKey] = useState<string | null>(null);
  const [heldKey, setHeldKey] = useState<string | null>(null);
  const [drop, setDrop] = useState<PaneDrop | null>(null);
  const gesture = useRef<Gesture | null>(null);
  const lastClick = useRef<{ key: string; at: number; x: number; y: number } | null>(null);
  const latest = useRef({ layout, destinations, onMove, refocus, panesRef, dividerDrag, outside });
  latest.current = { layout, destinations, onMove, refocus, panesRef, dividerDrag, outside };

  /**
   * Ends the gesture; `moved` says whether it ended in a move. A teardown (the pane gone or
   * hidden, the view unmounting) gives no pane the keyboard back.
   */
  const end = useCallback((moved: boolean, teardown = false) => {
    const current = gesture.current;
    if (!current) return;
    gesture.current = null;
    current.detach();
    current.endPause?.();
    if (current.dragging) latest.current.outside?.over(current.key, null);
    if (current.element.hasPointerCapture?.(current.pointerId))
      current.element.releasePointerCapture(current.pointerId);
    setDraggingKey(null);
    setHeldKey(null);
    setDrop(null);
    if (current.dragging && !moved && !teardown) latest.current.refocus?.(current.key);
  }, []);

  /** The spot under the pointer for the dragged pane, if it's one offered. */
  const dropAt = (current: Gesture, clientX: number, clientY: number): PaneDrop | null => {
    const root = latest.current.panesRef.current;
    const shown = latest.current.layout;
    if (!root || !shown) return null;
    const box = root.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) return null;
    const point = {
      x: ((clientX - box.left) / box.width) * 100,
      y: ((clientY - box.top) / box.height) * 100,
    };
    const spot = paneDropSpot(shown, point, current.shown);
    if (!spot || spot.targetKey === current.key) return null;
    if (!isPaneMoveDestination(current.offered, spot.targetKey, spot.side)) return null;
    const area = placedPaneArea(shown, {
      kind: "move",
      key: current.key,
      targetKey: spot.targetKey,
      side: spot.side,
    });
    return area ? { ...spot, area } : null;
  };

  const begin = (key: string, event: ReactPointerEvent<HTMLElement>) => {
    // A Ctrl-click is the Mac's right-click: it opens the row's menu, never a drag.
    if (event.button !== 0 || event.ctrlKey || gesture.current) return;
    if ((event.target as Element).closest(PANE_STRIP_CONTROL)) return;
    // A double-click maximizes; its second press on the same row never drags, however much the
    // hand moves. A press on another pane's row, however near, is its own.
    const click = lastClick.current;
    lastClick.current = null;
    if (
      click?.key === key &&
      performance.now() - click.at < DOUBLE_CLICK_MS &&
      Math.hypot(event.clientX - click.x, event.clientY - click.y) <= DOUBLE_CLICK_SLOP_PX
    )
      return;
    const element = event.currentTarget;
    const onPointerMove = (move: PointerEvent) => {
      const current = gesture.current;
      if (!current || move.pointerId !== current.pointerId) return;
      if (!current.dragging) {
        const distance = Math.hypot(move.clientX - current.startX, move.clientY - current.startY);
        if (distance < PANE_DRAG_THRESHOLD_PX) return;
        current.dragging = true;
        current.offered = latest.current.destinations(current.key);
        current.endPause = latest.current.dividerDrag.begin();
        // A frame with the keyboard would keep Escape from the page meanwhile.
        if (document.activeElement instanceof HTMLIFrameElement) document.activeElement.blur();
        setDraggingKey(current.key);
      }
      const spot = dropAt(current, move.clientX, move.clientY);
      const unchanged = sameDrop(spot, current.shown);
      current.shown = spot;
      // Only a new preview re-renders the panes; a move that shows the same one doesn't.
      if (!unchanged) setDrop(spot);
      latest.current.outside?.over(current.key, spot ? null : { x: move.clientX, y: move.clientY });
    };
    const onPointerUp = (up: PointerEvent) => {
      const current = gesture.current;
      if (!current || up.pointerId !== current.pointerId) return;
      lastClick.current = current.dragging
        ? null
        : { key: current.key, at: performance.now(), x: current.startX, y: current.startY };
      const landed = current.dragging ? dropAt(current, up.clientX, up.clientY) : null;
      // Still offered for the split as it is now, which the move checks again.
      const still =
        landed !== null &&
        isPaneMoveDestination(
          latest.current.destinations(current.key),
          landed.targetKey,
          landed.side,
        );
      const point = { x: up.clientX, y: up.clientY };
      const elsewhere = current.dragging && landed === null && latest.current.outside !== undefined;
      if (elsewhere) {
        // Ended first, so the place it goes sees the drag over.
        const { outside: place } = latest.current;
        end(true);
        if (!place?.drop(current.key, point)) latest.current.refocus?.(current.key);
        return;
      }
      end(still);
      if (landed && still) {
        latest.current.onMove({ key: current.key, targetKey: landed.targetKey, side: landed.side });
      }
    };
    const onCancel = (cancel: PointerEvent) => {
      if (cancel.pointerId === gesture.current?.pointerId) end(false);
    };
    const onKeyDown = (press: KeyboardEvent) => {
      if (press.key !== "Escape" || !gesture.current) return;
      // A held press ends too; only a drag's Escape is the drag's alone.
      if (gesture.current.dragging) {
        press.preventDefault();
        press.stopPropagation();
      }
      end(false);
    };
    // The window going inactive ends it. A frame taking the keyboard, as a press can give it,
    // blurs the window too, but the document keeps focus, so that one is let be.
    const onWindowBlur = () => {
      const held = gesture.current;
      window.setTimeout(() => {
        if (gesture.current === held && !document.hasFocus()) end(false);
      }, 0);
    };
    window.addEventListener("pointermove", onPointerMove, true);
    window.addEventListener("pointerup", onPointerUp, true);
    window.addEventListener("pointercancel", onCancel, true);
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("blur", onWindowBlur);
    document.addEventListener("visibilitychange", onHidden);
    element.addEventListener("lostpointercapture", onCancel);
    function onHidden() {
      if (document.visibilityState === "hidden") end(false);
    }
    gesture.current = {
      key,
      pointerId: event.pointerId,
      element,
      startX: event.clientX,
      startY: event.clientY,
      dragging: false,
      endPause: null,
      shown: null,
      offered: [],
      detach: () => {
        window.removeEventListener("pointermove", onPointerMove, true);
        window.removeEventListener("pointerup", onPointerUp, true);
        window.removeEventListener("pointercancel", onCancel, true);
        window.removeEventListener("keydown", onKeyDown, true);
        window.removeEventListener("blur", onWindowBlur);
        document.removeEventListener("visibilitychange", onHidden);
        element.removeEventListener("lostpointercapture", onCancel);
      },
    };
    setHeldKey(key);
    // The row keeps the pointer over the frames, whose own documents would take it otherwise.
    element.setPointerCapture?.(event.pointerId);
  };

  // The dragged pane closing, or the split it was in going (one pane left, one maximized).
  const keys = layout === null ? null : terminalLayoutKeys(layout).join("\n");
  useEffect(() => {
    const current = gesture.current;
    if (current && (keys === null || !keys.split("\n").includes(current.key))) end(false, true);
  }, [keys, end]);
  // The split changing under a drag, as its sizes or panes change: the preview no longer shows
  // where the pane would land, so the drag ends as on Escape, landing nothing.
  const shape = layout === null ? null : JSON.stringify(layout);
  useEffect(() => {
    if (gesture.current?.dragging) end(false);
  }, [shape, end]);
  useEffect(() => () => end(false, true), [end]);

  return {
    draggingKey,
    heldKey,
    drop,
    pointerDownFor: (key) =>
      layout === null || typeof layout === "string" ? undefined : (event) => begin(key, event),
  };
}
