// A rightward swipe across the chat slides the closed mobile drawer open under
// the finger — the mirror of useSidebarSwipeToClose. Unlike the iOS shell's
// native gesture it starts anywhere on the page rather than at the screen edge:
// on Android 10+ (and in mobile Safari) the OS back gesture owns the edges.

import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";

import {
  SWIPE_SETTLE_FRACTION,
  SWIPE_FLICK_VELOCITY,
  DECIDE_SLOP_PX,
  FLICK_MAX_IDLE_MS,
  recordTouchSample,
  startsInHorizontalScroller,
  windowVelocity,
  type TouchSample,
} from "./useSidebarSwipeToClose";

// Touches this close to the left screen edge belong to the OS / shell back or
// edge-swipe gesture, so this gesture never competes with it.
export const EDGE_EXCLUSION_PX = 24;
// Text inputs and terminals handle their own horizontal touch (caret moves,
// selection, terminal scrollback).
const OWN_TOUCH_SELECTOR =
  'input, textarea, select, [contenteditable]:not([contenteditable="false"]), .xterm';

/**
 * True when the touch should be left alone: it is outside `surface`, inside a
 * floating overlay (drawers, popovers, FABs are `position: fixed`), on an
 * element that handles its own touch, or on horizontally scrollable content.
 */
function ignoresTouch(target: Element, surface: Element): boolean {
  if (!surface.contains(target)) return true;
  if (target.closest(OWN_TOUCH_SELECTOR)) return true;
  for (let node: Element | null = target; node && node !== surface; node = node.parentElement) {
    if (getComputedStyle(node).position === "fixed") return true;
  }
  return startsInHorizontalScroller(target, surface);
}

export interface SidebarSwipeToOpenOptions {
  /** Listen only while the drawer is closed in the mobile drawer layout. */
  enabled: boolean;
  /** The (closed) drawer; its width scales the drag. */
  drawerRef: RefObject<HTMLElement | null>;
  /**
   * Selector for the drawer's ancestor a swipe may start inside; overlays
   * within it are excluded.
   */
  surfaceSelector: string;
  /**
   * A touch held this long (ms) before moving belongs to long-press gestures
   * (drag-to-reorder, text selection), not to this swipe.
   */
  holdMs: number;
  /** Live open fraction (0 → 1) while dragging; `null` once released. */
  onDragProgress: (progress: number | null) => void;
  /** Release decision: `true` opens the drawer, `false` slides it back shut. */
  onSettle: (open: boolean) => void;
}

/** Attach the swipe-right-to-open gesture to a closed sidebar drawer. */
export function useSidebarSwipeToOpen({
  enabled,
  drawerRef,
  surfaceSelector,
  holdMs,
  onDragProgress,
  onSettle,
}: SidebarSwipeToOpenOptions): void {
  // Read callbacks through a ref so per-frame re-renders (each progress update
  // sets state) don't re-subscribe the listeners and drop the gesture mid-drag.
  const callbacks = useRef({ onDragProgress, onSettle });
  useLayoutEffect(() => {
    callbacks.current = { onDragProgress, onSettle };
  });

  useEffect(() => {
    if (!enabled) return;

    let tracking = false;
    let dragging = false;
    let startX = 0;
    let startY = 0;
    let startTime = 0;
    let drawerWidth = 1;
    let progress = 0;
    let samples: TouchSample[] = [];

    const reset = () => {
      tracking = false;
      dragging = false;
      samples = [];
    };

    const onTouchStart = (event: TouchEvent) => {
      // A second finger ends the current drag where it is, like a cancel.
      if (dragging) settle(null);
      reset();
      if (event.touches.length !== 1) return;
      const drawer = drawerRef.current;
      const surface = drawer?.closest(surfaceSelector);
      const target = event.target instanceof Element ? event.target : null;
      if (!drawer || !surface || !target) return;
      const touch = event.touches[0];
      if (touch.clientX < EDGE_EXCLUSION_PX || ignoresTouch(target, surface)) return;
      tracking = true;
      startX = touch.clientX;
      startY = touch.clientY;
      startTime = event.timeStamp;
      recordTouchSample(samples, startX, startTime);
      drawerWidth = Math.max(1, drawer.getBoundingClientRect().width);
      progress = 0;
    };

    const onTouchMove = (event: TouchEvent) => {
      if (!tracking) return;
      if (event.touches.length !== 1) {
        settle(null);
        return;
      }
      const touch = event.touches[0];
      const dx = touch.clientX - startX;
      const dy = touch.clientY - startY;
      recordTouchSample(samples, touch.clientX, event.timeStamp);
      if (!dragging) {
        if (Math.abs(dx) < DECIDE_SLOP_PX && Math.abs(dy) < DECIDE_SLOP_PX) return;
        // Vertical scrolls, leftward swipes, and hold-then-drag belong to the
        // transcript, the page, and long-press gestures respectively.
        if (Math.abs(dy) >= Math.abs(dx) || dx < 0 || event.timeStamp - startTime >= holdMs) {
          tracking = false;
          return;
        }
        dragging = true;
      }
      progress = Math.max(0, Math.min(1, dx / drawerWidth));
      callbacks.current.onDragProgress(progress);
      if (event.cancelable) event.preventDefault();
    };

    const settle = (event: TouchEvent | null) => {
      if (dragging) {
        const lastMoveTime = samples.at(-1)?.time ?? startTime;
        const flicking =
          event !== null &&
          event.timeStamp - lastMoveTime <= FLICK_MAX_IDLE_MS &&
          windowVelocity(samples) >= SWIPE_FLICK_VELOCITY;
        const open = flicking || progress >= SWIPE_SETTLE_FRACTION;
        // A completed drag must not also activate whatever is under the finger.
        if (event?.cancelable) event.preventDefault();
        callbacks.current.onDragProgress(null);
        callbacks.current.onSettle(open);
      }
      reset();
    };
    const onTouchEnd = (event: TouchEvent) => settle(event);
    const onTouchCancel = () => settle(null);

    const activeOptions: AddEventListenerOptions = { passive: false };
    document.addEventListener("touchstart", onTouchStart, { passive: true });
    document.addEventListener("touchmove", onTouchMove, activeOptions);
    document.addEventListener("touchend", onTouchEnd, activeOptions);
    document.addEventListener("touchcancel", onTouchCancel);
    return () => {
      document.removeEventListener("touchstart", onTouchStart);
      document.removeEventListener("touchmove", onTouchMove, activeOptions);
      document.removeEventListener("touchend", onTouchEnd, activeOptions);
      document.removeEventListener("touchcancel", onTouchCancel);
      if (dragging) callbacks.current.onDragProgress(null);
    };
  }, [enabled, drawerRef, surfaceSelector, holdMs]);
}
