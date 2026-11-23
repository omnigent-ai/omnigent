// A split's panes as flat keyed siblings, so splitting, unsplitting or hiding a pane never remounts
// its content; each shown pane keeps a strip row above it (see paneStrip/).

import {
  Fragment,
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { useTerminalIsDark } from "@/components/theme/useTerminalIsDark";
import { APP_PANE_HOST, CHAT_PANE_BODY, TERMINAL_SURFACE } from "@/lib/paneSurfaces";
import { terminalSurfaceStyle } from "@/lib/terminalPalette";
import {
  resizeTerminalSplit,
  type TerminalLayout,
  terminalLayoutGeometry,
  terminalLayoutKeys,
  type TerminalPaneSide,
  type TerminalSplitState,
} from "@/lib/paneSplitLayout";
import { cn } from "@/lib/utils";

import { type PaneDragOutside, paneMoveDestinations, paneMoveLayout } from "./paneMove";
import { chatClickTakesKeyboard } from "./paneFocus";
import { domCellSizing, type PaneSizing } from "./paneSizing";
import {
  applyDividerResizes,
  cornerDragResizes,
  type DividerDragSession,
  type DividerResize,
  dividerCorners,
  dividerDragSession,
  followCorner,
  holdsItsDividers,
} from "./paneDividerCorners";
import { PaneSplitDivider } from "./PaneSplitDivider";
import { TerminalSplitCorner } from "./TerminalSplitCorner";
import { TerminalZoomPill } from "./TerminalZoomPill";
import { PANE_ROW_PX } from "./paneStrip/paneGeometry";
import { PaneTitleStrip } from "./paneStrip/PaneTitleStrip";
import { usePaneMoveDrag } from "./paneStrip/usePaneMoveDrag";
import { type PaneStripSource, usePaneStrips } from "./paneStrip/usePaneStrips";
import { usePendingAppPaneDrop } from "./appPanes";

/** A row along the window's top, as tall as index.css makes it: the Mac title bar there. */
export const WINDOW_TOP_ROW_HEIGHT = `var(--pane-window-top-row, ${PANE_ROW_PX}px)`;

/** A pane row's height: the title bar's along the window's top, else a plain row's. */
const paneRowHeight = (titleBarRow: boolean) =>
  titleBarRow ? WINDOW_TOP_ROW_HEIGHT : `${PANE_ROW_PX}px`;

export interface SplitPanesLayoutProps {
  conversationId: string;
  /** The split shown, or undefined for one pane: ``singlePaneKey``. */
  split: TerminalSplitState | undefined;
  singlePaneKey: string | null;
  /** More panes kept mounted, hidden, so their terminals survive while not shown. */
  alsoMounted?: readonly string[];
  /** The stable part of a pane's key, such as its terminal id. */
  paneIdOf: (paneKey: string) => string;
  renderPane: (paneKey: string, focused: boolean, shown: boolean) => ReactNode;
  onFocusPane?: (paneKey: string) => void;
  /** Saves the ratios of the dividers let go, or of one moved by a key, as one change. */
  onResize: (resizes: readonly DividerResize[]) => void;
  panesRef: RefObject<HTMLDivElement | null>;
  /** The size rule a divider drag stops at; by default the cells of the terminals rendered here. */
  sizing?: () => PaneSizing | null;
  /** The split's pane filling the view, its other panes kept hidden at their own size. */
  zoomedKey?: string | null;
  /** The zoomed pane's pill was pressed. */
  onUnzoom?: (paneKey: string) => void;
  /**
   * Each pane's strip row: its name always, and its actions on the active or hovered pane.
   * Without it, panes have no row, and a maximized pane shows the Maximized pill instead.
   */
  strip?: PaneStripSource;
  /**
   * The pane whose body is a slot over its terminal: the chat pane's, where the web chat renders
   * while the session shows Chat beside other panes. Its terminal stays attached beneath.
   */
  bodySlot?: { paneKey: string; ref: (element: HTMLElement | null) => void };
  /**
   * Moves a pane dropped beside another, dragged by its strip row. Without it (or a row), panes
   * can't be dragged.
   */
  onMovePane?: (move: { key: string; targetKey: string; side: TerminalPaneSide }) => void;
  /** Gives a pane the keyboard back after a drag that moved nothing; by default its focus. */
  refocusPane?: (paneKey: string) => void;
  /** Where a pane dragged by its row may go off the split. */
  paneDragOutside?: PaneDragOutside;
  /**
   * Rowed panes float as rounded cards with a gap between them, over the container's own
   * background; the focused card's edge brightens instead of its row carrying an accent line.
   */
  cards?: boolean;
  className?: string;
}

const NO_PANES: readonly string[] = [];

// A pane's content is keyed in both its shown and hidden shapes, so the strip row and pill around
// it in one shape never shift it to another position and remount it.
const PANE_CONTENT_KEY = "pane-content";

/** Half the gap between two cards, on each card's every side. */
const PANE_CARD_GAP_PX = 3;
const PANE_CARD_GAP_CLASS = "p-[3px]";

export function SplitPanesLayout({
  conversationId,
  split,
  singlePaneKey,
  alsoMounted = NO_PANES,
  paneIdOf,
  renderPane,
  onFocusPane,
  onResize,
  panesRef,
  zoomedKey = null,
  onUnzoom,
  sizing,
  strip,
  bodySlot,
  onMovePane,
  refocusPane,
  paneDragOutside,
  cards = false,
  className,
}: SplitPanesLayoutProps) {
  // The pointer drag on a divider or corner, from press to release: its sizes show over the latest
  // saved layout only while it lives, and only its release saves them.
  const [session, setSession] = useState<DividerDragSession | null>(null);
  // A layout saved meanwhile, as from another window, restarts the drag's sizes from it.
  // The drag's sizes as its last pointer move left them, which may not have rendered yet.
  const latestResizes = useRef<readonly DividerResize[] | null>(null);
  useEffect(() => {
    latestResizes.current = null;
    setSession((current) => current && { ...current, resizes: null });
  }, [split]);
  // Maximizing, unsplitting, or closing a pane so another split takes a held divider's place ends
  // the drag, dropping its sizes.
  const maximized =
    split !== undefined &&
    zoomedKey !== null &&
    terminalLayoutKeys(split.layout).includes(zoomedKey);
  const liveSession =
    session && split && !maximized && holdsItsDividers(session, split.layout) ? session : null;
  useEffect(() => {
    if (session && !liveSession) setSession(null);
  }, [session, liveSession]);
  // A divider stops where a pane it shrinks would fall below the minimum.
  const keeps = (before: TerminalLayout, after: TerminalLayout) => {
    const rule = sizing ? sizing() : domCellSizing(panesRef.current);
    return rule === null || rule.keeps(before, after);
  };
  const roomFor = (path: number[], ratio: number) =>
    !split || keeps(split.layout, resizeTerminalSplit(split.layout, path, ratio));
  const draft = liveSession?.resizes ?? null;
  const layout = split && draft ? applyDividerResizes(split.layout, draft) : split?.layout;
  const startDrag = (taken: Parameters<typeof dividerDragSession>[0], from: TerminalLayout) => {
    latestResizes.current = null;
    setSession(dividerDragSession(taken, from));
  };
  const moveDrag = (resizes: readonly DividerResize[]) => {
    latestResizes.current = resizes;
    setSession((current) => current && { ...current, resizes });
  };
  // Saves the last move's sizes, even one a release follows before it renders.
  const commitDrag = () => {
    const resizes = latestResizes.current;
    latestResizes.current = null;
    setSession(null);
    if (resizes && liveSession) onResize(resizes);
  };
  const abortDrag = () => {
    latestResizes.current = null;
    setSession(null);
  };
  const shownLayout = layout ?? singlePaneKey;
  const geometry =
    shownLayout === null ? { panes: [], dividers: [] } : terminalLayoutGeometry(shownLayout);
  const placed = new Map(geometry.panes.map((pane) => [pane.key, pane]));
  const zoomed = split && zoomedKey !== null && placed.has(zoomedKey) ? zoomedKey : null;
  // The corner the drag took, where its dividers are now, though its junction split or merged.
  const heldCorner = liveSession?.corner
    ? followCorner(liveSession.corner, geometry.dividers)
    : null;
  // One keyed list in a steady order: a pane moving between shown and hidden keeps its terminal.
  const paneKeys = [...new Set([...alsoMounted, ...placed.keys()])];
  const { stripOf, atWindowTop } = usePaneStrips({
    source: strip,
    panesRef,
    onUnzoom,
    refocus: refocusPane,
    placements: geometry.panes.map((pane) => ({
      key: pane.key,
      widthPercent: pane.width,
      atViewCorner: pane.left < 0.01 && pane.top < 0.01,
      atViewTop: pane.top < 0.01,
      zoomed: pane.key === zoomed,
      // A maximized pane is the only one shown, so it's the active one.
      active: !split || pane.key === split.focusedKey || pane.key === zoomed,
    })),
  });
  // The row and card take the terminal's colors, whatever the app's appearance.
  const surfaceStyle = terminalSurfaceStyle(useTerminalIsDark());
  const rowed = strip !== undefined;
  const carded = rowed && cards;
  const splitShown = split !== undefined && geometry.panes.length > 1;
  // Panes move by their rows while a split shows whole: not maximized, nor mid divider drag.
  const movable = rowed && splitShown && liveSession === null && !!onMovePane;
  // The split's focused pane becomes `key`, and its content takes the keyboard.
  const givePaneKeyboard = (key: string) => {
    onFocusPane?.(key);
    refocusPane?.(key);
  };
  const givePaneKeyboardFromChat = (event: MouseEvent, key: string) => {
    if (event.button !== 0 || !(event.target instanceof Element)) return;
    onFocusPane?.(key);
    if (!chatClickTakesKeyboard(event.target, document.getSelection())) return;
    refocusPane?.(key);
  };
  const paneDrag = usePaneMoveDrag({
    panesRef,
    // Where ⌘K's Move Pane… is offered too: not while a pane is maximized.
    layout: movable ? paneMoveLayout(split?.layout, zoomed) : null,
    destinations: (key) =>
      split
        ? paneMoveDestinations(
            split.layout,
            key,
            sizing ? sizing() : domCellSizing(panesRef.current),
          )
        : [],
    onMove: (move) => onMovePane?.(move),
    refocus: refocusPane ?? onFocusPane,
    outside: paneDragOutside,
  });

  // A sidebar tab dragged over the panes shows where it would open, as a dragged pane does.
  const tabDrop = usePendingAppPaneDrop(conversationId);
  const shownDrop = paneDrag.drop ?? tabDrop;

  return (
    <div
      ref={panesRef}
      data-split-panes={conversationId}
      className={cn("relative min-h-0 flex-1", className)}
    >
      {paneKeys.map((key) => {
        const pane = placed.get(key);
        if (!pane) {
          return (
            // Not display:none: a hidden terminal would measure 0px and lose its size.
            <div
              key={`${conversationId}:${paneIdOf(key)}`}
              data-terminal-pane={key}
              data-pane-hidden=""
              aria-hidden
              className={cn(
                "invisible absolute inset-0 flex flex-col",
                carded && PANE_CARD_GAP_CLASS,
              )}
              // Kept at the size it has when it shows alone: below its row, inside its card.
              style={
                rowed
                  ? {
                      paddingTop: `calc(${paneRowHeight(atWindowTop)} + ${carded ? PANE_CARD_GAP_PX : 0}px)`,
                    }
                  : undefined
              }
            >
              {/* The shown pane's shape, so showing it never remounts its terminal. */}
              <div
                className={cn(
                  "relative flex min-h-0 min-w-0 flex-1 flex-col",
                  carded && "border border-transparent",
                )}
              >
                <Fragment key={PANE_CONTENT_KEY}>{renderPane(key, false, false)}</Fragment>
              </div>
            </div>
          );
        }
        const focused = !split || pane.key === split.focusedKey;
        const isZoomed = zoomed === pane.key;
        // Behind a zoomed pane the others keep their size, so their shells aren't resized.
        const zoomedAway = zoomed !== null && !isZoomed;
        const area = isZoomed ? { left: 0, top: 0, width: 100, height: 100 } : pane;
        const paneStrip = stripOf(pane.key);
        const content = renderPane(pane.key, focused, !zoomedAway);
        // Only a split's focused, unmaximized pane shows focus: its card's border, or its row's
        // accent line. A lone or maximized pane has none to show.
        const focusShown = rowed && splitShown && !isZoomed && focused;
        // Held by its row, to move: the pane lifts from the press until the drag ends.
        const held = paneDrag.heldKey === pane.key;
        return (
          <div
            key={`${conversationId}:${paneIdOf(pane.key)}`}
            data-terminal-pane={pane.key}
            data-pane-zoomed={isZoomed ? "" : undefined}
            data-pane-hidden={zoomedAway ? "" : undefined}
            aria-hidden={zoomedAway || undefined}
            style={{
              left: `${area.left}%`,
              top: `${area.top}%`,
              width: `${area.width}%`,
              height: `${area.height}%`,
            }}
            className={cn(
              "absolute flex flex-col",
              // Half the gap on every side: neighbours sit a full gap apart.
              carded && PANE_CARD_GAP_CLASS,
              zoomedAway && "invisible",
              // Above its neighbours, so its lift's shadow falls over them.
              held && "z-[5]",
              !rowed && split && focused && !isZoomed && "ring-1 ring-ring ring-inset",
            )}
            // Focus in the pane itself chooses it; its portaled menus' focus doesn't.
            onFocusCapture={
              split && onFocusPane
                ? (event) => {
                    if (event.currentTarget.contains(event.target as Node)) onFocusPane(pane.key);
                  }
                : undefined
            }
          >
            <div
              style={rowed ? surfaceStyle : undefined}
              className={cn(
                "relative flex min-h-0 min-w-0 flex-1 flex-col",
                rowed && "overflow-hidden bg-background",
                rowed && "transition-[box-shadow,translate] duration-150 ease-out",
                carded && "rounded-lg border",
                carded &&
                  (focusShown
                    ? "border-[color:var(--pane-active-border,var(--ring))]"
                    : "border-border/60"),
                held && "-translate-y-px shadow-lg ring-2 ring-primary/70",
              )}
              data-pane-held={held ? "" : undefined}
            >
              {paneStrip ? (
                <PaneTitleStrip
                  paneKey={pane.key}
                  pane={
                    paneStrip.pane ?? {
                      name: "",
                      commands: null,
                      focus: () => givePaneKeyboard(pane.key),
                    }
                  }
                  layout={paneStrip.layout}
                  focused={focusShown}
                  focusLine={!carded}
                  actionsShown={paneStrip.actionsShown && !zoomedAway}
                  zoomed={paneStrip.zoomed}
                  windowCorner={paneStrip.windowCorner && !zoomedAway}
                  windowTop={paneStrip.windowTop && !zoomedAway}
                  titleBarRow={paneStrip.windowTop}
                  onFocusChange={paneStrip.onFocusChange}
                  onDragStart={paneDrag.pointerDownFor(pane.key)}
                  dragging={paneDrag.draggingKey === pane.key}
                  held={held}
                />
              ) : null}
              <Fragment key={PANE_CONTENT_KEY}>{content}</Fragment>
              {bodySlot?.paneKey === pane.key ? (
                <ChatPaneBodyHost
                  hostRef={bodySlot.ref}
                  top={paneStrip ? paneRowHeight(paneStrip.windowTop) : 0}
                  onChatClick={(event) => givePaneKeyboardFromChat(event, pane.key)}
                />
              ) : null}
              {!paneStrip && isZoomed && onUnzoom ? (
                <TerminalZoomPill onUnzoom={() => onUnzoom(pane.key)} />
              ) : null}
            </div>
          </div>
        );
      })}
      {split &&
        zoomed === null &&
        geometry.dividers.map((divider) => (
          <PaneSplitDivider
            key={divider.path.join(".")}
            divider={divider}
            containerRef={panesRef}
            inGap={carded}
            dragging={
              liveSession !== null &&
              liveSession.corner === null &&
              liveSession.held[0].path.join(".") === divider.path.join(".")
            }
            onStart={(taken) => startDrag(taken, split.layout)}
            onResize={(path, ratio, final) => {
              if (!roomFor(path, ratio)) return;
              if (final) onResize([{ path, ratio }]);
              else moveDrag([{ path, ratio }]);
            }}
            onCommit={commitDrag}
            onAbort={abortDrag}
          />
        ))}
      {split &&
        layout &&
        zoomed === null &&
        (heldCorner ? [heldCorner] : dividerCorners(geometry.dividers)).map((corner) => (
          <TerminalSplitCorner
            key={corner.key}
            corner={corner}
            containerRef={panesRef}
            inGap={carded}
            dragging={heldCorner !== null && heldCorner.key === corner.key}
            onStart={(taken) => startDrag(taken, split.layout)}
            // Each way moves from where the drag has it, so one stopped at a minimum stays put.
            onDrag={(dragged, point) => moveDrag(cornerDragResizes(layout, dragged, point, keeps))}
            onCommit={commitDrag}
            onAbort={abortDrag}
          />
        ))}
      {shownDrop ? (
        // The half the dragged pane or tab would take, edged in the accent on the side it meets.
        <div
          aria-hidden
          data-pane-drop={`${shownDrop.targetKey}:${shownDrop.side}`}
          className={cn(
            "pointer-events-none absolute z-20 border-primary bg-primary/15",
            DROP_EDGE[shownDrop.side],
          )}
          style={{
            left: `${shownDrop.area.left}%`,
            top: `${shownDrop.area.top}%`,
            width: `${shownDrop.area.width}%`,
            height: `${shownDrop.area.height}%`,
          }}
        />
      ) : null}
    </div>
  );
}

/** The drop half's edge against the rest of its target, where the two panes will meet. */
const DROP_EDGE: Record<TerminalPaneSide, string> = {
  left: "border-r-2",
  right: "border-l-2",
  top: "border-b-2",
  bottom: "border-t-2",
};

/**
 * When a pane may take keyboard focus on its own. A pane split off may take it once, as it
 * first connects, also when the split itself brings the panes into view (the first split from
 * Chat); a pane that comes back with the view (a reload, a return) waits for a request. Focus
 * the person moved out of the panes' terminals, such as into a Find field, stays put.
 */
export function useSplitPaneFocusGuards({
  conversationId,
  visible,
  split,
  shownPaneKeys,
  panesRef,
}: {
  conversationId: string;
  visible: boolean;
  split: TerminalSplitState | undefined;
  shownPaneKeys: readonly string[];
  panesRef: RefObject<HTMLDivElement | null>;
}): { takeConnectFocus: (paneKey: string) => boolean; focusStillHere: () => boolean } {
  const shown = useRef<{ conversationId: string; keys: Set<string> } | null>(null);
  const connectFocus = useRef(new Set<string>());
  useEffect(() => {
    // Hidden panes are tracked too: a pane that shows with the view is new only if the split
    // that brought the view in added it, never if it waited hidden.
    if (shown.current?.conversationId !== conversationId) {
      connectFocus.current.clear();
      shown.current = { conversationId, keys: new Set(shownPaneKeys) };
      return;
    }
    if (!visible) connectFocus.current.clear();
    // Only splitting adds a pane; a single view swapping its terminal grants nothing.
    else if (split) {
      for (const key of shownPaneKeys) {
        if (!shown.current.keys.has(key)) connectFocus.current.add(key);
      }
    }
    shown.current.keys = new Set(shownPaneKeys);
  });
  return {
    takeConnectFocus: (paneKey) => connectFocus.current.delete(paneKey),
    focusStillHere: () => {
      const focused = document.activeElement;
      if (focused === null || focused === document.body) return true;
      // In a pane: its terminal, the web chat that is the chat pane, or an app pane's tab.
      return (
        !!panesRef.current?.contains(focused) &&
        (focused.closest(TERMINAL_SURFACE) !== null ||
          focused.closest(CHAT_PANE_BODY) !== null ||
          focused.closest(APP_PANE_HOST) !== null)
      );
    },
  };
}

/** The panes a split shows, or the single pane unsplit. */
export const shownPanesOf = (
  split: TerminalSplitState | undefined,
  singlePaneKey: string | null,
) => (split ? terminalLayoutKeys(split.layout) : singlePaneKey ? [singlePaneKey] : []);

/**
 * Where the chat page portals the chat, below the pane's title row. A click on the chat's own
 * space chooses its pane and gives the composer the keyboard, as a click in a terminal does. The
 * chat is portaled from another React branch, so its clicks reach this host only in the DOM.
 */
function ChatPaneBodyHost({
  hostRef,
  top,
  onChatClick,
}: {
  hostRef: (element: HTMLElement | null) => void;
  /** Below the pane's row: its height in pixels, or the window-top row's CSS height. */
  top: number | string;
  onChatClick: (event: MouseEvent) => void;
}) {
  const [host, setHost] = useState<HTMLElement | null>(null);
  const latestClick = useRef(onChatClick);
  latestClick.current = onChatClick;
  useEffect(() => {
    if (host === null) return;
    const onClick = (event: MouseEvent) => latestClick.current(event);
    host.addEventListener("click", onClick);
    return () => host.removeEventListener("click", onClick);
  }, [host]);
  const setHostRef = useCallback(
    (element: HTMLElement | null) => {
      hostRef(element);
      setHost(element);
    },
    [hostRef],
  );
  return (
    <div
      ref={setHostRef}
      data-chat-pane-body-host=""
      className="absolute inset-x-0 bottom-0 z-10 flex min-h-0 flex-col bg-background"
      style={{ top }}
    />
  );
}
