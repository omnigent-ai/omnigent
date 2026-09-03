// Resize hook for the always-visible left sidebar (the conversations aside in
// AppShell/Sidebar). Mirrors useResizableInlinePanel's persistence + keyboard
// behavior, but for the inline-start panel: the drag handle lives on the
// sidebar's inline-end edge, so the live width tracks the cursor's distance
// from the viewport's inline-start edge. It keeps its own module-level store +
// preference key so resizing the sidebar never disturbs the right rail's
// inline-panel width (and vice versa).
//
// Unlike the inline panel this has no "boost" machinery — nothing auto-widens
// the sidebar — so the store is just a persisted, viewport-clamped width.

import { useCallback, useEffect, useReducer, useRef } from "react";
import { createResizableWidthStore, useResizableWidthSnapshot } from "@/hooks/resizableWidthStore";
import { useInputCapabilities } from "@/hooks/useInputCapabilities";
import { useResizeDrag } from "@/hooks/useResizeDrag";
import { MD_MIN_WIDTH_QUERY, isMobileViewport, subscribeMatchMedia } from "@/lib/breakpoints";
import { readPanelSizePreference, writePanelSizePreference } from "@/lib/panelSizePreferences";

// The default gives conversation titles room before truncating. The floor keeps
// controls usable; the viewport-relative ceiling preserves the main content.
const DEFAULT_WIDTH_PX = 320;
const MIN_WIDTH_PX = 220;
const MAX_WIDTH_RATIO = 0.5;
const PAINTED_STRIP_PX = 4; // must match the handle's `w-1` class
const COARSE_GUTTER_PX = 8;
const FINE_GUTTER_PX = 6;
const INWARD_SLIVER_PX = 8;
const OUTWARD_SLIVER_PX = 10;

function gutterStyle(coarsePointer: boolean): React.CSSProperties {
  const gutter = coarsePointer ? COARSE_GUTTER_PX : FINE_GUTTER_PX;
  const inset = (gutter - PAINTED_STRIP_PX) / 2;
  // The handle is absolutely anchored to the sidebar's right edge (the seam),
  // NOT an in-flow flex gutter, so it must position with an explicit negative
  // end inset: on an absolutely positioned right-anchored box the flex hooks'
  // negative `marginInlineStart` is absorbed by the auto left inset, which
  // shifted the whole hit box inward — overlapping the conversation rows'
  // hover kebab — and pulled the painted strip off the seam. Anchoring the
  // border box at seam + OUTWARD_SLIVER and padding the 4px strip back to the
  // seam makes the hit box span
  // [seam − INWARD_SLIVER − inset, seam + OUTWARD_SLIVER + inset], matching
  // the flex-gutter hooks' inward/outward reach.
  return {
    touchAction: "none",
    boxSizing: "content-box",
    paddingInlineStart: INWARD_SLIVER_PX - PAINTED_STRIP_PX + inset,
    paddingInlineEnd: OUTWARD_SLIVER_PX + inset,
    insetInlineEnd: -(OUTWARD_SLIVER_PX + inset),
    backgroundClip: "content-box",
  };
}

function clamp(w: number): number {
  // No viewport available off the DOM (SSR / node test env) — this runs during
  // render, so guard before reading ``window`` to avoid a hard throw.
  if (typeof window === "undefined") return Math.max(MIN_WIDTH_PX, w);
  const ceiling = window.innerWidth * MAX_WIDTH_RATIO;
  return Math.max(MIN_WIDTH_PX, Math.min(w, ceiling));
}

// ---------------------------------------------------------------------------
// Module-level width store (independent of the inline panel / push-panel stores)
// ---------------------------------------------------------------------------

const widthStore = createResizableWidthStore(readPanelSizePreference("sidebarWidthPx"), (width) =>
  writePanelSizePreference("sidebarWidthPx", width),
);

/** Reset module-level state. Only for use in tests. */
export function resetSidebarWidthStoreForTesting(): void {
  widthStore.reset(readPanelSizePreference("sidebarWidthPx"));
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/**
 * Makes the desktop left sidebar resizable via a drag handle on its right
 * edge. Persists the chosen width across reloads and re-clamps on viewport
 * resize so the sidebar can't overflow a shrunken window.
 *
 * Returns the current pixel width and handle props to spread onto the resize
 * handle element. Desktop-only — callers should not render the handle on
 * mobile (where the sidebar is a full-screen overlay).
 */
export function useResizableSidebar() {
  const { anyCoarse } = useInputCapabilities();
  const raw = useResizableWidthSnapshot(widthStore);
  const width = clamp(raw ?? DEFAULT_WIDTH_PX);
  const [, bumpViewport] = useReducer((version: number) => version + 1, 0);

  // Re-clamp on viewport resize so a shrunken window pulls the sidebar back
  // under the ceiling; widening re-derives from the persisted preference so the
  // user's chosen width springs back when space returns.
  useEffect(() => {
    function onResize() {
      widthStore.set((prev) => clamp(widthStore.getPreferred() ?? prev ?? DEFAULT_WIDTH_PX));
      bumpViewport();
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Cancellation (Escape, blur, …) restores the width the drag started from —
  // onMove writes the live store on every pointermove, so without the
  // snapshot an aborted drag would keep the dragged width on screen while the
  // persisted preference still held the old one.
  const dragStartWidth = useRef<number | null>(null);
  const dragDirection = useRef<"ltr" | "rtl">("ltr");
  const resizeDrag = useResizeDrag({
    onStart: useCallback((event: React.PointerEvent<HTMLElement>) => {
      dragStartWidth.current = widthStore.getSnapshot();
      dragDirection.current =
        getComputedStyle(event.currentTarget).direction === "rtl" ? "rtl" : "ltr";
    }, []),
    onCancel: useCallback(() => {
      widthStore.set(dragStartWidth.current);
    }, []),
    onCommit: widthStore.persist,
    onMove: useCallback((e: React.PointerEvent<HTMLElement>) => {
      const inlineStartDistance =
        dragDirection.current === "rtl" ? window.innerWidth - e.clientX : e.clientX;
      widthStore.set(clamp(inlineStartDistance));
    }, []),
    observeHandleRemoval: true,
  });

  // A touch/pen drag started at desktop width can outlive the viewport
  // crossing below the md breakpoint: the handle hides (the sidebar becomes a
  // full-screen overlay) but pointer capture persists, so release would write
  // an unintended width. Cancel — restoring the pre-drag width — the moment
  // the desktop query stops matching.
  const cancelResizeDrag = resizeDrag.cancelDrag;
  useEffect(
    () =>
      subscribeMatchMedia([MD_MIN_WIDTH_QUERY], () => {
        if (isMobileViewport()) cancelResizeDrag();
      }),
    [cancelResizeDrag],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      const step = 20;
      // Right edge of a left panel: ArrowRight widens, ArrowLeft narrows.
      if (e.key === "ArrowRight") {
        e.preventDefault();
        widthStore.set((prev) => clamp((prev ?? width) + step), true);
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        widthStore.set((prev) => clamp((prev ?? width) - step), true);
      }
    },
    [width],
  );

  return {
    /** Current sidebar width in px (already viewport-clamped). */
    width,
    handleProps: {
      ...resizeDrag.handleProps,
      onKeyDown,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize sidebar",
      "aria-valuenow": width,
      "aria-valuemin": MIN_WIDTH_PX,
      "aria-valuemax":
        typeof window === "undefined"
          ? width
          : Math.max(MIN_WIDTH_PX, window.innerWidth * MAX_WIDTH_RATIO),
      tabIndex: 0,
      style: gutterStyle(anyCoarse),
    },
  };
}
