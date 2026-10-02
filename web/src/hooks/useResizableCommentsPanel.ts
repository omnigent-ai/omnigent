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
// Under a narrow viewer row the panel stacks below the viewer and the same
// handle moves to the panel's TOP edge, dragging its height instead
// (`containerRef.bottom - clientY`).
//
// Sizes are kept in module-level stores so the chosen size survives
// the panel unmounting when comments are toggled off or a different
// file is opened, matching the other panel-resize hooks. Explicit user
// resizes are also persisted so a full page reload restores them.

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import {
  readPanelSizePreference,
  writePanelSizePreference,
  type PanelSizePreferenceKey,
} from "@/lib/panelSizePreferences";

/** Matches the `@md/viewer` container breakpoint; resolved against the root font size. */
const SIDE_BY_SIDE_MIN_REM = 28;
const KEYBOARD_STEP_PX = 20;

type Axis = "width" | "height";

// ---------------------------------------------------------------------------
// Module-level size stores (shared across panel remounts within a session)
// ---------------------------------------------------------------------------

// `preferred` mirrors the persisted user choice; `stored` is the effective
// size after clamping to the available row space. Keeping the preference in
// memory lets the resize handler re-derive the effective size from it —
// restoring the larger choice when the row grows again.
function createSizeStore(key: PanelSizePreferenceKey) {
  let preferred: number | null = readPanelSizePreference(key);
  let stored: number | null = preferred;
  const listeners = new Set<() => void>();

  function persist(value: number | null) {
    preferred = value;
    writePanelSizePreference(key, value);
  }

  function set(next: number | null | ((prev: number | null) => number | null), persistNow = false) {
    const value = typeof next === "function" ? next(stored) : next;
    if (value === stored) return;
    stored = value;
    if (persistNow) persist(value);
    for (const l of listeners) l();
  }

  return {
    subscribe(cb: () => void): () => void {
      listeners.add(cb);
      return () => {
        listeners.delete(cb);
      };
    },
    getSnapshot: () => stored,
    preferred: () => preferred,
    set,
    /** Snapshot the current size to storage (called once at drag end). */
    persistStored: () => persist(stored),
    /** Re-read the persisted preference. Only for tests. */
    reset() {
      preferred = readPanelSizePreference(key);
      set(preferred);
    },
  };
}

interface AxisConfig {
  min: number;
  max: number;
  fallback: number;
  /** Room kept for the viewer on this axis so the panel can't swallow the row. */
  viewerMin: number;
  store: ReturnType<typeof createSizeStore>;
  cursor: "col-resize" | "row-resize";
  orientation: "vertical" | "horizontal";
}

const AXES: Record<Axis, AxisConfig> = {
  // Beside the viewer: the handle is the panel's left edge.
  width: {
    min: 200,
    max: 640,
    fallback: 240, // matches the previous fixed `md:w-60`
    viewerMin: 240,
    store: createSizeStore("commentsPanelWidthPx"),
    cursor: "col-resize",
    orientation: "vertical",
  },
  // Stacked under a narrow row: the handle is the panel's top edge.
  height: {
    min: 160,
    max: 640,
    fallback: 256, // matches the previous fixed `h-64`
    viewerMin: 160,
    store: createSizeStore("commentsPanelHeightPx"),
    cursor: "row-resize",
    orientation: "horizontal",
  },
};

const KEY_STEPS: Record<string, [Axis, number]> = {
  ArrowLeft: ["width", KEYBOARD_STEP_PX],
  ArrowRight: ["width", -KEYBOARD_STEP_PX],
  ArrowUp: ["height", KEYBOARD_STEP_PX],
  ArrowDown: ["height", -KEYBOARD_STEP_PX],
};

function getServerSnapshot(): number | null {
  return null;
}

function useAxisSize(axis: Axis): number {
  const { store, min, max, fallback } = AXES[axis];
  const raw = useSyncExternalStore(store.subscribe, store.getSnapshot, getServerSnapshot);
  return Math.max(min, Math.min(raw ?? fallback, max));
}

/** Reset module-level size state from localStorage. Only for tests. */
export function resetCommentsSizeStoreForTesting(): void {
  AXES.width.store.reset();
  AXES.height.store.reset();
}

/**
 * Stack comments when the viewer row is too narrow for side-by-side panes.
 * Attach `containerRef` to the panel to measure the row and anchor drag math.
 */
export function useResizableCommentsPanel() {
  const width = useAxisSize("width");
  const height = useAxisSize("height");
  const dragging = useRef<Axis | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);

  // While dragging, a transparent full-window overlay sits above the panel so
  // the pointer stream keeps reaching the parent document. Without it, dragging
  // over a cross-origin/sandboxed iframe (e.g. the HTML preview) routes mousemove
  // /mouseup into the frame, the parent never sees mouseup, and the drag sticks.
  const addDragOverlay = useCallback((cursor: string) => {
    if (overlayRef.current || typeof document === "undefined") return;
    const el = document.createElement("div");
    el.style.cssText = `position:fixed;inset:0;z-index:2147483647;cursor:${cursor};background:transparent;`;
    document.body.appendChild(el);
    overlayRef.current = el;
  }, []);

  const removeDragOverlay = useCallback(() => {
    overlayRef.current?.remove();
    overlayRef.current = null;
  }, []);

  const [sideBySide, setSideBySide] = useState(false);
  const axis: Axis = sideBySide ? "width" : "height";

  // Clamp a candidate size to [min, dynamic max], leaving `viewerMin` for the
  // code/diff viewer beside (width) or above (height) the panel.
  const clampSize = useCallback((target: Axis, candidate: number): number => {
    const { min, max, viewerMin } = AXES[target];
    const row = containerRef.current?.parentElement?.getBoundingClientRect();
    const available =
      target === "width" ? row?.width || window.innerWidth : row?.height || window.innerHeight;
    const dynamicMax = Math.max(min, Math.min(max, available - viewerMin));
    return Math.max(min, Math.min(candidate, dynamicMax));
  }, []);

  // A rail resize changes row size without resizing the window. The row spans
  // the viewer's content box, the width the `@md/viewer` query measures.
  useLayoutEffect(() => {
    const row = containerRef.current?.parentElement;
    if (!row) return;
    const update = () => {
      const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
      setSideBySide(row.getBoundingClientRect().width >= SIDE_BY_SIDE_MIN_REM * rem);
      // Restore the preferred sizes as space returns.
      for (const target of ["width", "height"] as const) {
        const { store, fallback } = AXES[target];
        store.set(clampSize(target, store.preferred() ?? fallback));
      }
    };
    update();
    // Without ResizeObserver, fall back to re-measuring on window resizes.
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", update);
      return () => window.removeEventListener("resize", update);
    }
    const ro = new ResizeObserver(update);
    ro.observe(row);
    return () => ro.disconnect();
  }, [clampSize]);

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      dragging.current = axis;
      addDragOverlay(AXES[axis].cursor);
      document.body.style.cursor = AXES[axis].cursor;
      document.body.style.userSelect = "none";
    },
    [addDragOverlay, axis],
  );

  // Keyboard resize: left/right arrows change the width, up/down the height.
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      const step = KEY_STEPS[e.key];
      if (!step) return;
      e.preventDefault();
      const [target, delta] = step;
      const { store, fallback } = AXES[target];
      store.set((prev) => clampSize(target, (prev ?? fallback) + delta), true);
    },
    [clampSize],
  );

  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      const target = dragging.current;
      if (!target || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const candidate = target === "width" ? rect.right - e.clientX : rect.bottom - e.clientY;
      // Update the live size only; persist once on release to avoid a
      // synchronous localStorage write per mousemove.
      AXES[target].store.set(clampSize(target, candidate));
    }
    function onMouseUp() {
      const target = dragging.current;
      if (!target) return;
      dragging.current = null;
      removeDragOverlay();
      AXES[target].store.persistStored();
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
      if (dragging.current) {
        dragging.current = null;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      }
      removeDragOverlay();
    };
  }, [clampSize, removeDragOverlay]);

  return {
    /** Side-by-side width in px; the panel applies it only beside the viewer. */
    width,
    /** Stacked height in px; the panel applies it only under a narrow row. */
    height,
    /** Attach to the panel root to anchor drag math and the dynamic max. */
    containerRef,
    /** Whether the panel sits beside the viewer (width handle) or under it (height handle). */
    sideBySide,
    /** Props to spread onto the resize handle element. */
    handleProps: {
      onMouseDown,
      onKeyDown,
      role: "separator" as const,
      "aria-orientation": AXES[axis].orientation,
      "aria-label": "Resize comments panel",
      tabIndex: 0,
    },
  };
}
