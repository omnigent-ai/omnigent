// A downward swipe blurs the composer, closing the soft keyboard as native iOS
// apps do. The iOS shell disables document scrolling, so WebKit's own
// scroll-driven keyboard dismissal never fires.

import { useEffect } from "react";

import { isTextEntry } from "@/lib/hotkeys";
import { DECIDE_SLOP_PX } from "./useSidebarSwipeToClose";
import { OWN_TOUCH_SELECTOR } from "./useSidebarSwipeToOpen";

// Downward travel (CSS px) after which the swipe dismisses the keyboard.
export const DISMISS_DISTANCE_PX = 40;
// A touch held this long (ms) before moving is a long press (iOS text
// selection), not a swipe. Shorter limits reject slow, deliberate swipes.
export const DISMISS_HOLD_MS = 500;

/** True for touches outside `surface`, on elements with their own touch
 * handling, or in overlays (popovers, menus) that don't host the focused field. */
function ignoresTouch(target: Element, surface: Element, focused: Element): boolean {
  if (!surface.contains(target)) return true;
  if (target.closest(OWN_TOUCH_SELECTOR)) return true;
  for (let node: Element | null = target; node && node !== surface; node = node.parentElement) {
    if (getComputedStyle(node).position === "fixed" && !node.contains(focused)) return true;
  }
  return false;
}

export interface SwipeDownToDismissKeyboardOptions {
  /** Listen only while the composer holds focus in the mobile layout. */
  enabled: boolean;
  /** Selector for the ancestor a swipe may start inside. */
  surfaceSelector: string;
}

/** Blur the focused text field when the user swipes down across the page. */
export function useSwipeDownToDismissKeyboard({
  enabled,
  surfaceSelector,
}: SwipeDownToDismissKeyboardOptions): void {
  useEffect(() => {
    if (!enabled) return;

    let focused: HTMLElement | null = null;
    let decided = false;
    let startX = 0;
    let startY = 0;
    let startTime = 0;

    const reset = () => {
      focused = null;
      decided = false;
    };

    const onTouchStart = (event: TouchEvent) => {
      reset();
      if (event.touches.length !== 1) return;
      const active = document.activeElement;
      const target = event.target instanceof Element ? event.target : null;
      const surface = target?.closest(surfaceSelector);
      if (!(active instanceof HTMLElement) || !isTextEntry(active) || !target || !surface) return;
      if (ignoresTouch(target, surface, active)) return;
      focused = active;
      startX = event.touches[0].clientX;
      startY = event.touches[0].clientY;
      startTime = event.timeStamp;
    };

    const onTouchMove = (event: TouchEvent) => {
      if (!focused) return;
      if (event.touches.length !== 1) {
        reset();
        return;
      }
      const dx = event.touches[0].clientX - startX;
      const dy = event.touches[0].clientY - startY;
      if (!decided) {
        if (Math.abs(dx) < DECIDE_SLOP_PX && Math.abs(dy) < DECIDE_SLOP_PX) return;
        // Sideways swipes belong to the sidebar and scrollers, upward ones to
        // reading newer content, and hold-then-drag to long-press gestures.
        if (
          Math.abs(dy) <= Math.abs(dx) ||
          dy < 0 ||
          event.timeStamp - startTime >= DISMISS_HOLD_MS
        ) {
          reset();
          return;
        }
        decided = true;
      }
      if (dy < DISMISS_DISTANCE_PX) return;
      // The page keeps scrolling under the finger; only the keyboard goes away.
      if (document.activeElement === focused) focused.blur();
      reset();
    };

    document.addEventListener("touchstart", onTouchStart, { passive: true });
    document.addEventListener("touchmove", onTouchMove, { passive: true });
    document.addEventListener("touchend", reset);
    document.addEventListener("touchcancel", reset);
    return () => {
      document.removeEventListener("touchstart", onTouchStart);
      document.removeEventListener("touchmove", onTouchMove);
      document.removeEventListener("touchend", reset);
      document.removeEventListener("touchcancel", reset);
    };
  }, [enabled, surfaceSelector]);
}
