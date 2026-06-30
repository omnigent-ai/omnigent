import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { readPanelSizePreference, writePanelSizePreference } from "@/lib/panelSizePreferences";

const MIN_WIDTH_PX = 320;
const MAX_WIDTH_RATIO = 0.8; // 80% of viewport
/** Tailwind `md` breakpoint — must track the value in tailwind.config. */
const MD_BREAKPOINT = 768;

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
 * Hook for making a right-side panel resizable via mouse drag on its left edge.
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
  const width = useSyncExternalStore(
    subscribeSharedWidth,
    getSharedWidthSnapshot,
    getSharedWidthServerSnapshot,
  );
  const dragging = useRef(false);
  const minWidthRef = useRef(minWidthPx);
  minWidthRef.current = minWidthPx;

  // Track whether we're on desktop — only apply inline width there.
  const [isDesktop, setIsDesktop] = useState(
    () => typeof window !== "undefined" && window.innerWidth >= MD_BREAKPOINT,
  );

  useEffect(() => {
    const mql = window.matchMedia(`(min-width: ${MD_BREAKPOINT}px)`);
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

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (!open || !isDesktop) return;
      e.preventDefault();
      dragging.current = true;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [open, isDesktop],
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

  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      if (!dragging.current) return;
      // Update the live width only; persisting on every move would fire a
      // synchronous localStorage write per mousemove. We snapshot once on release.
      setSharedWidth(clampWidth(window.innerWidth - e.clientX, minWidthRef.current));
    }

    function onMouseUp() {
      if (!dragging.current) return;
      dragging.current = false;
      persistSharedWidth();
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }

    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
      // Reset body styles if unmounted mid-drag (e.g. panel closed
      // via Escape while dragging).
      if (dragging.current) {
        dragging.current = false;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      }
    };
  }, []);

  // On mobile the panel is a fixed full-screen overlay — no inline width.
  const panelWidth = isDesktop ? (open ? resolvedWidth : 0) : undefined;

  return {
    /** Pixel width to apply as an inline style (undefined on mobile). */
    panelWidth,
    /** Props to spread onto the resize handle element. */
    handleProps: {
      onMouseDown,
      onKeyDown,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize panel",
      tabIndex: 0,
    },
    /** Whether the resize handle should be visible (desktop only). */
    isDesktop,
  };
}
