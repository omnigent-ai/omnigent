// Resize hook for the always-visible right inline panel (the aside in
// AppShell that holds FilesPanel + SessionRail). Uses a separate
// module-level width store so the inline panel's preferred width doesn't
// bleed into the push-panel store shared by FileViewer / TerminalsPanel /
// ExecutionLogsPanel / FilesPanelDrawer — those open at ~50 % by default
// while the inline panel starts at a compact sidebar width.

import { useCallback, useEffect, useRef } from "react";
import { useSyncExternalStore } from "react";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

const MIN_WIDTH_PX = 240;
const MAX_WIDTH_RATIO = 0.6;

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

function clamp(w: number, minPx = MIN_WIDTH_PX): number {
  // No viewport ceiling available off the DOM (SSR / node test env) — this runs
  // during render, so guard before reading `window` to avoid a hard throw.
  if (typeof window === "undefined") return Math.max(minPx, w);
  return Math.max(minPx, Math.min(w, window.innerWidth * MAX_WIDTH_RATIO));
}

// ---------------------------------------------------------------------------
// Module-level width store (independent of the push-panel store)
// ---------------------------------------------------------------------------

// `preferredWidth` mirrors the persisted user choice; `storedWidth` is the
// effective (viewport-clamped) width. Keeping the preference in
// memory lets the resize handler re-derive the effective width from it —
// restoring the larger choice when space returns — without touching disk.
// Both start null: the active session's saved width is loaded once the hook
// learns its conversationId (see loadSession), since the width is per-session.
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

// Re-seed the module store from a session's saved width. Called when the
// active conversation changes so each session restores its own width (and a
// session with no saved width falls back to the viewport-derived default).
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
 * `sessionId` scopes the persisted width: each conversation remembers its own
 * rail width. Pass `null` when there is no active conversation (the panel then
 * uses the default width and resizes are not persisted).
 */
export function useResizableInlinePanel(sessionId: string | null, minWidthPx = MIN_WIDTH_PX) {
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
  const resolvedWidth = clamp(effectiveRaw ?? defaultWidthPx(), minWidthPx);
  const dragging = useRef(false);
  const minWidthRef = useRef(minWidthPx);
  minWidthRef.current = minWidthPx;

  // Load the active session's saved width into the module store (and re-load
  // when it changes) so the live store and the drag handlers operate on the
  // right session.
  useEffect(() => {
    loadSession(sessionId);
  }, [sessionId]);

  // Re-clamp on viewport resize so the panel can't overflow a shrunken window.
  // Re-derive the effective width from the persisted preference so widening the
  // window restores the user's saved choice.
  useEffect(() => {
    function onResize() {
      setStoredWidth((prev) => {
        const base = preferredWidth ?? prev;
        return base !== null ? clamp(base, minWidthRef.current) : defaultWidthPx();
      });
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // The resolvedWidth formula already enforces the visual minimum. No effect
  // needed — this lets the panel shrink back when minWidthPx drops.

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      const step = 20;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        setStoredWidth((prev) => clamp((prev ?? resolvedWidth) + step, minWidthRef.current), true);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        setStoredWidth((prev) => clamp((prev ?? resolvedWidth) - step, minWidthRef.current), true);
      }
    },
    [resolvedWidth],
  );

  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      if (!dragging.current) return;
      // Update the live width only; persist once on release to avoid a
      // synchronous localStorage write per mousemove.
      setStoredWidth(clamp(window.innerWidth - e.clientX, minWidthRef.current));
    }

    function onMouseUp() {
      if (!dragging.current) return;
      dragging.current = false;
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
    };
  }, []);

  return {
    panelWidth: resolvedWidth,
    handleProps: {
      onMouseDown,
      onKeyDown,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize panel",
      tabIndex: 0,
    },
  };
}
