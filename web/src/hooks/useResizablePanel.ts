import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useInputCapabilities } from "@/hooks/useInputCapabilities";
import { MD_MIN_WIDTH_QUERY, isMobileViewport } from "@/lib/breakpoints";
import { readPanelSizePreference, writePanelSizePreference } from "@/lib/panelSizePreferences";

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
// `preferredWidth` mirrors the persisted user choice; `sharedWidth` is the
// effective (viewport-clamped) width that drives layout. They diverge after a
// viewport-shrink clamp — keeping the preference in memory lets the resize
// handler re-derive the effective width from it (restoring the larger choice
// when space returns) without reading localStorage on every resize event.
let preferredWidth: number | null = readPanelSizePreference("pushPanelWidthPx");
let sharedWidth: number | null = preferredWidth;
const listeners = new Set<() => void>();

function persistWidth(value: number | null) {
  preferredWidth = value;
  writePanelSizePreference("pushPanelWidthPx", value);
}

function setSharedWidthRaw(value: number | null, persist = false) {
  if (value === sharedWidth) return;
  sharedWidth = value;
  if (persist) persistWidth(value);
  for (const l of listeners) l();
}

function setSharedWidth(
  next: number | null | ((prev: number | null) => number | null),
  persist = false,
) {
  setSharedWidthRaw(typeof next === "function" ? next(sharedWidth) : next, persist);
}

/** Snapshot the current shared width to storage (called once at drag end). */
function persistSharedWidth() {
  persistWidth(sharedWidth);
}

function subscribeSharedWidth(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

function getSharedWidthSnapshot(): number | null {
  return sharedWidth;
}

function getSharedWidthServerSnapshot(): number | null {
  return null;
}

/** Reset module-level width state from localStorage. Only for tests. */
export function resetSharedWidthStoreForTesting(): void {
  preferredWidth = readPanelSizePreference("pushPanelWidthPx");
  setSharedWidthRaw(preferredWidth);
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
  const { coarsePrimary } = useInputCapabilities();
  const width = useSyncExternalStore(
    subscribeSharedWidth,
    getSharedWidthSnapshot,
    getSharedWidthServerSnapshot,
  );
  const activePointerId = useRef<number | null>(null);
  const documentFallbackCleanupRef = useRef<(() => void) | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const minWidthRef = useRef(minWidthPx);
  minWidthRef.current = minWidthPx;

  // Track whether we're on desktop — only apply inline width there.
  const [isDesktop, setIsDesktop] = useState(
    () => typeof window !== "undefined" && !isMobileViewport(),
  );

  useEffect(() => {
    const mql = window.matchMedia(MD_MIN_WIDTH_QUERY);
    const handler = (e: MediaQueryListEvent) => setIsDesktop(e.matches);
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, []);

  // Re-clamp the stored width when the viewport resizes so a width
  // that was valid on a wider monitor doesn't push content off-screen
  // after shrinking the browser.
  useEffect(() => {
    function onResize() {
      // Re-derive the effective width from the persisted preference (not the
      // possibly-already-clamped live value) so widening the viewport restores
      // the user's larger choice instead of sticking at the prior clamp.
      setSharedWidth((prev) => {
        const base = preferredWidth ?? prev;
        return base !== null ? clampWidth(base, minWidthRef.current) : prev;
      });
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // When the minimum width increases (e.g. comments panel opens), ensure
  // the current shared width is at least the new minimum.
  useEffect(() => {
    setSharedWidth((prev) => {
      if (prev !== null && prev < minWidthPx) return clampWidth(prev, minWidthPx);
      return prev;
    });
  }, [minWidthPx]);

  // Initialise from viewport on first open (or if never set), respecting the minimum.
  const resolvedWidth = clampWidth(
    width ?? (typeof window !== "undefined" ? window.innerWidth * (defaultWidthVw / 100) : 600),
    minWidthPx,
  );

  const addDragOverlay = useCallback(() => {
    if (overlayRef.current || typeof document === "undefined") return;
    const element = document.createElement("div");
    element.style.cssText =
      "position:fixed;inset:0;z-index:2147483647;cursor:col-resize;background:transparent;";
    document.body.appendChild(element);
    overlayRef.current = element;
  }, []);

  const removeDragOverlay = useCallback(() => {
    overlayRef.current?.remove();
    overlayRef.current = null;
  }, []);

  const endDrag = useCallback(
    (persist: boolean) => {
      if (activePointerId.current === null) return;
      activePointerId.current = null;
      documentFallbackCleanupRef.current?.();
      documentFallbackCleanupRef.current = null;
      if (persist) persistSharedWidth();
      removeDragOverlay();
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    },
    [removeDragOverlay],
  );

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      if (!open || !isDesktop) return;
      if (activePointerId.current !== null) return;
      if (e.button !== 0) return;
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        return;
      }
      e.preventDefault();
      activePointerId.current = e.pointerId;
      const onDocumentPointerUp = (event: PointerEvent) => {
        if (event.pointerId === activePointerId.current) endDrag(true);
      };
      const onDocumentPointerCancel = (event: PointerEvent) => {
        if (event.pointerId === activePointerId.current) endDrag(false);
      };
      document.addEventListener("pointerup", onDocumentPointerUp);
      document.addEventListener("pointercancel", onDocumentPointerCancel);
      documentFallbackCleanupRef.current = () => {
        document.removeEventListener("pointerup", onDocumentPointerUp);
        document.removeEventListener("pointercancel", onDocumentPointerCancel);
      };
      addDragOverlay();
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [addDragOverlay, endDrag, open, isDesktop],
  );

  const onPointerMove = useCallback((e: React.PointerEvent<HTMLElement>) => {
    if (e.pointerId !== activePointerId.current) return;
    // Update the live width only; persist once on pointerup to avoid a
    // synchronous localStorage write per pointermove.
    setSharedWidth(clampWidth(window.innerWidth - e.clientX, minWidthRef.current));
  }, []);

  const onPointerUp = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      if (e.pointerId !== activePointerId.current) return;
      endDrag(true);
      if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
        e.currentTarget.releasePointerCapture(e.pointerId);
      }
    },
    [endDrag],
  );

  const onPointerCancel = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      if (e.pointerId !== activePointerId.current) return;
      endDrag(false);
    },
    [endDrag],
  );

  // Keyboard resize: left/right arrow keys adjust width by 20px.
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (!open || !isDesktop) return;
      const step = 20;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        setSharedWidth(
          (prev) => clampWidth((prev ?? resolvedWidth) + step, minWidthRef.current),
          true,
        );
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        setSharedWidth(
          (prev) => clampWidth((prev ?? resolvedWidth) - step, minWidthRef.current),
          true,
        );
      }
    },
    [open, isDesktop, resolvedWidth],
  );

  useEffect(() => () => endDrag(false), [endDrag]);

  useEffect(() => {
    if (!open || !isDesktop) endDrag(false);
  }, [endDrag, isDesktop, open]);

  // On mobile the panel is a fixed full-screen overlay — no inline width.
  const panelWidth = isDesktop ? (open ? resolvedWidth : 0) : undefined;

  return {
    /** Pixel width to apply as an inline style (undefined on mobile). */
    panelWidth,
    /** Props to spread onto the resize handle element. */
    handleProps: {
      onPointerDown,
      onPointerMove,
      onPointerUp,
      onPointerCancel,
      onLostPointerCapture: onPointerCancel,
      onKeyDown,
      style: handleGutterStyle(coarsePrimary),
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
