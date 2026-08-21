import { useCallback, useEffect, useRef, useState } from "react";

import { useInputCapabilities } from "@/hooks/useInputCapabilities";

const KEYBOARD_STEP_PX = 20;
// Width of the painted separator strip (the consumer's `w-1` element).
const PAINTED_WIDTH_PX = 4;

// Invisible hit-target padding around the painted strip. The handle sits over
// the boundary between the terminal list and the xterm pane, so the pad is
// biased toward the terminal side: list rows end with a status badge flush
// against the boundary (taps there must select the row), while the xterm's
// leftmost pixels are rarely interactive. Coarse pointers get a 44px total
// target; fine pointers get the 24px minimum so the pad steals less hover
// area from the row badges.
const COARSE_PAD = { left: 12, right: 28 }; // 12 + 4 + 28 = 44px
const FINE_PAD = { left: 6, right: 14 }; // 6 + 4 + 14 = 24px

export function useResizableColumn(
  defaultWidth = 176,
  minWidth = 100,
  maxWidth = 480,
  enabled = true,
) {
  const { coarsePrimary } = useInputCapabilities();
  const [width, setWidth] = useState(defaultWidth);
  // Pointer id of the active drag; null when idle. First pointer wins — a
  // second concurrent pointer is ignored until the first drag ends.
  const activePointerId = useRef<number | null>(null);
  const containerRef = useRef<HTMLElement | null>(null);
  // Removes the document-level fallback listeners for the active drag.
  const removeDocListeners = useRef<(() => void) | null>(null);
  const pad = coarsePrimary ? COARSE_PAD : FINE_PAD;

  const clamp = useCallback(
    (w: number) => Math.max(minWidth, Math.min(maxWidth, w)),
    [minWidth, maxWidth],
  );

  const endDrag = useCallback(() => {
    if (activePointerId.current === null) return;
    activePointerId.current = null;
    removeDocListeners.current?.();
    removeDocListeners.current = null;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }, []);

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      // Only the primary button starts a drag — right-click / pen barrel
      // button must not capture the pointer or flip body styles.
      if (!enabled || e.button !== 0) return;
      if (activePointerId.current !== null) return;
      // Capture so moves keep arriving when the pointer leaves the handle (or
      // crosses an iframe), and so no other gesture consumer sees the stream.
      // Capture first: if it throws (pointer already gone), stay idle rather
      // than publishing a drag that can never receive its end events.
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        return;
      }
      e.preventDefault();
      activePointerId.current = e.pointerId;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      // Document-level fallback: if the handle element unmounts mid-drag
      // (breakpoint flip, active terminal exits) its React handlers never
      // fire, which would leave the drag armed and body styles stuck.
      const onDocEnd = (ev: PointerEvent) => {
        if (ev.pointerId === activePointerId.current) endDrag();
      };
      document.addEventListener("pointerup", onDocEnd);
      document.addEventListener("pointercancel", onDocEnd);
      removeDocListeners.current = () => {
        document.removeEventListener("pointerup", onDocEnd);
        document.removeEventListener("pointercancel", onDocEnd);
      };
    },
    [enabled, endDrag],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (e.pointerId !== activePointerId.current || !containerRef.current) return;
      const left = containerRef.current.getBoundingClientRect().left;
      setWidth(clamp(e.clientX - left));
    },
    [clamp],
  );

  // pointerup ends the drag; pointercancel and capture loss abort it cleanly,
  // keeping the last applied width (never a half-state).
  const onPointerEnd = useCallback(
    (e: React.PointerEvent) => {
      if (e.pointerId !== activePointerId.current) return;
      endDrag();
    },
    [endDrag],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      // Vertical separator between columns: ArrowRight widens the left
      // column, ArrowLeft narrows it, with the same clamps as dragging.
      if (e.key === "ArrowRight") {
        e.preventDefault();
        setWidth((w) => clamp(w + KEYBOARD_STEP_PX));
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        setWidth((w) => clamp(w - KEYBOARD_STEP_PX));
      }
    },
    [clamp],
  );

  // Reset body cursor/selection if the hook itself unmounts mid-drag.
  useEffect(() => endDrag, [endDrag]);

  // The handle is conditionally rendered at desktop widths with an active
  // terminal. If that gate closes mid-drag, no element remains to deliver an
  // up/cancel event, so abort immediately.
  useEffect(() => {
    if (!enabled) endDrag();
  }, [enabled, endDrag]);

  return {
    /** Pixel width for the left column (apply as inline style). */
    width,
    /** Attach to the flex-row container to anchor drag calculations. */
    containerRef,
    /**
     * Spread onto the resize handle. Render the handle as a direct child of
     * the (overflow-hidden, relative) split row — NOT inside the scrollable
     * list panel, where the invisible pad would be clipped and add horizontal
     * overflow — absolutely positioned with `left: width`. The negative
     * margin then aligns the painted strip's right edge to the boundary,
     * with the invisible pad straddling it.
     */
    handleProps: {
      onPointerDown,
      onPointerMove,
      onPointerUp: onPointerEnd,
      onPointerCancel: onPointerEnd,
      onLostPointerCapture: onPointerEnd,
      onKeyDown,
      role: "separator" as const,
      tabIndex: 0,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize terminal list",
      "aria-valuenow": width,
      "aria-valuemin": minWidth,
      "aria-valuemax": maxWidth,
      style: {
        touchAction: "none",
        boxSizing: "content-box",
        paddingLeft: pad.left,
        paddingRight: pad.right,
        marginLeft: -(pad.left + PAINTED_WIDTH_PX),
        backgroundClip: "content-box",
      } as const,
    },
  };
}
