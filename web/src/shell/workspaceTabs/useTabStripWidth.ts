// The workspace strip's shared tab width, measured from the space the tabs have
// and held after a pointer close until the pointer leaves the strip.

import { type RefObject, useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";

import { useElementWidth } from "@/hooks/useElementWidth";
import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";
import { holdTabStripScroll } from "@/shell/TabStripScroller";
import { TAB_STRIP_MOTION } from "@/shell/workspaceSidebar/tabStripMotion";

import { INITIAL_STRIP_WIDTH_STATE, effectiveTabWidth, stripWidthReducer } from "./tabStripWidth";

export interface TabStripWidth {
  /** Every tab's width, in pixels. */
  width: number;
  /** The width of the room the tabs and "+" share, in pixels. */
  areaWidth: number;
  /** A pointer close is held: tabs keep their width, and the strip its scroll, until the pointer leaves. */
  holding: boolean;
  /**
   * Tabs are easing to a width set by a tab coming or going or a hold letting go. A resize of
   * the pane sets the width directly, so the tabs track the drag.
   */
  reflowing: boolean;
  /** Put on the element whose width the tabs (and "+") share. */
  areaRef: RefObject<HTMLDivElement | null>;
  /** Give to the strip's scroller as its row, so a pointer close can keep a scrolled row's width. */
  rowRef: RefObject<HTMLDivElement | null>;
  /** Pointer handlers for the whole strip, which decide when a held width is let go. */
  pointerHandlers: { onPointerEnter: () => void; onPointerLeave: () => void };
  /** Call before closing a tab by its × or a middle click. */
  closedByPointer: () => void;
  /** Call before closing a tab any other way. */
  closedByKeyboard: () => void;
}

/** `reserved` is the room the "+" button and its gap take inside the area. */
export function useTabStripWidth(
  count: number,
  reserved: number | ((areaWidth: number) => number),
): TabStripWidth {
  const [state, dispatch] = useReducer(stripWidthReducer, INITIAL_STRIP_WIDTH_STATE);
  const areaRef = useRef<HTMLDivElement | null>(null);
  const rowRef = useRef<HTMLDivElement | null>(null);
  const areaWidth = useElementWidth(areaRef);
  const available = Math.max(
    0,
    areaWidth - (typeof reserved === "function" ? reserved(areaWidth) : reserved),
  );

  const width = effectiveTabWidth(state, available, count);
  const latestWidth = useRef(width);
  latestWidth.current = width;
  const reflowing = useReflowing(count, state.heldWidth !== null, available);

  // A new tab lays the strip out again, held or not.
  const previousCount = useRef(count);
  useLayoutEffect(() => {
    if (count > previousCount.current) dispatch({ type: "tab-added" });
    previousCount.current = count;
  }, [count]);

  return {
    width,
    areaWidth,
    holding: state.heldWidth !== null,
    reflowing,
    areaRef,
    rowRef,
    pointerHandlers: {
      onPointerEnter: () => dispatch({ type: "pointer-enter" }),
      onPointerLeave: () => dispatch({ type: "pointer-leave" }),
    },
    closedByPointer: () => {
      // Before the tab goes: a scrolled row keeps its width so the scroll can't clamp; an
      // unscrolled one shrinks, and "+" follows the last tab.
      const row = rowRef.current;
      if (row) holdTabStripScroll(row);
      dispatch({ type: "closed-by-pointer", width: latestWidth.current });
    },
    closedByKeyboard: () => dispatch({ type: "closed-by-keyboard" }),
  };
}

interface ReflowCause {
  count: number;
  holding: boolean;
  available: number;
  /** Counts each reflow, so a new one restarts the timer that ends it. */
  generation: number;
  reflowing: boolean;
}

/**
 * Whether the tabs ease to their width. Decided while rendering, so the transition is on in
 * the same commit as the new width and nothing can measure the width before it starts.
 */
function useReflowing(count: number, holding: boolean, available: number): boolean {
  const reduced = usePrefersReducedMotion();
  const [cause, setCause] = useState<ReflowCause>(() => ({
    count,
    holding,
    available,
    generation: 0,
    reflowing: false,
  }));
  let current = cause;
  if (count !== cause.count || holding !== cause.holding) {
    current = { count, holding, available, generation: cause.generation + 1, reflowing: true };
  } else if (available !== cause.available) {
    // The pane is being resized: let the tabs follow it at once.
    current = { ...cause, available, reflowing: false };
  }
  if (current !== cause) setCause(current);

  useEffect(() => {
    if (!cause.reflowing) return;
    const timer = setTimeout(
      () => setCause((latest) => (latest.reflowing ? { ...latest, reflowing: false } : latest)),
      // Past the transition's own end, so its last frames aren't cut when it starts late.
      TAB_STRIP_MOTION.reflowMs + TAB_STRIP_MOTION.settleSlackMs,
    );
    return () => clearTimeout(timer);
  }, [cause.reflowing, cause.generation]);

  return current.reflowing && !reduced;
}
