// Resize hook for the CommentsPanel inside the FileViewer.
//
// Unlike the right-side push panels (useResizablePanel /
// useResizableInlinePanel), the CommentsPanel is NOT pinned to the
// viewport's right edge — it sits at the right edge of the FileViewer,
// which itself has an arbitrary width. So width is derived from the
// panel's own right edge (`containerRef.right - clientX`), not from
// `window.innerWidth - clientX`. The drag handle lives on the panel's
// LEFT edge; dragging it leftward widens the panel and the flex-1 code
// viewer (min-w-0) absorbs the difference.
//
// Width is kept in a module-level store so the chosen width survives
// the panel unmounting when comments are toggled off or a different
// file is opened, matching the other panel-resize hooks. Explicit user
// resizes are also persisted so a full page reload restores the width.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createResizableWidthStore, useResizableWidthSnapshot } from "@/hooks/resizableWidthStore";
import { useInputCapabilities } from "@/hooks/useInputCapabilities";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useResizeDrag } from "@/hooks/useResizeDrag";
import { readPanelSizePreference, writePanelSizePreference } from "@/lib/panelSizePreferences";

const DEFAULT_WIDTH_PX = 240;
const MIN_WIDTH_PX = 200;
const MAX_WIDTH_PX = 640;
/** Keep at least this much room for the code/diff viewer beside the panel. */
const MIN_VIEWER_PX = 240;
// The handle is a dedicated divider gutter between the viewer and the panel —
// a real flex child outside both scroll containers, so its hit area overlays
// almost no content. The painted strip (`w-1`) sits centered in the gutter;
// invisible padding fills the rest and overhangs each side by a small sliver
// via negative margins. The slivers are capped so the viewer's scrollbar and
// the panel's header/tabs/cards keep their taps and scroll starts — which
// also caps the hit total at 26px coarse / 24px fine (TR-7's 24px floor; the
// preferred 44px would need a visually wide gutter the layout doesn't permit).
const PAINTED_STRIP_PX = 4; // must match the handle's `w-1` class
const GUTTER_COARSE_PX = 8;
const GUTTER_FINE_PX = 6;
const VIEWER_SLIVER_PX = 10; // ≤10: a 14px viewer scrollbar keeps 4px + its own gutter
const INWARD_SLIVER_PX = 8; // ≤8: stays within the panel's 12px content gutter

/** Inline style for the divider-gutter handle: layout footprint = gutter
 * width, hit box = gutter + both slivers, paint = the centered `w-1` strip. */
function gutterStyle(isCoarse: boolean): React.CSSProperties {
  const gutter = isCoarse ? GUTTER_COARSE_PX : GUTTER_FINE_PX;
  const inset = (gutter - PAINTED_STRIP_PX) / 2;
  return {
    touchAction: "none",
    boxSizing: "content-box",
    paddingLeft: VIEWER_SLIVER_PX + inset,
    paddingRight: INWARD_SLIVER_PX + inset,
    marginLeft: -VIEWER_SLIVER_PX,
    marginRight: -INWARD_SLIVER_PX,
    backgroundClip: "content-box",
  };
}

// ---------------------------------------------------------------------------
// Module-level width store (shared across panel remounts within a session)
// ---------------------------------------------------------------------------

const widthStore = createResizableWidthStore(
  readPanelSizePreference("commentsPanelWidthPx"),
  (width) => writePanelSizePreference("commentsPanelWidthPx", width),
);

/** Reset module-level width state from localStorage. Only for tests. */
export function resetCommentsWidthStoreForTesting(): void {
  widthStore.reset(readPanelSizePreference("commentsPanelWidthPx"));
}

/**
 * Makes the CommentsPanel resizable via a drag handle on its left edge.
 *
 * On desktop (`≥ md`) returns a pixel `width` to apply as an inline style
 * plus `handleProps` for the drag handle. On mobile (`< md`) the panel is
 * stacked full-width below the viewer, so `width` is `undefined` (the
 * `w-full` class wins) and the handle should not be rendered.
 *
 * `containerRef` must be attached to the panel root so drag math can anchor
 * to the panel's right edge, and the dynamic max can leave room for the
 * sibling viewer.
 */
export function useResizableCommentsPanel() {
  const { anyCoarse } = useInputCapabilities();
  const mobileViewport = useIsMobileViewport();
  const isDesktop = typeof window !== "undefined" && !mobileViewport;
  const raw = useResizableWidthSnapshot(widthStore);
  const width = Math.max(MIN_WIDTH_PX, Math.min(raw ?? DEFAULT_WIDTH_PX, MAX_WIDTH_PX));
  const containerRef = useRef<HTMLDivElement | null>(null);

  // Clamp a candidate width to [MIN, dynamic max], leaving MIN_VIEWER_PX for
  // the sibling code/diff viewer so the panel can't swallow the whole row.
  // The divider gutter is a third flex child in the row, so its footprint
  // comes out of the budget too — always the coarse 8px, so a pointer-type
  // flip mid-session can never shrink the viewer below its minimum.
  const reachableMax = useCallback((): number => {
    const parent = containerRef.current?.parentElement;
    const parentWidth =
      parent?.getBoundingClientRect().width ??
      (typeof window === "undefined" ? MAX_WIDTH_PX : window.innerWidth);
    return Math.max(
      MIN_WIDTH_PX,
      Math.min(MAX_WIDTH_PX, parentWidth - MIN_VIEWER_PX - GUTTER_COARSE_PX),
    );
  }, []);

  const clampWidth = useCallback(
    (candidate: number): number => Math.max(MIN_WIDTH_PX, Math.min(candidate, reachableMax())),
    [reachableMax],
  );

  // A row can resize without the window changing (for example, the sidebar
  // opens). Re-render so the separator's reachable ARIA maximum stays in sync.
  const [constraintVersion, setConstraintVersion] = useState(0);
  useEffect(() => {
    const parent = containerRef.current?.parentElement;
    if (!parent || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      widthStore.set((prev) => {
        const base = widthStore.getPreferred() ?? prev;
        return base !== null ? clampWidth(base) : prev;
      });
      setConstraintVersion((version) => version + 1);
    });
    observer.observe(parent);
    return () => observer.disconnect();
  }, [clampWidth]);

  // Cancellation restores the pre-drag width: onMove writes the live store on
  // every pointermove, so an abort (Escape, blur, …) must undo those writes.
  const dragStartWidth = useRef<number | null>(null);
  const resizeDrag = useResizeDrag({
    enabled: isDesktop,
    overlay: true,
    onStart: useCallback(() => {
      dragStartWidth.current = widthStore.getSnapshot();
    }, []),
    onCancel: useCallback(() => {
      widthStore.set(dragStartWidth.current !== null ? clampWidth(dragStartWidth.current) : null);
    }, [clampWidth]),
    onCommit: widthStore.persist,
    onMove: useCallback(
      (e: React.PointerEvent) => {
        if (!containerRef.current) return;
        const right = containerRef.current.getBoundingClientRect().right;
        widthStore.set(clampWidth(right - e.clientX));
      },
      [clampWidth],
    ),
  });

  // Keyboard resize: left/right arrows widen/narrow by 20px.
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      const step = 20;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        widthStore.set((prev) => clampWidth((prev ?? DEFAULT_WIDTH_PX) + step), true);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        widthStore.set((prev) => clampWidth((prev ?? DEFAULT_WIDTH_PX) - step), true);
      }
    },
    [clampWidth],
  );

  // Re-clamp the stored width when the viewport resizes so a width chosen on
  // a wider layout doesn't crowd out the viewer after the window shrinks.
  useEffect(() => {
    function onResize() {
      // Re-derive the effective width from the persisted preference so the
      // panel widens back to the user's choice when the row regains space.
      widthStore.set((prev) => {
        const base = widthStore.getPreferred() ?? prev;
        return base !== null ? clampWidth(base) : prev;
      });
      setConstraintVersion((version) => version + 1);
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [clampWidth]);

  const ariaValueMax = useMemo(() => {
    // Row-size signals invalidate the cached layout measurement.
    void constraintVersion;
    return reachableMax();
  }, [constraintVersion, reachableMax]);

  return {
    /** Pixel width to apply as an inline style (undefined on mobile). */
    width: isDesktop ? width : undefined,
    /** Attach to the panel root to anchor drag math and the dynamic max. */
    containerRef,
    /** Whether the resize handle should render (desktop only). */
    isDesktop,
    /**
     * Props to spread onto the divider-gutter handle. Render it as the
     * panel's PRECEDING SIBLING in the split row (a `w-1 shrink-0` flex
     * child), never inside either scroll container — the pads would be
     * clipped and would steal the neighbors' pointer streams.
     */
    handleProps: {
      ...resizeDrag.handleProps,
      onKeyDown,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize comments panel",
      "aria-valuenow": width,
      "aria-valuemin": MIN_WIDTH_PX,
      "aria-valuemax": ariaValueMax,
      tabIndex: 0,
      // The gutter owns its touches outright (no scroll/selection may start
      // from it). With content-box sizing the `w-1` class is the painted
      // strip; padding centers it in the gutter and adds the overhang
      // slivers, whose footprint the negative margins cancel — so the
      // element occupies exactly the gutter width and the hover/active
      // background (content-box clipped) never widens visually.
      style: gutterStyle(anyCoarse),
    },
  };
}
