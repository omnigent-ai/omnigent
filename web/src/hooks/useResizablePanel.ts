import { useCallback, useEffect, useReducer, useRef } from "react";
import {
  arrowResizeDelta,
  createPersistedWidthStore,
  useResizableWidthSnapshot,
} from "@/hooks/resizableWidthStore";
import { useInputCapabilities } from "@/hooks/useInputCapabilities";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useResizeDrag } from "@/hooks/useResizeDrag";

const MIN_WIDTH_PX = 320;
const MAX_WIDTH_RATIO = 0.8; // 80% of viewport
const PAINTED_HANDLE_WIDTH_PX = 4;
export const HANDLE_OUTWARD_SLIVER_PX = 6;
export const HANDLE_INWARD_SLIVER_PX = 8;
export const HANDLE_COARSE_GUTTER_PX = 12;
export const HANDLE_FINE_GUTTER_PX = 10;

function handleGutterStyle(isCoarse: boolean): React.CSSProperties {
  const gutter = isCoarse ? HANDLE_COARSE_GUTTER_PX : HANDLE_FINE_GUTTER_PX;
  const inset = (gutter - PAINTED_HANDLE_WIDTH_PX) / 2;
  return {
    touchAction: "none",
    boxSizing: "content-box",
    paddingInlineStart: HANDLE_OUTWARD_SLIVER_PX + inset,
    paddingInlineEnd: HANDLE_INWARD_SLIVER_PX + inset,
    marginInlineStart: -HANDLE_OUTWARD_SLIVER_PX,
    marginInlineEnd: -HANDLE_INWARD_SLIVER_PX,
    backgroundClip: "content-box",
  };
}

/** Clamp a width value to the allowed range for the current viewport. */
function clampWidth(w: number, minPx = MIN_WIDTH_PX): number {
  // No viewport ceiling available off the DOM (SSR / node test env) — this runs
  // during render, so guard before reading `window` to avoid a hard throw.
  if (typeof window === "undefined") return Math.max(minPx, w);
  return Math.max(minPx, Math.min(w, window.innerWidth * MAX_WIDTH_RATIO));
}

// ---------------------------------------------------------------------------
// Shared width store
// ---------------------------------------------------------------------------
// Every right-side push panel (FileViewer, TerminalsPanel,
// ExecutionLogsPanel, FilesPanelDrawer) reads and writes the same
// width via this module-level store. Without sharing, switching
// between panels would snap the layout back to each panel's
// independent default, which feels like the chat is jumping width
// for no reason. ``null`` means "not yet set — fall back to the
// caller's default (vw-based)". The first drag persists a px value.
const widthStore = createPersistedWidthStore("pushPanelWidthPx");

/** Reset module-level width state from localStorage. Only for tests. */
export function resetSharedWidthStoreForTesting(): void {
  widthStore.resetForTesting();
}

/**
 * Hook for making a right-side panel resizable via pointer drag on its left edge.
 *
 * On desktop (`≥ md`) the panel width is controlled via an inline style
 * driven by drag state. On mobile (`< md`) the panel is a full-screen
 * overlay — the hook returns `undefined` so no inline width is set.
 *
 * The width is stored at module scope and shared across every caller,
 * so all right-side push panels resize together — the layout stays
 * stable as the user switches between FileViewer / Terminals / Logs /
 * Files.
 *
 * Returns the current pixel width (or undefined on mobile) and a set of
 * props to spread onto the resize handle element.
 */
export function useResizablePanel(open: boolean, defaultWidthVw = 50, minWidthPx = MIN_WIDTH_PX) {
  const { anyCoarse } = useInputCapabilities();
  const mobileViewport = useIsMobileViewport();
  const isDesktop = typeof window !== "undefined" && !mobileViewport;
  const width = useResizableWidthSnapshot(widthStore);
  const minWidthRef = useRef(minWidthPx);
  minWidthRef.current = minWidthPx;
  const [, bumpViewport] = useReducer((version: number) => version + 1, 0);

  // Re-clamp the stored width when the viewport resizes so a width
  // that was valid on a wider monitor doesn't push content off-screen
  // after shrinking the browser.
  useEffect(() => {
    function onResize() {
      // Re-derive the effective width from the persisted preference (not the
      // possibly-already-clamped live value) so widening the viewport restores
      // the user's larger choice instead of sticking at the prior clamp.
      widthStore.set((prev) => {
        const base = widthStore.getPreferred() ?? prev;
        return base !== null ? clampWidth(base, minWidthRef.current) : prev;
      });
      bumpViewport();
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // When the minimum width increases (e.g. comments panel opens), ensure
  // the current shared width is at least the new minimum.
  useEffect(() => {
    widthStore.set((prev) => {
      if (prev !== null && prev < minWidthPx) return clampWidth(prev, minWidthPx);
      return prev;
    });
  }, [minWidthPx]);

  // Initialise from viewport on first open (or if never set), respecting the minimum.
  const resolvedWidth = clampWidth(
    width ?? (typeof window !== "undefined" ? window.innerWidth * (defaultWidthVw / 100) : 600),
    minWidthPx,
  );

  const resizeDrag = useResizeDrag({
    enabled: open && isDesktop,
    overlay: true,
    onStart: widthStore.beginDrag,
    onCancel: widthStore.rollbackDrag,
    onCommit: widthStore.persist,
    onMove: useCallback((e: React.PointerEvent<HTMLElement>) => {
      widthStore.set(clampWidth(window.innerWidth - e.clientX, minWidthRef.current));
    }, []),
  });

  // Keyboard resize: left/right arrow keys adjust width by 20px.
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (!open || !isDesktop) return;
      const delta = arrowResizeDelta(e, "ArrowLeft");
      if (delta === null) return;
      widthStore.set(
        (prev) => clampWidth((prev ?? resolvedWidth) + delta, minWidthRef.current),
        true,
      );
    },
    [open, isDesktop, resolvedWidth],
  );

  // On mobile the panel is a fixed full-screen overlay — no inline width.
  const panelWidth = isDesktop ? (open ? resolvedWidth : 0) : undefined;

  return {
    /** Pixel width to apply as an inline style (undefined on mobile). */
    panelWidth,
    /** Props to spread onto the resize handle element. */
    handleProps: {
      ...resizeDrag.handleProps,
      onKeyDown,
      style: handleGutterStyle(anyCoarse),
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize panel",
      "aria-valuenow": resolvedWidth,
      "aria-valuemin": minWidthPx,
      "aria-valuemax":
        typeof window === "undefined"
          ? resolvedWidth
          : Math.max(minWidthPx, window.innerWidth * MAX_WIDTH_RATIO),
      tabIndex: 0,
    },
    /** Whether the resize handle should be visible (desktop only). */
    isDesktop,
  };
}
