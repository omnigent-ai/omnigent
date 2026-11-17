import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  readPanelSizePreference,
  writePanelSizePreference,
  type PanelSizePreferenceKey,
} from "@/lib/panelSizePreferences";

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
// Shared width stores
// ---------------------------------------------------------------------------
// Every right-side push panel (FileViewer, TerminalsPanel,
// ExecutionLogsPanel, FilesPanelDrawer) reads and writes the same
// width via the `pushPanelWidthPx` store. Without sharing, switching
// between panels would snap the layout back to each panel's
// independent default, which feels like the chat is jumping width
// for no reason. A panel outside that group passes its own storage key.
// `null` means "not yet set — fall back to the caller's default (vw-based)".
// The first drag persists a px value.
interface WidthStore {
  /**
   * The persisted user choice. It diverges from `current` after a viewport-shrink
   * clamp, so the resize handler can restore the larger choice when space returns
   * without reading localStorage on every resize event.
   */
  preferred: number | null;
  /** The effective (viewport-clamped) width that drives layout. */
  current: number | null;
  listeners: Set<() => void>;
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => number | null;
}

const widthStores = new Map<PanelSizePreferenceKey, WidthStore>();

function widthStore(key: PanelSizePreferenceKey): WidthStore {
  const existing = widthStores.get(key);
  if (existing) return existing;
  const preferred = readPanelSizePreference(key);
  const store: WidthStore = {
    preferred,
    current: preferred,
    listeners: new Set(),
    subscribe: (listener) => {
      store.listeners.add(listener);
      return () => {
        store.listeners.delete(listener);
      };
    },
    getSnapshot: () => store.current,
  };
  widthStores.set(key, store);
  return store;
}

function persistWidth(key: PanelSizePreferenceKey, value: number | null) {
  widthStore(key).preferred = value;
  writePanelSizePreference(key, value);
}

function setWidthRaw(key: PanelSizePreferenceKey, value: number | null, persist = false) {
  const store = widthStore(key);
  if (value === store.current) return;
  store.current = value;
  if (persist) persistWidth(key, value);
  for (const listener of store.listeners) listener();
}

function setWidth(
  key: PanelSizePreferenceKey,
  next: number | null | ((prev: number | null) => number | null),
  persist = false,
) {
  setWidthRaw(key, typeof next === "function" ? next(widthStore(key).current) : next, persist);
}

/** Snapshot the current width to storage (called once at drag end). */
function persistCurrentWidth(key: PanelSizePreferenceKey) {
  persistWidth(key, widthStore(key).current);
}

function getServerWidthSnapshot(): number | null {
  return null;
}

/** Reset module-level width state from localStorage. Only for tests. */
export function resetSharedWidthStoreForTesting(): void {
  for (const key of new Set<PanelSizePreferenceKey>(["pushPanelWidthPx", ...widthStores.keys()])) {
    widthStore(key).preferred = readPanelSizePreference(key);
    setWidthRaw(key, widthStore(key).preferred);
  }
}

/**
 * Hook for making a right-side panel resizable via mouse drag on its left edge.
 *
 * On desktop (`≥ md`) the panel width is controlled via an inline style
 * driven by drag state. On mobile (`< md`) the panel is a full-screen
 * overlay — the hook returns `undefined` so no inline width is set.
 *
 * The width is stored at module scope and shared across every caller with
 * the same `storageKey`, so all right-side push panels resize together — the
 * layout stays stable as the user switches between FileViewer / Terminals /
 * Logs / Files.
 *
 * Returns the current pixel width (or undefined on mobile) and a set of
 * props to spread onto the resize handle element.
 */
export function useResizablePanel(
  open: boolean,
  defaultWidthVw = 50,
  minWidthPx = MIN_WIDTH_PX,
  storageKey: PanelSizePreferenceKey = "pushPanelWidthPx",
) {
  const store = widthStore(storageKey);
  const width = useSyncExternalStore(store.subscribe, store.getSnapshot, getServerWidthSnapshot);
  const storageKeyRef = useRef(storageKey);
  storageKeyRef.current = storageKey;
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
      const key = storageKeyRef.current;
      setWidth(key, (prev) => {
        const base = widthStore(key).preferred ?? prev;
        return base !== null ? clampWidth(base, minWidthRef.current) : prev;
      });
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // When the minimum width increases (e.g. comments panel opens), ensure
  // the current shared width is at least the new minimum.
  useEffect(() => {
    setWidth(storageKey, (prev) => {
      if (prev !== null && prev < minWidthPx) return clampWidth(prev, minWidthPx);
      return prev;
    });
  }, [minWidthPx, storageKey]);

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
        setWidth(
          storageKey,
          (prev) => clampWidth((prev ?? resolvedWidth) + step, minWidthRef.current),
          true,
        );
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        setWidth(
          storageKey,
          (prev) => clampWidth((prev ?? resolvedWidth) - step, minWidthRef.current),
          true,
        );
      }
    },
    [open, isDesktop, resolvedWidth, storageKey],
  );

  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      if (!dragging.current) return;
      // Update the live width only; persisting on every move would fire a
      // synchronous localStorage write per mousemove. We snapshot once on release.
      setWidth(
        storageKeyRef.current,
        clampWidth(window.innerWidth - e.clientX, minWidthRef.current),
      );
    }

    function onMouseUp() {
      if (!dragging.current) return;
      dragging.current = false;
      persistCurrentWidth(storageKeyRef.current);
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
