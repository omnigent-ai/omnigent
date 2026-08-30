// Resize hook for the always-visible right inline panel (the aside in
// AppShell that holds FilesPanel + SessionRail). Uses a separate
// module-level width store so the inline panel's preferred width doesn't
// bleed into the push-panel store shared by FileViewer / TerminalsPanel /
// ExecutionLogsPanel / FilesPanelDrawer — those open at ~50 % by default
// while the inline panel starts at a compact sidebar width.

import { useCallback, useEffect, useReducer, useRef } from "react";
import { createResizableWidthStore, useResizableWidthSnapshot } from "@/hooks/resizableWidthStore";
import { useInputCapabilities } from "@/hooks/useInputCapabilities";
import { useResizeDrag } from "@/hooks/useResizeDrag";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

const MIN_WIDTH_PX = 240;
const MAX_WIDTH_RATIO = 0.99;
/** Comfortable chat-column minimum — held whenever the row has room for it. */
const CHAT_MIN_WIDTH_PX = 480;
/** Hard chat floor on cramped (tablet-width) rows; the chat never goes lower. */
const CHAT_HARD_MIN_WIDTH_PX = 240;
/** Drag travel the rail keeps above its own floor before the chat stops ceding. */
const MIN_DRAG_RANGE_PX = 120;
// The handle is a dedicated flex gutter between chat and panel, outside both
// scroll containers. The painted `w-1` strip is centered in a real layout
// gutter, with tightly bounded overhangs that avoid owning either surface.
const PAINTED_STRIP_PX = 4;
const COARSE_GUTTER_PX = 12;
const FINE_GUTTER_PX = 10;
const CHAT_SLIVER_PX = 6;
const PANEL_SLIVER_PX = 8;

function gutterStyle(isCoarse: boolean): React.CSSProperties {
  const gutter = isCoarse ? COARSE_GUTTER_PX : FINE_GUTTER_PX;
  const inset = (gutter - PAINTED_STRIP_PX) / 2;
  return {
    touchAction: "none",
    boxSizing: "content-box",
    paddingLeft: CHAT_SLIVER_PX + inset,
    paddingRight: PANEL_SLIVER_PX + inset,
    marginLeft: -CHAT_SLIVER_PX,
    marginRight: -PANEL_SLIVER_PX,
    backgroundClip: "content-box",
  };
}

// Default to ~36% of the viewport, clamped to a comfortable working width.
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
  // Reserve the largest possible gutter footprint so a pointer-capability
  // change cannot pull the chat below its floor.
  const available = window.innerWidth - reservedPx - COARSE_GUTTER_PX;
  // The chat holds its comfortable minimum only while the row also fits the
  // rail's floor plus a usable drag range. On tablet-width rows (an unfolded
  // foldable with the sidebar open) reserving the full 480px pinned the clamp's
  // floor onto its ceiling — every drag computed the same width — so the chat
  // cedes down to its hard floor before the rail loses its travel.
  const chatReserve = Math.min(
    CHAT_MIN_WIDTH_PX,
    Math.max(CHAT_HARD_MIN_WIDTH_PX, available - minPx - MIN_DRAG_RANGE_PX),
  );
  // 99vw is the nominal ceiling, but the chat reservation (plus the gap
  // between the two) is the one that actually binds on a normal desktop.
  const ceiling = Math.max(
    0,
    Math.min(window.innerWidth * MAX_WIDTH_RATIO, available - chatReserve),
  );
  // The chat's hard floor wins over the panel's own comfort minimum: when the
  // viewport (with the sidebar open) is too small to grant both, the panel
  // yields below `minPx` rather than let the chat break its floor. Clamping
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
const widthStore = createResizableWidthStore(null, (value) => {
  if (currentSessionId !== null && value !== null) {
    writeSessionWorkspaceState(currentSessionId, { widthPx: value });
  }
});

// Re-seed the module store from a storage key's saved width. A key with no
// saved width falls back to the viewport-derived default.
function loadSession(sessionId: string | null): void {
  if (sessionId === currentSessionId) return;
  currentSessionId = sessionId;
  widthStore.reset(
    sessionId !== null ? (readSessionWorkspaceState(sessionId).widthPx ?? null) : null,
  );
}

/** Reset all module-level state. Only for use in tests. */
export function resetWidthStoreForTesting(): void {
  currentSessionId = null;
  widthStore.reset(null);
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
 * handle element. Drag uses pointer events with capture so touch/stylus work
 * the same as mouse. Callers should not render the handle on mobile.
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
 * `enabled` disables manual resizing while the panel is hidden. `persistEnabled`
 * separately disables interaction while the storage key is tentative.
 */
export function useResizableInlinePanel(
  sessionId: string | null,
  minWidthPx = MIN_WIDTH_PX,
  reservedPx = 0,
  enabled = true,
  persistEnabled = true,
) {
  const { anyCoarse } = useInputCapabilities();
  const raw = useResizableWidthSnapshot(widthStore);
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
  // `resolvedWidth` reads `window.innerWidth` at render, but a viewport resize
  // that leaves the stored (no-reserve) width unchanged wouldn't otherwise
  // re-render — so the render-time reserve clamp would go stale and the chat
  // could dip below its minimum on a shrink. This tick forces a recompute on
  // every resize regardless of whether the stored width moved.
  const [, bumpViewport] = useReducer((n: number) => n + 1, 0);
  const minWidthRef = useRef(minWidthPx);
  minWidthRef.current = minWidthPx;
  const reservedRef = useRef(reservedPx);
  reservedRef.current = reservedPx;

  // Re-clamp on viewport resize so the panel can't overflow a shrunken window.
  // Re-derive the effective width from the persisted preference so widening the
  // window restores the user's saved choice. Deliberately clamped WITHOUT
  // `reservedPx`: the store holds the preferred width, and the reserve is
  // applied at render time, so an open sidebar's squeeze is never written in
  // (collapsing it restores this width).
  useEffect(() => {
    function onResize() {
      widthStore.set((prev) => {
        const base = widthStore.getPreferred() ?? prev;
        return base !== null ? clamp(base, minWidthRef.current) : defaultWidthPx();
      });
      // Force a re-render even when the stored width is unchanged, so the
      // render-time reserve clamp re-runs against the new viewport.
      bumpViewport();
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const resizeEnabled = enabled && persistEnabled && resolvedWidth !== 0;
  // Cancellation restores the pre-drag width: onMove writes the live store on
  // every pointermove, so an abort (Escape, blur, session switch) must undo
  // those writes.
  const dragStartWidth = useRef<number | null>(null);
  const resizeDrag = useResizeDrag({
    enabled: resizeEnabled,
    overlay: true,
    onStart: useCallback(() => {
      dragStartWidth.current = widthStore.getSnapshot();
    }, []),
    onCancel: useCallback(() => {
      widthStore.set(dragStartWidth.current);
    }, []),
    onCommit: widthStore.persist,
    onMove: useCallback((e: React.PointerEvent<HTMLElement>) => {
      widthStore.set(
        clamp(window.innerWidth - e.clientX, minWidthRef.current, reservedRef.current),
      );
    }, []),
  });
  const cancelResizeDrag = resizeDrag.cancelDrag;

  // A session switch removes the old panel identity even when the next
  // session also renders a rail. Abort before re-seeding so a late pointerup
  // cannot persist the old drag into the new conversation.
  useEffect(() => {
    cancelResizeDrag();
    loadSession(sessionId);
  }, [cancelResizeDrag, sessionId]);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (!resizeEnabled) return;
      const step = 20;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        widthStore.set(
          (prev) => clamp((prev ?? resolvedWidth) + step, minWidthRef.current, reservedRef.current),
          true,
        );
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        widthStore.set(
          (prev) => clamp((prev ?? resolvedWidth) - step, minWidthRef.current, reservedRef.current),
          true,
        );
      }
    },
    [resizeEnabled, resolvedWidth],
  );
  return {
    panelWidth: resolvedWidth,
    handleProps: {
      ...resizeDrag.handleProps,
      onKeyDown,
      style: gutterStyle(anyCoarse),
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize panel",
      "aria-valuenow": resolvedWidth,
      "aria-valuemin": Math.min(minWidthPx, resolvedWidth),
      "aria-valuemax": clamp(Number.POSITIVE_INFINITY, minWidthPx, reservedPx),
      "aria-disabled": !resizeEnabled,
      hidden: !enabled || resolvedWidth === 0,
      tabIndex: resizeEnabled ? 0 : -1,
    },
  };
}
