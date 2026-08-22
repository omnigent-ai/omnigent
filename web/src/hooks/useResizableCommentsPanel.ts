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
import { useInputCapabilities } from "@/hooks/useInputCapabilities";
import { MD_MIN_WIDTH_QUERY, isMobileViewport } from "@/lib/breakpoints";
import { readPanelSizePreference, writePanelSizePreference } from "@/lib/panelSizePreferences";

const DEFAULT_WIDTH_PX = 240; // matches the previous fixed `md:w-60`
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
  const { coarsePrimary } = useInputCapabilities();
  const raw = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  const width = Math.max(MIN_WIDTH_PX, Math.min(raw ?? DEFAULT_WIDTH_PX, MAX_WIDTH_PX));
  // Pointer id of the active drag; null when idle. A second concurrent
  // pointer (e.g. another finger) is ignored — first pointer wins.
  const activePointerId = useRef<number | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  // Removes the document-level pointerup/pointercancel fallbacks installed
  // for the active drag; null when idle.
  const removeDocFallbacks = useRef<(() => void) | null>(null);

  // While dragging, a transparent full-window overlay sits above the panel so
  // the pointer stream keeps reaching the parent document even if capture is
  // lost. Without it, dragging over a cross-origin/sandboxed iframe (e.g. the
  // HTML preview) routes moves into the frame and the drag sticks.
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
    () => typeof window !== "undefined" && !isMobileViewport(),
  );

  useEffect(() => {
    const mql = window.matchMedia(MD_MIN_WIDTH_QUERY);
    const handler = (e: MediaQueryListEvent) => setIsDesktop(e.matches);
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, []);

  // Coarse pointers get the deliberate capped 26px target; fine pointers
  // (mouse, trackpad, pen tip) get a 24px target.

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
  const [, setConstraintVersion] = useState(0);
  useEffect(() => {
    const parent = containerRef.current?.parentElement;
    if (!parent || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      setStoredWidth((prev) => {
        const base = preferredWidth ?? prev;
        return base !== null ? clampWidth(base) : prev;
      });
      setConstraintVersion((version) => version + 1);
    });
    observer.observe(parent);
    return () => observer.disconnect();
  }, [clampWidth]);

  // Ends the drag at the last applied width (never a half-state): clears the
  // active pointer, drops the overlay, and restores the body cursor/selection.
  // Only a deliberate release persists; aborts (cancel, capture loss, unmount)
  // keep the width on screen but don't write storage. Idempotent so pointerup
  // + the lostpointercapture it triggers don't double-run.
  const endDrag = useCallback(
    (persist: boolean) => {
      if (activePointerId.current === null) return;
      activePointerId.current = null;
      removeDocFallbacks.current?.();
      removeDocFallbacks.current = null;
      removeDragOverlay();
      if (persist) persistStoredWidth();
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    },
    [removeDragOverlay],
  );

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      // First pointer wins; only the primary button/tip starts a drag. (A pen
      // barrel button reports pointerType "pen" with button 2, so the guard
      // must not be mouse-only; touch and pen tip are always button 0.)
      if (activePointerId.current !== null) return;
      if (e.button !== 0) return;
      // Capture BEFORE publishing any drag state: if capture throws (pointer
      // already gone, detached node), staying fully idle avoids a stale
      // activePointerId that a later reused pointerId could match — which
      // would spuriously end (and persist) a drag that never started.
      try {
        e.currentTarget.setPointerCapture?.(e.pointerId); // jsdom lacks capture
      } catch {
        return;
      }
      e.preventDefault();
      activePointerId.current = e.pointerId;
      // Document-level fallbacks: if the browser drops capture without
      // delivering the handle's up/cancel, the drag still ends here so the
      // max-z overlay can never outlive it.
      const onDocPointerUp = (ev: PointerEvent) => {
        if (ev.pointerId === activePointerId.current) endDrag(true);
      };
      const onDocPointerCancel = (ev: PointerEvent) => {
        if (ev.pointerId === activePointerId.current) endDrag(false);
      };
      document.addEventListener("pointerup", onDocPointerUp);
      document.addEventListener("pointercancel", onDocPointerCancel);
      removeDocFallbacks.current = () => {
        document.removeEventListener("pointerup", onDocPointerUp);
        document.removeEventListener("pointercancel", onDocPointerCancel);
      };
      addDragOverlay();
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [addDragOverlay, endDrag],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (e.pointerId !== activePointerId.current || !containerRef.current) return;
      const right = containerRef.current.getBoundingClientRect().right;
      // Update the live width only; persist once on release to avoid a
      // synchronous localStorage write per move.
      setStoredWidth(clampWidth(right - e.clientX));
    },
    [clampWidth],
  );

  const onPointerUp = useCallback(
    (e: React.PointerEvent) => {
      if (e.pointerId !== activePointerId.current) return;
      if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
        e.currentTarget.releasePointerCapture(e.pointerId);
      }
      endDrag(true);
    },
    [endDrag],
  );

  // pointercancel (e.g. the browser reclaims the touch) and capture loss
  // both abort cleanly to the last applied width, without persisting it.
  const onPointerCancel = useCallback(
    (e: React.PointerEvent) => {
      if (e.pointerId !== activePointerId.current) return;
      endDrag(false);
    },
    [endDrag],
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

  // Unmount mid-drag: abort (no persist) and clean up body/overlay state.
  useEffect(() => () => endDrag(false), [endDrag]);

  // The layout flipping to mobile mid-drag unmounts the handle, so its
  // up/cancel can never arrive — abort so the overlay doesn't outlive the drag.
  useEffect(() => {
    if (!isDesktop) endDrag(false);
  }, [isDesktop, endDrag]);

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
    /**
     * Props to spread onto the divider-gutter handle. Render it as the
     * panel's PRECEDING SIBLING in the split row (a `w-1 shrink-0` flex
     * child), never inside either scroll container — the pads would be
     * clipped and would steal the neighbors' pointer streams.
     */
    handleProps: {
      onPointerDown,
      onPointerMove,
      onPointerUp,
      onPointerCancel,
      onLostPointerCapture: onPointerCancel,
      onKeyDown,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize comments panel",
      "aria-valuenow": width,
      "aria-valuemin": MIN_WIDTH_PX,
      "aria-valuemax": reachableMax(),
      tabIndex: 0,
      // The gutter owns its touches outright (no scroll/selection may start
      // from it). With content-box sizing the `w-1` class is the painted
      // strip; padding centers it in the gutter and adds the overhang
      // slivers, whose footprint the negative margins cancel — so the
      // element occupies exactly the gutter width and the hover/active
      // background (content-box clipped) never widens visually.
      style: gutterStyle(coarsePrimary),
    },
  };
}
