// A leftward swipe on the open mobile drawer (or the scrim beside it) slides it
// shut, reporting the same progress → settle contract as the native open swipe.

import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";

// Travel (CSS px) before a touch is classified as a horizontal swipe rather
// than a tap or a vertical scroll of the session list.
const DECIDE_SLOP_PX = 12;
// Fraction of the drawer's width the finger must cover to close on release.
// Mirrors the iOS shell's open threshold so both directions feel the same.
const CLOSE_DISTANCE_FRACTION = 0.33;
// Leftward flick speed (CSS px/ms) that closes regardless of distance. Mirrors
// the iOS shell's 600 pt/s open velocity.
const CLOSE_FLICK_VELOCITY = 0.6;
// A finger that rested this long before lifting is not flicking.
const FLICK_MAX_IDLE_MS = 100;
// Flick speed is the finger's travel over this trailing window, so one jittery
// or coalesced touchmove can't read as a flick on its own.
const VELOCITY_WINDOW_MS = 100;

interface TouchSample {
  x: number;
  time: number;
}

/** Leftward speeds are negative. `samples[0]` is the position at the window start. */
function windowVelocity(samples: TouchSample[]): number {
  const reference = samples[0];
  const latest = samples.at(-1);
  if (!reference || !latest) return 0;
  const span = Math.min(VELOCITY_WINDOW_MS, latest.time - reference.time);
  return span > 0 ? (latest.x - reference.x) / span : 0;
}

/** True when `target` sits inside content that can itself scroll horizontally. */
function startsInHorizontalScroller(target: EventTarget | null, boundary: Element): boolean {
  let node = target instanceof Element ? target : null;
  while (node && node !== boundary) {
    if (node.scrollWidth > node.clientWidth + 1) {
      const { overflowX } = getComputedStyle(node);
      if (overflowX === "auto" || overflowX === "scroll") return true;
    }
    node = node.parentElement;
  }
  return false;
}

export interface SidebarSwipeToCloseOptions {
  /** Listen only while the drawer is open in the mobile drawer layout. */
  enabled: boolean;
  /** The drawer: a swipe may start on it, and its width scales the drag. */
  drawerRef: RefObject<HTMLElement | null>;
  /** The scrim over the exposed chat strip, which a swipe may also start on. */
  scrimRef: RefObject<HTMLElement | null>;
  /**
   * A touch held this long (ms) before moving belongs to the row's long-press
   * gestures (drag-to-reorder, context menu), not to this swipe.
   */
  holdMs: number;
  /** Live open fraction (1 → 0) while dragging; `null` once released. */
  onDragProgress: (progress: number | null) => void;
  /** Release decision: `true` closes the drawer, `false` springs it back open. */
  onSettle: (close: boolean) => void;
}

/** Attach the swipe-left-to-close gesture to an open sidebar drawer. */
export function useSidebarSwipeToClose({
  enabled,
  drawerRef,
  scrimRef,
  holdMs,
  onDragProgress,
  onSettle,
}: SidebarSwipeToCloseOptions): void {
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
    let progress = 1;
    let samples: TouchSample[] = [];

    const reset = () => {
      tracking = false;
      dragging = false;
      samples = [];
    };
    const recordSample = (x: number, time: number) => {
      samples.push({ x, time });
      // Keep the newest sample at or before the window start as its reference.
      while (samples.length > 2 && samples[1].time <= time - VELOCITY_WINDOW_MS) samples.shift();
    };

    const onTouchStart = (event: TouchEvent) => {
      // A second finger ends the current drag where it is, like a cancel.
      if (dragging) settle(null);
      reset();
      if (event.touches.length !== 1) return;
      const drawer = drawerRef.current;
      const target = event.target instanceof Node ? event.target : null;
      if (!drawer || !target) return;
      const onDrawer = drawer.contains(target);
      if (!onDrawer && !scrimRef.current?.contains(target)) return;
      if (onDrawer && startsInHorizontalScroller(target, drawer)) return;
      const touch = event.touches[0];
      tracking = true;
      startX = touch.clientX;
      startY = touch.clientY;
      startTime = event.timeStamp;
      recordSample(startX, startTime);
      drawerWidth = Math.max(1, drawer.getBoundingClientRect().width);
      progress = 1;
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
      recordSample(touch.clientX, event.timeStamp);
      if (!dragging) {
        if (Math.abs(dx) < DECIDE_SLOP_PX && Math.abs(dy) < DECIDE_SLOP_PX) return;
        // Vertical scrolls, rightward swipes, and hold-then-drag belong to
        // the list, the shell's edge swipe, and row long-press respectively.
        if (Math.abs(dy) >= Math.abs(dx) || dx > 0 || event.timeStamp - startTime >= holdMs) {
          tracking = false;
          return;
        }
        dragging = true;
      }
      progress = Math.max(0, Math.min(1, 1 + dx / drawerWidth));
      callbacks.current.onDragProgress(progress);
      if (event.cancelable) event.preventDefault();
    };

    const settle = (event: TouchEvent | null) => {
      if (dragging) {
        const lastMoveTime = samples.at(-1)?.time ?? startTime;
        const flicking =
          event !== null &&
          event.timeStamp - lastMoveTime <= FLICK_MAX_IDLE_MS &&
          windowVelocity(samples) <= -CLOSE_FLICK_VELOCITY;
        const close = flicking || progress <= 1 - CLOSE_DISTANCE_FRACTION;
        // A completed drag must not also activate the row under the finger.
        if (event?.cancelable) event.preventDefault();
        callbacks.current.onDragProgress(null);
        callbacks.current.onSettle(close);
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
  }, [enabled, drawerRef, scrimRef, holdMs]);
}
