import { useCallback, useRef, useState } from "react";

import { useInputCapabilities } from "@/hooks/useInputCapabilities";
import { useResizeDrag } from "@/hooks/useResizeDrag";

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
  const { anyCoarse } = useInputCapabilities();
  const [width, setWidth] = useState(defaultWidth);
  const containerRef = useRef<HTMLElement | null>(null);
  const pad = anyCoarse ? COARSE_PAD : FINE_PAD;

  const clamp = useCallback(
    (w: number) => Math.max(minWidth, Math.min(maxWidth, w)),
    [minWidth, maxWidth],
  );

  // Cancellation restores the pre-drag width: onMove applies each pointermove
  // live, so an abort (Escape, blur, …) must undo those writes rather than
  // silently keeping the dragged width.
  const dragStartWidth = useRef(defaultWidth);
  const resizeDrag = useResizeDrag({
    enabled,
    onStart: useCallback(() => {
      dragStartWidth.current = width;
    }, [width]),
    onCancel: useCallback(() => {
      setWidth(clamp(dragStartWidth.current));
    }, [clamp]),
    onMove: useCallback(
      (e: React.PointerEvent) => {
        if (!containerRef.current) return;
        const left = containerRef.current.getBoundingClientRect().left;
        setWidth(clamp(e.clientX - left));
      },
      [clamp],
    ),
  });

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      // Keyboard resize obeys the same gate as pointer drags: a handle inside
      // a closed (aria-hidden) panel must not resize the off-screen column.
      if (!enabled) return;
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
    [clamp, enabled],
  );

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
      ...resizeDrag.handleProps,
      onKeyDown,
      role: "separator" as const,
      // Focusable only while resizing is possible: a tabbable separator
      // inside an aria-hidden closed panel is an ARIA violation, and a
      // keyboard user could otherwise focus an invisible handle.
      tabIndex: enabled ? 0 : -1,
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
