// The strip rows of a view's panes, for SplitPanesLayout: each pane's width and hover, worked
// out once for every pane kind.

import {
  type RefObject,
  useCallback,
  useEffect,
  useLayoutEffect,
  useReducer,
  useRef,
  useState,
} from "react";

import { usePaneDividerDragging } from "@/shell/paneDividerDrag";

import { createPaneHover, type PaneHover, stripShown } from "./paneHover";
import { type PaneStripLayout, paneStripLayout } from "./paneStripLayout";
import { type PaneStripPane, paneStripParts } from "./PaneTitleStrip";

/** What a pane kind gives its rows. */
export interface PaneStripSource {
  /** The pane's name and commands, or null for a pane whose row stays blank. */
  paneOf: (paneKey: string) => PaneStripPane | null;
  /** The hover the panes' frames report into; otherwise the layout keeps its own. */
  hover?: PaneHover;
}

export interface PaneStripPlacement {
  key: string;
  /** Percent of the view's width. */
  widthPercent: number;
  /** Whether the pane's tile starts at the view's top-left corner. */
  atViewCorner: boolean;
  /** Whether the pane's tile runs along the view's top edge. */
  atViewTop: boolean;
  zoomed: boolean;
  /** The pane the keyboard is in, or the view's only pane: its actions always show. */
  active: boolean;
}

export interface PaneStripRender {
  pane: PaneStripPane | null;
  layout: PaneStripLayout;
  actionsShown: boolean;
  zoomed: boolean;
  /** The row starts at the window's top-left corner, under the Mac's traffic lights. */
  windowCorner: boolean;
  /** The row starts under the Mac title bar's drag strip, along the view's top edge. */
  windowTop: boolean;
  onFocusChange: (focused: boolean) => void;
}

/** Where the Mac title bar's drag strip ends, CSS px; 0 where the window draws none. */
function titleBarBottom(document: Document): number {
  return document.querySelector(".electron-drag-strip")?.getBoundingClientRect().bottom ?? 0;
}

export interface PaneStrips {
  stripOf: (paneKey: string) => PaneStripRender | null;
  /** The view starts under the Mac title bar's drag strip, as a pane shown alone's row does. */
  atWindowTop: boolean;
}

export function usePaneStrips({
  source,
  panesRef,
  onUnzoom,
  refocus,
  placements,
}: {
  /** Without one, the panes have no rows. */
  source: PaneStripSource | undefined;
  panesRef: RefObject<HTMLDivElement | null>;
  /** Restores the maximized pane, handing it the keyboard: its row's Restore. */
  onUnzoom?: (paneKey: string) => void;
  /** Gives the keyboard to the split's chosen pane, as `paneKey`'s menu closes after a split. */
  refocus?: (paneKey: string) => void;
  placements: readonly PaneStripPlacement[];
}): PaneStrips {
  const active = source !== undefined;
  const [ownHover] = useState(() => createPaneHover());
  const hover = source?.hover ?? ownHover;
  useEffect(() => () => ownHover.dispose(), [ownHover]);
  const [, rerender] = useReducer((count: number) => count + 1, 0);
  useEffect(() => hover.subscribe(rerender), [hover]);
  const dividerDragging = usePaneDividerDragging();

  // The view's width, so each pane's is known in pixels; whether it starts at the window's
  // top-left corner; and how much of the corner row the window's controls take there.
  const [viewWidth, setViewWidth] = useState(0);
  const [atWindowCorner, setAtWindowCorner] = useState(false);
  const [atWindowTop, setAtWindowTop] = useState(false);
  const [cornerInset, setCornerInset] = useState(0);
  useLayoutEffect(() => {
    const element = panesRef.current;
    if (!active || element === null) return;
    const measure = () => {
      const box = element.getBoundingClientRect();
      setViewWidth(box.width);
      const titleBar = titleBarBottom(element.ownerDocument);
      // Floating cards start a few pixels in from the window's edges, still under the title bar
      // and its traffic lights; edge to edge, the view starts at the corner itself.
      setAtWindowCorner(box.top < Math.max(titleBar, 1) && box.left < Math.max(titleBar, 1));
      setAtWindowTop(box.top < titleBar);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [active, panesRef]);

  // The corner row's room for the window's controls, which CSS sizes and shows only on the Mac
  // with the sidebar closed; it changes as the sidebar or full screen toggles.
  const cornerKey = atWindowCorner
    ? (placements.find((pane) => pane.zoomed) ?? placements.find((pane) => pane.atViewCorner))?.key
    : undefined;
  useLayoutEffect(() => {
    const inset =
      cornerKey === undefined
        ? null
        : panesRef.current?.querySelector(
            `[data-pane-strip="${CSS.escape(cornerKey)}"] [data-pane-strip-window-inset]`,
          );
    if (!inset) {
      setCornerInset(0);
      return;
    }
    const measure = () => setCornerInset(Math.ceil(inset.getBoundingClientRect().width));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(inset);
    return () => observer.disconnect();
  }, [cornerKey, panesRef]);

  // The page's own view of the pointer over these panes: a move it sees is over no frame.
  const paneOfEvent = (event: Event): string | null => {
    const root = panesRef.current;
    const target = event.target;
    if (!root || !(target instanceof Element) || !root.contains(target)) return null;
    return target.closest<HTMLElement>("[data-terminal-pane]")?.dataset.terminalPane ?? null;
  };
  const paneOfEventRef = useRef(paneOfEvent);
  paneOfEventRef.current = paneOfEvent;
  useEffect(() => {
    if (!active) return;
    const onPointerMove = (event: PointerEvent) =>
      hover.pointerMoved(paneOfEventRef.current(event));
    const onLeave = () => hover.resetAll();
    const onVisibility = () => {
      if (document.visibilityState === "hidden") hover.resetAll();
    };
    document.addEventListener("pointermove", onPointerMove, true);
    document.documentElement.addEventListener("pointerleave", onLeave);
    window.addEventListener("blur", onLeave);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("pointermove", onPointerMove, true);
      document.documentElement.removeEventListener("pointerleave", onLeave);
      window.removeEventListener("blur", onLeave);
      document.removeEventListener("visibilitychange", onVisibility);
      hover.resetAll();
    };
  }, [active, hover]);

  // A pane that stops showing (closed, hidden behind a maximized one) has no pointer over it.
  const zoomedKey = placements.find((pane) => pane.zoomed)?.key ?? null;
  const visibleKeys = active
    ? zoomedKey !== null
      ? [zoomedKey]
      : placements.map((pane) => pane.key)
    : [];
  const visibleSignature = visibleKeys.join("\n");
  const previousVisible = useRef<readonly string[]>([]);
  useEffect(() => {
    const now = new Set(visibleKeys);
    for (const key of previousVisible.current) if (!now.has(key)) hover.reset(key);
    previousVisible.current = visibleKeys;
    // visibleSignature stands for visibleKeys.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visibleSignature, hover]);

  const setFocusInStrip = useCallback(
    (key: string, focused: boolean) => hover.setFocusInStrip(key, focused),
    [hover],
  );

  const stripOf = (paneKey: string): PaneStripRender | null => {
    if (!active) return null;
    const placement = placements.find((pane) => pane.key === paneKey);
    if (!placement) return null;
    const pane = source.paneOf(paneKey);
    const width = ((placement.zoomed ? 100 : placement.widthPercent) / 100) * viewWidth;
    const zoomed = placement.zoomed;
    const windowCorner = atWindowCorner && (zoomed || placement.atViewCorner);
    const windowTop = atWindowTop && (zoomed || placement.atViewTop);
    // A maximized pane's Restore is the view's own, as its pill was: it hands the keyboard back,
    // and stays while the split's commands wait (a shell starting).
    const restore = zoomed && onUnzoom ? { toggleZoom: () => onUnzoom(paneKey) } : null;
    const paneCommands =
      pane && (pane.commands || restore) ? { ...pane.commands, ...restore } : null;
    const shownPane = pane && {
      ...pane,
      commands: paneCommands,
      ...(refocus ? { refocus: () => refocus(paneKey) } : {}),
    };
    const parts = shownPane && paneStripParts(shownPane, { zoomed });
    const layout = parts
      ? paneStripLayout(width, {
          ...parts,
          fixed: [
            ...(windowCorner && cornerInset > 0 ? [cornerInset] : []),
            ...(parts.fixed ?? []),
          ],
        })
      : paneStripLayout(width, { actions: 0, restore: false });
    return {
      pane: shownPane,
      layout,
      actionsShown:
        viewWidth > 0 && (placement.active || stripShown(hover.get(paneKey), dividerDragging)),
      zoomed,
      windowCorner,
      windowTop,
      onFocusChange: (focused) => setFocusInStrip(paneKey, focused),
    };
  };
  return { stripOf, atWindowTop: active && atWindowTop };
}
