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

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { readPanelSizePreference, writePanelSizePreference } from "@/lib/panelSizePreferences";

const DEFAULT_WIDTH_PX = 240; // matches the previous fixed `md:w-60`
const MIN_WIDTH_PX = 200;
const MAX_WIDTH_PX = 640;
/** Keep at least this much room for the code/diff viewer beside the panel. */
const MIN_VIEWER_PX = 240;
/** Tailwind `md` breakpoint — must track the value in tailwind.config. */
const MD_BREAKPOINT = 768;

// ---------------------------------------------------------------------------
// Module-level width store (shared across panel remounts within a session)
// ---------------------------------------------------------------------------

// `preferredWidth` mirrors the persisted user choice; `storedWidth` is the
// effective width after clamping to the available row space. Keeping the
// preference in memory lets the resize handler re-derive the effective width
// from it — restoring the larger choice when the row widens again.
let preferredWidth: number | null = readPanelSizePreference("commentsPanelWidthPx");
let storedWidth: number | null = preferredWidth;
const listeners = new Set<() => void>();

function persistWidth(value: number | null) {
  preferredWidth = value;
  writePanelSizePreference("commentsPanelWidthPx", value);
}

function setStoredWidthRaw(value: number | null, persist = false) {
  if (value === storedWidth) return;
  storedWidth = value;
  if (persist) persistWidth(value);
  for (const l of listeners) l();
}

function setStoredWidth(
  next: number | null | ((prev: number | null) => number | null),
  persist = false,
) {
  setStoredWidthRaw(typeof next === "function" ? next(storedWidth) : next, persist);
}

/** Snapshot the current width to storage (called once at drag end). */
function persistStoredWidth() {
  persistWidth(storedWidth);
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

function getSnapshot(): number | null {
  return storedWidth;
}

function getServerSnapshot(): number | null {
  return null;
}

/** Reset module-level width state from localStorage. Only for tests. */
export function resetCommentsWidthStoreForTesting(): void {
  preferredWidth = readPanelSizePreference("commentsPanelWidthPx");
  setStoredWidthRaw(preferredWidth);
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
  const raw = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  const width = Math.max(MIN_WIDTH_PX, Math.min(raw ?? DEFAULT_WIDTH_PX, MAX_WIDTH_PX));
  const dragging = useRef(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);

  // While dragging, a transparent full-window overlay sits above the panel so
  // the pointer stream keeps reaching the parent document. Without it, dragging
  // over a cross-origin/sandboxed iframe (e.g. the HTML preview) routes mousemove
  // /mouseup into the frame, the parent never sees mouseup, and the drag sticks.
  const addDragOverlay = useCallback(() => {
    if (overlayRef.current || typeof document === "undefined") return;
    const el = document.createElement("div");
    el.style.cssText =
      "position:fixed;inset:0;z-index:2147483647;cursor:col-resize;background:transparent;";
    document.body.appendChild(el);
    overlayRef.current = el;
  }, []);

  const removeDragOverlay = useCallback(() => {
    overlayRef.current?.remove();
    overlayRef.current = null;
  }, []);

  const [isDesktop, setIsDesktop] = useState(
    () => typeof window !== "undefined" && window.innerWidth >= MD_BREAKPOINT,
  );

  useEffect(() => {
    const mql = window.matchMedia(`(min-width: ${MD_BREAKPOINT}px)`);
    const handler = (e: MediaQueryListEvent) => setIsDesktop(e.matches);
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, []);

  // Clamp a candidate width to [MIN, dynamic max], leaving MIN_VIEWER_PX for
  // the sibling code/diff viewer so the panel can't swallow the whole row.
  const clampWidth = useCallback((candidate: number): number => {
    const parent = containerRef.current?.parentElement;
    const parentWidth = parent?.getBoundingClientRect().width ?? window.innerWidth;
    const max = Math.max(MIN_WIDTH_PX, Math.min(MAX_WIDTH_PX, parentWidth - MIN_VIEWER_PX));
    return Math.max(MIN_WIDTH_PX, Math.min(candidate, max));
  }, []);

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      dragging.current = true;
      addDragOverlay();
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [addDragOverlay],
  );

  // Keyboard resize: left/right arrows widen/narrow by 20px.
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      const step = 20;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        setStoredWidth((prev) => clampWidth((prev ?? DEFAULT_WIDTH_PX) + step), true);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        setStoredWidth((prev) => clampWidth((prev ?? DEFAULT_WIDTH_PX) - step), true);
      }
    },
    [clampWidth],
  );

  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      if (!dragging.current || !containerRef.current) return;
      const right = containerRef.current.getBoundingClientRect().right;
      // Update the live width only; persist once on release to avoid a
      // synchronous localStorage write per mousemove.
      setStoredWidth(clampWidth(right - e.clientX));
    }
    function onMouseUp() {
      if (!dragging.current) return;
      dragging.current = false;
      removeDragOverlay();
      persistStoredWidth();
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
      if (dragging.current) {
        dragging.current = false;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      }
      removeDragOverlay();
    };
  }, [clampWidth, removeDragOverlay]);

  // Re-clamp the stored width when the viewport resizes so a width chosen on
  // a wider layout doesn't crowd out the viewer after the window shrinks.
  useEffect(() => {
    function onResize() {
      // Re-derive the effective width from the persisted preference so the
      // panel widens back to the user's choice when the row regains space.
      setStoredWidth((prev) => {
        const base = preferredWidth ?? prev;
        return base !== null ? clampWidth(base) : prev;
      });
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [clampWidth]);

  return {
    /** Pixel width to apply as an inline style (undefined on mobile). */
    width: isDesktop ? width : undefined,
    /** Attach to the panel root to anchor drag math and the dynamic max. */
    containerRef,
    /** Whether the resize handle should render (desktop only). */
    isDesktop,
    /** Props to spread onto the resize handle element. */
    handleProps: {
      onMouseDown,
      onKeyDown,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize comments panel",
      tabIndex: 0,
    },
  };
}
