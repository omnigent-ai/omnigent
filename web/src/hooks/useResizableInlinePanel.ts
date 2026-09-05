// Resize hook for the always-visible right inline panel (the aside in
// AppShell that holds FilesPanel + SessionRail). Uses a separate
// module-level width store so the inline panel's preferred width doesn't
// bleed into the push-panel store shared by FileViewer / TerminalsPanel /
// ExecutionLogsPanel / FilesPanelDrawer — those open at ~50 % by default
// while the inline panel starts at a compact sidebar width.

import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

const MIN_WIDTH_PX = 240;
const MAX_WIDTH_RATIO = 0.99;
/** The center chat column never shrinks past this, whatever the rail wants. */
const CHAT_MIN_WIDTH_PX = 480;
/** Visual gap between the chat column and the rail. */
const GAP_PX = 8;

// ~36 % of viewport, clamped [420, 600] — ~30 % wider than the prior default so
// the first manual open lands at a comfortable working width.
const DEFAULT_RATIO = 0.36;
const DEFAULT_MIN_PX = 420;
const DEFAULT_MAX_PX = 600;
const DEFAULT_SSR_PX = 500;

function defaultWidthPx(): number {
  if (typeof window === "undefined") return DEFAULT_SSR_PX;
  const candidate = Math.round(window.innerWidth * DEFAULT_RATIO);
  return Math.max(DEFAULT_MIN_PX, Math.min(DEFAULT_MAX_PX, candidate));
}

// Width the panel may not eat into: the sidebar when it's open. Passed down
// from AppShell so opening the sidebar tightens the ceiling instead of
// squeezing the chat.
function clamp(w: number, minPx = MIN_WIDTH_PX, reservedPx = 0): number {
  // No viewport ceiling available off the DOM (SSR / node test env) — this runs
  // during render, so guard before reading `window` to avoid a hard throw.
  if (typeof window === "undefined") return Math.max(minPx, w);
  // 99vw is the nominal ceiling, but the chat column's minimum (plus the gap
  // between the two) is the one that actually binds on a normal desktop.
  const ceiling = Math.max(
    0,
    Math.min(
      window.innerWidth * MAX_WIDTH_RATIO,
      window.innerWidth - reservedPx - CHAT_MIN_WIDTH_PX - GAP_PX,
    ),
  );
  // The chat's 480px floor wins over the panel's own comfort minimum: when the
  // viewport (with the sidebar open) is too small to grant both, the panel
  // yields below `minPx` rather than let the chat break its minimum. Clamping
  // the floor to the ceiling keeps the range valid so `Math.max` can't push the
  // width back up past the chat-preserving cap.
  return Math.max(Math.min(minPx, ceiling), Math.min(w, ceiling));
}

// ---------------------------------------------------------------------------
// Module-level width store (independent of the push-panel store)
// ---------------------------------------------------------------------------

// `preferredWidth` mirrors the persisted user choice; `storedWidth` is the
// effective (viewport-clamped) width. Keeping the preference in
// memory lets the resize handler re-derive the effective width from it —
// restoring the larger choice when space returns — without touching disk.
// Both start null: the active session's saved width is loaded once the hook
// learns its storage key (see loadSession), since width is scoped by the caller.
let currentSessionId: string | null = null;
let preferredWidth: number | null = null;
let storedWidth: number | null = null;
const listeners = new Set<() => void>();

function persistWidth(value: number | null) {
  preferredWidth = value;
  if (currentSessionId !== null && value !== null) {
    writeSessionWorkspaceState(currentSessionId, { widthPx: value });
  }
}

function setStoredWidthRaw(value: number | null, persist = false) {
  if (value === storedWidth) return;
  storedWidth = value;
  if (persist) persistWidth(value);
  for (const l of listeners) l();
}

function setStoredWidth(next: number | ((prev: number | null) => number), persist = false) {
  setStoredWidthRaw(typeof next === "function" ? next(storedWidth) : next, persist);
}

/** Snapshot the current width to storage (called once at drag end). */
function persistStoredWidth() {
  persistWidth(storedWidth);
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

// Re-seed the module store from a storage key's saved width. A key with no
// saved width falls back to the viewport-derived default.
function loadSession(sessionId: string | null): void {
  if (sessionId === currentSessionId) return;
  currentSessionId = sessionId;
  preferredWidth =
    sessionId !== null ? (readSessionWorkspaceState(sessionId).widthPx ?? null) : null;
  setStoredWidthRaw(preferredWidth);
}

/** Reset all module-level state. Only for use in tests. */
export function resetWidthStoreForTesting(): void {
  currentSessionId = null;
  preferredWidth = null;
  setStoredWidthRaw(null);
}

function getSnapshot(): number | null {
  return storedWidth;
}

function getServerSnapshot(): number | null {
  return null;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/**
 * Makes the always-visible right inline panel (AppShell aside) resizable via
 * a drag handle on its left edge. Uses its own width store so resizing the
 * inline panel doesn't disturb the push-panel widths (TerminalsPanel etc.).
 *
 * Returns the current pixel width and handle props to spread onto the resize
 * handle element. Intended for desktop-only use — callers should not render
 * the handle on mobile.
 *
 * `sessionId` scopes the persisted width. AppShell passes the root session so
 * one agent tree shares a rail width. Pass `null` when there is no active
 * conversation (the panel then uses the default width and resizes are not
 * persisted).
 *
 * `reservedPx` is layout width the panel may not claim — the open sidebar. It
 * tightens the ceiling without touching the persisted preference, so opening
 * the sidebar temporarily shrinks the panel and collapsing it restores the
 * user's width.
 *
 * `persistEnabled` disables manual resizing while the storage key is tentative.
 */
export function useResizableInlinePanel(
  sessionId: string | null,
  minWidthPx = MIN_WIDTH_PX,
  reservedPx = 0,
  persistEnabled = true,
) {
  const raw = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  // On a session switch the module store still holds the previous session's
  // width until the effect below re-seeds it after commit. Derive this render's
  // width straight from the incoming session's saved value so the panel doesn't
  // flash the old width for a frame. Once the effect runs `currentSessionId`
  // catches up and we fall back to the live store value (which the drag and
  // keyboard handlers mutate in place).
  let effectiveRaw = raw;
  if (sessionId !== currentSessionId) {
    effectiveRaw =
      sessionId !== null ? (readSessionWorkspaceState(sessionId).widthPx ?? null) : null;
  }
  // Clamped at render time only — the store keeps the user's preferred width, so
  // a temporary squeeze (sidebar opening) is undone when the space returns.
  const resolvedWidth = clamp(effectiveRaw ?? defaultWidthPx(), minWidthPx, reservedPx);
  // Drives the drag listeners' lifecycle: they mount only while a drag is
  // live, so there's no idle window-level mousemove handler firing during
  // ordinary page use.
  const [isDragging, setIsDragging] = useState(false);
  // `resolvedWidth` reads `window.innerWidth` at render, but a viewport resize
  // that leaves the stored (no-reserve) width unchanged wouldn't otherwise
  // re-render — so the render-time reserve clamp would go stale and the chat
  // could dip below its minimum on a shrink. This tick forces a recompute on
  // every resize regardless of whether the stored width moved.
  const [, bumpViewport] = useReducer((n: number) => n + 1, 0);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const minWidthRef = useRef(minWidthPx);
  minWidthRef.current = minWidthPx;
  const reservedRef = useRef(reservedPx);
  reservedRef.current = reservedPx;
  const persistEnabledRef = useRef(persistEnabled);
  persistEnabledRef.current = persistEnabled;

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

  // Load the active session's saved width into the module store (and re-load
  // when it changes) so the live store and the drag handlers operate on the
  // right session.
  useEffect(() => {
    loadSession(sessionId);
  }, [sessionId]);

  // Re-clamp on viewport resize so the panel can't overflow a shrunken window.
  // Re-derive the effective width from the persisted preference so widening the
  // window restores the user's saved choice. Deliberately clamped WITHOUT
  // `reservedPx`: the store holds the preferred width, and the reserve is
  // applied at render time, so an open sidebar's squeeze is never written in
  // (collapsing it restores this width).
  useEffect(() => {
    function onResize() {
      setStoredWidth((prev) => {
        const base = preferredWidth ?? prev;
        return base !== null ? clamp(base, minWidthRef.current) : defaultWidthPx();
      });
      // Force a re-render even when the stored width is unchanged, so the
      // render-time reserve clamp re-runs against the new viewport.
      bumpViewport();
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // The resolvedWidth formula already enforces the visual minimum. No effect
  // needed — this lets the panel shrink back when minWidthPx drops.

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (!persistEnabled) return;
      e.preventDefault();
      setIsDragging(true);
      addDragOverlay();
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [addDragOverlay, persistEnabled],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (!persistEnabled) return;
      const step = 20;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        setStoredWidth(
          (prev) => clamp((prev ?? resolvedWidth) + step, minWidthRef.current, reservedRef.current),
          true,
        );
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        setStoredWidth(
          (prev) => clamp((prev ?? resolvedWidth) - step, minWidthRef.current, reservedRef.current),
          true,
        );
      }
    },
    [persistEnabled, resolvedWidth],
  );

  // Drag listeners live only while a drag is active — no idle window-level
  // mousemove handler during ordinary use. Moves are coalesced through a
  // single rAF so a burst of mousemove events yields at most one width update
  // per frame (setStoredWidth already dedupes equal values).
  useEffect(() => {
    if (!isDragging) return;
    let frame = 0;
    let pending: number | null = null;

    function flush() {
      frame = 0;
      if (pending === null || !persistEnabledRef.current) return;
      setStoredWidth(clamp(pending, minWidthRef.current, reservedRef.current));
      pending = null;
    }

    function onMouseMove(e: MouseEvent) {
      pending = window.innerWidth - e.clientX;
      if (frame === 0) frame = requestAnimationFrame(flush);
    }

    function stop() {
      if (frame !== 0) cancelAnimationFrame(frame);
      flush();
      setIsDragging(false);
      removeDragOverlay();
      if (persistEnabledRef.current) persistStoredWidth();
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }

    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", stop);
    return () => {
      if (frame !== 0) cancelAnimationFrame(frame);
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", stop);
      // Unmounted mid-drag (panel closed via tab switch): reset the cursor and
      // drop the overlay so it can't swallow later clicks.
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      removeDragOverlay();
    };
  }, [isDragging, removeDragOverlay]);

  // Stable identity so consumers memoized on this prop (WorkspacePanel) don't
  // re-render on every parent render — the object only changes when its inputs do.
  const handleProps = useMemo(
    () => ({
      onMouseDown,
      onKeyDown,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize panel",
      "aria-disabled": !persistEnabled,
      tabIndex: persistEnabled ? 0 : -1,
    }),
    [onMouseDown, onKeyDown, persistEnabled],
  );

  return { panelWidth: resolvedWidth, handleProps };
}
