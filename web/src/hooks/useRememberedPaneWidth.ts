// The width of a pane at the left of its container, or at its right: the person drags its
// inner edge or uses the arrow keys, within limits that keep the pane and what's beside it readable.
// The choice is remembered on this device, in pixels or as a share of the container, and
// shared by every mounted pane of the kind.

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type RefObject,
} from "react";

import {
  readPanelSharePreference,
  readPanelSizePreference,
  writePanelSharePreference,
  writePanelSizePreference,
  type PanelSharePreferenceKey,
  type PanelSizePreferenceKey,
} from "@/lib/panelSizePreferences";

/**
 * A pane that keeps its share of its container, as a terminal split keeps its ratio, so
 * widening the container widens the pane. The pixel limits still apply to what it draws.
 */
export interface RelativePaneWidth {
  share: PanelSharePreferenceKey;
  /** The container width the default width is for; the default share is their ratio. */
  referenceWidth: number;
  /** A pixel width chosen before the pane kept a share, converted once it's measured. */
  legacyWidth?: () => number | null;
}

interface PaneWidthLimits {
  defaultWidth: number;
  minWidth: number;
  /** The widest the pane may be; with `growsWithRoom`, only until its container is measured. */
  maxWidth: number;
  /** The widest the pane may be in a container this wide, before the min and max apply. */
  roomIn: (containerWidth: number) => number;
  /** A measured container's room alone limits the pane, so it grows with a wide window. */
  growsWithRoom?: boolean;
  /** Names the resize handle for assistive technology. */
  label: string;
  keyboardStep: number;
  /** The step for Shift and an arrow key; without it, Shift steps as far as the arrow alone. */
  largeKeyboardStep?: number;
  /** The container edge the pane starts at; its resize handle is on the other side. Defaults to left. */
  anchor?: "left" | "right";
  /**
   * A pane that can collapse: a drag released with the pointer narrower than `below` calls
   * `onCollapse` instead of resizing, and keeps the choice from before the drag.
   */
  collapse?: { below: number; onCollapse: () => void };
}

/** Where a kind of pane keeps its choice: a pixel width, or a share of its container. */
type StoredPane =
  | { preference: PanelSizePreferenceKey; relative?: never }
  | { relative: RelativePaneWidth; preference?: never };

/** One kind of resizable pane: where its choice is kept and how wide it may be. */
export type PaneWidthSpec = PaneWidthLimits & StoredPane;

/** `width` within the spec's limits and the room `containerWidth` leaves; null is unmeasured. */
export function clampPaneWidth(
  spec: PaneWidthSpec,
  width: number,
  containerWidth: number | null,
): number {
  const measured = containerWidth !== null && containerWidth > 0;
  const room = measured ? spec.roomIn(containerWidth) : spec.maxWidth;
  const cap = measured && spec.growsWithRoom ? room : Math.min(spec.maxWidth, room);
  const ceiling = Math.max(spec.minWidth, cap);
  return Math.round(Math.max(spec.minWidth, Math.min(ceiling, width)));
}

// A choice is a pixel width, or a share of the container for a relative pane.
interface PaneWidthStore {
  preferred: number | null;
  /** A relative pane's pixel width from before it kept a share, until it's converted. */
  legacyWidth: number | null;
  listeners: Set<() => void>;
}

// One live choice per kind of pane, so a width chosen in one shows in the others too.
const stores = new Map<string, PaneWidthStore>();

function readChoice(spec: StoredPane): number | null {
  return spec.relative
    ? readPanelSharePreference(spec.relative.share)
    : readPanelSizePreference(spec.preference);
}

function writeChoice(spec: PaneWidthSpec, choice: number): void {
  if (spec.relative) writePanelSharePreference(spec.relative.share, choice);
  else writePanelSizePreference(spec.preference, choice);
}

function loadStore(spec: StoredPane, store: PaneWidthStore): void {
  store.preferred = readChoice(spec);
  store.legacyWidth = store.preferred === null ? (spec.relative?.legacyWidth?.() ?? null) : null;
}

function storeFor(spec: StoredPane): PaneWidthStore {
  const key = spec.relative ? spec.relative.share : spec.preference;
  let store = stores.get(key);
  if (!store) {
    store = { preferred: null, legacyWidth: null, listeners: new Set() };
    loadStore(spec, store);
    stores.set(key, store);
  }
  return store;
}

/** The container width a relative pane's share applies to; unmeasured is the reference. */
function shareBasis(relative: RelativePaneWidth, containerWidth: number | null): number {
  return containerWidth !== null && containerWidth > 0 ? containerWidth : relative.referenceWidth;
}

/** The width `choice` asks for in this container, before the limits. */
function requestedWidth(
  spec: PaneWidthSpec,
  store: PaneWidthStore,
  choice: number | null,
  containerWidth: number | null,
): number {
  const { relative } = spec;
  if (!relative) return choice ?? spec.defaultWidth;
  if (choice === null && store.legacyWidth !== null) return store.legacyWidth;
  const share = choice ?? spec.defaultWidth / relative.referenceWidth;
  return share * shareBasis(relative, containerWidth);
}

/** The choice that draws `width` in this container. */
function choiceFor(spec: PaneWidthSpec, width: number, containerWidth: number | null): number {
  if (!spec.relative) return width;
  const share = width / shareBasis(spec.relative, containerWidth);
  // A share is a part of the container; a minimum wider than a tiny container stays below all of it.
  return Math.min(0.99, Math.max(0.01, Math.round(share * 10_000) / 10_000));
}

function setPreferredWidth(spec: PaneWidthSpec, choice: number, persist: boolean) {
  const store = storeFor(spec);
  if (persist) {
    // A choice made now replaces a pixel width from before the pane kept a share.
    store.legacyWidth = null;
    writeChoice(spec, choice);
  }
  if (choice === store.preferred) return;
  store.preferred = choice;
  for (const listener of store.listeners) listener();
}

/** Put back a choice the drag only showed, unsaved: null is no choice, the default. */
function restorePreferredWidth(spec: PaneWidthSpec, preferred: number | null) {
  const store = storeFor(spec);
  if (preferred === store.preferred) return;
  store.preferred = preferred;
  for (const listener of store.listeners) listener();
}

/** A relative pane's old pixel width becomes the share that draws it in this container, once. */
function convertLegacyWidth(spec: PaneWidthSpec, containerWidth: number): void {
  const store = storeFor(spec);
  if (!spec.relative || store.preferred !== null || store.legacyWidth === null) return;
  const drawn = clampPaneWidth(spec, store.legacyWidth, containerWidth);
  setPreferredWidth(spec, choiceFor(spec, drawn, containerWidth), true);
}

/** Reload a kind of pane's shared choice from storage, named by its spec or its pixel key. Only for tests. */
export function resetRememberedPaneWidthForTesting(
  kind: PaneWidthSpec | PanelSizePreferenceKey,
): void {
  const spec = typeof kind === "string" ? { preference: kind } : kind;
  const store = storeFor(spec);
  loadStore(spec, store);
  for (const listener of store.listeners) listener();
}

/** A pane's width and the props for its resize handle, from `useRememberedPaneWidth`. */
export type RememberedPaneWidth = ReturnType<typeof useRememberedPaneWidth>;

/**
 * The pane's width in `container`, whose left edge the pane starts at, and the props for
 * its resize handle. A narrower container shows less of the choice until there's room again.
 * Pass a null `containerWidth` while the pane isn't docked, so a relative pane follows only
 * the container it sits in.
 */
export function useRememberedPaneWidth(
  spec: PaneWidthSpec,
  container: RefObject<HTMLElement | null>,
  containerWidth: number | null,
) {
  const store = storeFor(spec);
  const subscribe = useCallback(
    (listener: () => void) => {
      store.listeners.add(listener);
      return () => {
        store.listeners.delete(listener);
      };
    },
    [store],
  );
  const getPreferred = useCallback(() => store.preferred, [store]);
  const preferred = useSyncExternalStore(subscribe, getPreferred, getPreferred);
  const width = clampPaneWidth(
    spec,
    requestedWidth(spec, store, preferred, containerWidth),
    containerWidth,
  );
  const latest = useRef({ spec, width, containerWidth });
  latest.current = { spec, width, containerWidth };
  const [resizing, setResizing] = useState(false);
  // Past a collapsible pane's threshold, a release collapses it; the choice from before stays,
  // not the width a narrow page showed of it.
  const [collapsing, setCollapsing] = useState(false);
  const collapsingRef = useRef(false);
  const preferredBeforeDrag = useRef<number | null>(null);

  // Before paint, so converting an old pixel width never shows the tree at another width.
  const relativeShare = spec.relative?.share;
  useLayoutEffect(() => {
    if (relativeShare === undefined || containerWidth === null || containerWidth <= 0) return;
    convertLegacyWidth(latest.current.spec, containerWidth);
  }, [relativeShare, containerWidth]);

  const choose = useCallback((next: number, persist: boolean) => {
    const { spec: current, containerWidth: room } = latest.current;
    const clamped = clampPaneWidth(current, next, room);
    // Ahead of the render, so a release right after a move saves where the move left it.
    latest.current.width = clamped;
    setPreferredWidth(current, choiceFor(current, clamped, room), persist);
  }, []);

  const onPointerDown = useCallback((event: React.PointerEvent) => {
    event.preventDefault();
    preferredBeforeDrag.current = storeFor(latest.current.spec).preferred;
    setResizing(true);
  }, []);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      // The handle is the pane's inner edge: the arrow pointing away from the pane widens it.
      const { spec: current, width: shown } = latest.current;
      const step = (event.shiftKey && current.largeKeyboardStep) || current.keyboardStep;
      const widen = current.anchor === "right" ? "ArrowLeft" : "ArrowRight";
      const narrow = widen === "ArrowLeft" ? "ArrowRight" : "ArrowLeft";
      // Home and End go to the narrowest and the widest the container allows.
      const next =
        event.key === widen
          ? shown + step
          : event.key === narrow
            ? shown - step
            : event.key === "Home"
              ? current.minWidth
              : event.key === "End"
                ? Infinity
                : null;
      if (next === null) return;
      event.preventDefault();
      choose(next, true);
    },
    [choose],
  );

  // The default is the choice itself; a tight container only shows less of it until it widens.
  const onDoubleClick = useCallback(() => {
    const current = latest.current.spec;
    const choice = current.relative
      ? current.defaultWidth / current.relative.referenceWidth
      : current.defaultWidth;
    setPreferredWidth(current, choice, true);
  }, []);

  useEffect(() => {
    if (!resizing) return;
    const body = document.body.style;
    const before = { cursor: body.cursor, userSelect: body.userSelect };
    body.cursor = "col-resize";
    body.userSelect = "none";
    const markCollapsing = (next: boolean) => {
      if (collapsingRef.current === next) return;
      collapsingRef.current = next;
      setCollapsing(next);
    };
    const onPointerMove = (event: PointerEvent) => {
      const box = container.current?.getBoundingClientRect();
      if (!box) return;
      const raw =
        latest.current.spec.anchor === "right"
          ? box.right - event.clientX
          : event.clientX - box.left;
      const collapse = latest.current.spec.collapse;
      markCollapsing(collapse !== undefined && raw < collapse.below);
      choose(raw, false);
    };
    const stop = () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      window.removeEventListener("pointercancel", onPointerUp);
      body.cursor = before.cursor;
      body.userSelect = before.userSelect;
    };
    // Saved once on release, not on every move; a cancelled drag keeps where it got to.
    const onPointerUp = () => {
      const collapse = collapsingRef.current ? latest.current.spec.collapse : undefined;
      const { spec: current, width: shown, containerWidth: room } = latest.current;
      // The drag ends, listeners and cursor included, before the pane is told to go.
      stop();
      markCollapsing(false);
      setResizing(false);
      if (collapse) {
        // A move only showed its width; storage still holds the choice.
        restorePreferredWidth(current, preferredBeforeDrag.current);
        collapse.onCollapse();
        return;
      }
      setPreferredWidth(current, choiceFor(current, shown, room), true);
    };
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
    return stop;
  }, [resizing, container, choose]);

  return {
    width,
    resizing,
    /** A release now would collapse the pane. */
    collapsing,
    handleProps: {
      onPointerDown,
      onKeyDown,
      onDoubleClick,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": spec.label,
      "aria-valuemin": spec.minWidth,
      "aria-valuemax": clampPaneWidth(spec, Infinity, containerWidth),
      "aria-valuenow": width,
      tabIndex: 0,
      title: spec.collapse
        ? "Drag to resize, or past the edge to collapse. Double-click to reset."
        : "Drag to resize. Double-click to reset.",
    },
  };
}
