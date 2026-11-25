// The session's main view as panes: the agent's terminal, the shells and workspace tabs split
// beside it, and the web chat as the agent's pane while the session shows Chat. Every state with
// nothing to show as panes (starting up, stopped, a shell the sidebar opened, a viewer who can't
// split) is the Terminal view's own, unchanged.

import { MessageSquareIcon, PanelRightIcon } from "lucide-react";
import {
  type ComponentProps,
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
} from "react";

import { usePaneCommandsListed, useTerminalPaneHotkeys } from "@/hooks/useTerminalPaneHotkeys";
import {
  findAgentTerminal,
  isAgentTerminalKey,
  PANEL_NO_TERMINAL_KEY,
  type TerminalInfo,
  terminalDisplayLabel,
  terminalTabKey,
  useTerminals,
} from "@/hooks/useTerminals";
import {
  reconcileTerminalSplit,
  terminalLayoutKeys,
  type TerminalSplitState,
} from "@/lib/paneSplitLayout";
import { CHAT_PANE_BODY, TERMINAL_SURFACE } from "@/lib/paneSurfaces";
import { cn } from "@/lib/utils";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";
import { showToast } from "@/components/ui/toast";

import { AppPaneHost } from "./AppPaneHost";
import {
  type AppPaneTitle,
  appPaneSidebarDrop,
  appPaneTabActions,
  appPaneTabId,
  focusAppPane,
  isAppPaneKey,
  useAppPaneTitles,
} from "./appPanes";
import {
  setChatPaneBodyHost,
  setOtherPaneMaximized,
  setSplitBesideChat,
  useChatPaneViewToggleHostRef,
} from "./chatPaneToolbarHost";
import { FileTypeIcon } from "./FileTypeIcon";
import type { MainTerminalView } from "./MainTerminalView";
import { type AgentRecoveryAllowance, MissingAgentPane } from "./MissingAgentPane";
import { MissingTerminalPane } from "./MissingTerminalPane";
import { PaneCloseDialog } from "./PaneCloseDialog";
import { applyDividerResizes } from "./paneDividerCorners";
import { moveSplitPane } from "./paneMove";
import { domCellSizing } from "./paneSizing";
import { usePaneSplit } from "./paneSplitStore";
import { giveNextPaneTheKeyboard } from "./paneShellClose";
import { isFailedPaneStart } from "./paneStartFailures";
import { type SessionPaneMenu, sessionPaneStrip } from "./paneStrip/paneMenu";
import type { PaneStripPane } from "./paneStrip/PaneTitleStrip";
import { shownPanesOf, SplitPanesLayout, useSplitPaneFocusGuards } from "./SplitPanesLayout";
import { focusSplitPane } from "./splitTargetActions";
import { TerminalPaneSurface } from "./TerminalPaneSurface";
import {
  requestTerminalFocus,
  settleTerminalFocus,
  TERMINAL_FOCUS_SURFACE_ATTRIBUTE,
  terminalFocusSurfaceId,
  usePendingTerminalFocus,
} from "./terminalFocusRequest";
import { type TerminalPaneActions, useTerminalPaneActions } from "./terminalPaneActions";
import {
  moveTerminalToSidebar,
  type TerminalMoveScope,
  terminalToSidebarRefusal,
} from "./terminalPaneMoves";
import { unzoomPane, useZoomedPaneKey } from "./terminalPaneZoom";
import { usePanesSplitsAllowed } from "./panesViewGate";
import {
  currentTerminalListGeneration,
  type TerminalListGeneration,
  useTerminalListGeneration,
} from "./terminalListGeneration";
import { usePublishMainTerminalKeys } from "./terminalPlacement";
import { PublishedTerminalView } from "./PublishedTerminalView";
import { usePaneKeyboardOwner } from "./usePaneKeyboardOwner";
import { mainSplitTarget } from "./terminalSplitTarget";
import { useTerminalStatuses } from "./useTerminalStatuses";
import { BELOW_SESSION_HEADER } from "./sessionHeaderInset";
import { usePaneCycleHotkey } from "@/hooks/usePaneCycleHotkey";

type MainTerminalViewProps = ComponentProps<typeof MainTerminalView>;

const NO_KEYS: readonly string[] = [];

export interface SplitPanesMainViewProps extends Omit<
  MainTerminalViewProps,
  "splittable" | "onShownTerminalKeys"
> {
  /** The session shows Chat: the agent's pane is the web chat, and a split shows beside it. */
  chatShown?: boolean;
  /** Panes float as rounded cards over the view's background. */
  cards?: boolean;
}

export function SplitPanesMainView(props: SplitPanesMainViewProps) {
  const { conversationId, initialTerminalKey, readOnly = false, sandboxOffline = false } = props;
  const { terminals, listCompleteAt, isFetching } = useTerminals(conversationId);
  // Kept here, since the panes unmount while the list is empty.
  const listGeneration = useTerminalListGeneration(conversationId, terminals.length);
  const agentTerminal = useMemo(() => findAgentTerminal(terminals), [terminals]);
  const savedSplit = usePaneSplit(conversationId);
  // While the list briefly lacks the agent's terminal, the saved split keeps its pane, so the
  // split and a drag in it stay up. An empty list shows no panes.
  const agentKey = agentTerminal
    ? terminalTabKey(agentTerminal)
    : savedSplit && terminals.length > 0
      ? (terminalLayoutKeys(savedSplit.layout).find(isAgentTerminalKey) ?? null)
      : null;
  const splitsAllowed = usePanesSplitsAllowed(readOnly);
  // A shell the sidebar opened takes over the view unsplit, as the Terminal view shows it.
  const shellView =
    !!initialTerminalKey &&
    initialTerminalKey !== PANEL_NO_TERMINAL_KEY &&
    initialTerminalKey !== agentKey;
  const panes = splitsAllowed && !shellView && !sandboxOffline && agentKey !== null;
  // The panes own their shells; in their place, the Terminal view reports the ones it shows.
  usePublishMainTerminalKeys(conversationId, NO_KEYS);
  if (!panes) {
    const { chatShown: _chatShown, cards: _cards, ...terminalViewProps } = props;
    return <PublishedTerminalView {...terminalViewProps} />;
  }
  return (
    <MainSplitPanes
      {...props}
      agentKey={agentKey}
      terminals={terminals}
      listComplete={(listCompleteAt ?? 0) > 0 && !isFetching}
      listCompleteAt={listCompleteAt ?? 0}
      listGeneration={listGeneration}
    />
  );
}

interface MainSplitPanesProps extends SplitPanesMainViewProps {
  agentKey: string;
  terminals: TerminalInfo[];
  listComplete: boolean;
  listCompleteAt: number;
  listGeneration: RefObject<TerminalListGeneration>;
}

function MainSplitPanes({
  conversationId,
  agentKey,
  terminals,
  listComplete,
  listCompleteAt,
  listGeneration,
  visible = true,
  active = visible,
  readOnly = false,
  runnerOnline,
  onResume,
  onSurfaceElement,
  className,
  chatShown = false,
  cards = false,
}: MainSplitPanesProps) {
  const target = mainSplitTarget(conversationId);
  const savedSplit = usePaneSplit(conversationId);
  const appPaneTitles = useAppPaneTitles(conversationId);
  const { setTerminalConnectionState, markTerminalActive } = useTerminalStatuses(terminals);
  const panesRef = useRef<HTMLDivElement>(null);
  // One recovery per opening: a healthy agent spends it, so a later reap waits for Resume session.
  const agentRecovery = useRef<AgentRecoveryAllowance>({ attempted: false });
  const agentHealthy =
    runnerOnline === true && terminals.some((terminal) => terminalTabKey(terminal) === agentKey);
  useEffect(() => {
    if (!visible) agentRecovery.current.attempted = false;
    else if (agentHealthy) agentRecovery.current.attempted = true;
  }, [visible, agentHealthy]);

  // In Chat, a split of more than the chat pane still shows, with the web chat as its pane.
  const splitBesideChat =
    savedSplit !== undefined && terminalLayoutKeys(savedSplit.layout).length > 1;
  useEffect(() => {
    setSplitBesideChat(conversationId, splitBesideChat);
    return () => setSplitBesideChat(conversationId, false);
  }, [conversationId, splitBesideChat]);
  const chatInPane = visible && chatShown && splitBesideChat;
  const split = visible ? savedSplit : undefined;

  const keyboardOwner = usePaneKeyboardOwner(panesRef);
  useReconcileMainSplit(
    conversationId,
    agentKey,
    terminals,
    listComplete,
    listCompleteAt,
    listGeneration,
    keyboardOwner,
  );

  // The web chat renders over the agent's terminal, which stays attached at its size beneath.
  const focusChatPaneRef = useRef<() => void>(() => {});
  const chatBodyListener = useRef<(() => void) | null>(null);
  const chatBodyHostRef = useRef<HTMLElement | null>(null);
  const setChatBodyHost = useCallback(
    (element: HTMLElement | null) => {
      chatBodyListener.current?.();
      chatBodyListener.current = null;
      chatBodyHostRef.current = element;
      setChatPaneBodyHost(conversationId, element);
      if (!element) return;
      // The chat is portaled in, so focus inside it reaches the pane only through the DOM.
      const onFocusIn = () => focusChatPaneRef.current();
      element.addEventListener("focusin", onFocusIn);
      chatBodyListener.current = () => element.removeEventListener("focusin", onFocusIn);
    },
    [conversationId],
  );
  const setViewToggleHost = useChatPaneViewToggleHostRef(conversationId);

  const focusPane = (key: string) => focusSplitPane(target, key);
  const requestFocus = (key: string | null) =>
    requestTerminalFocus({ conversationId, surface: "main", tabKey: key });
  const givePaneKeyboard = (key: string) => {
    focusPane(key);
    requestFocus(key);
  };
  focusChatPaneRef.current = () => focusPane(agentKey);
  usePaneCycleHotkey({
    enabled: split !== undefined,
    paneKeys: split ? terminalLayoutKeys(split.layout) : [],
    focusedKey: split?.focusedKey,
    onFocusPane: givePaneKeyboard,
  });

  const zoomedKey = useZoomedPaneKey(target, split);
  const otherPaneMaximized = visible && zoomedKey !== null && zoomedKey !== agentKey;
  useEffect(() => {
    setOtherPaneMaximized(conversationId, otherPaneMaximized);
    return () => setOtherPaneMaximized(conversationId, false);
  }, [conversationId, otherPaneMaximized]);

  // Whether a shell pane may move to the sidebar, read as each entry point asks.
  const moveScopeRef = useRef<TerminalMoveScope>({
    agentKey,
    splitsAllowed: false,
    isLive: () => false,
  });
  moveScopeRef.current = {
    agentKey,
    splitsAllowed: split !== undefined,
    isLive: (key) => terminals.some((t) => t.running && terminalTabKey(t) === key),
  };
  // An app pane or a shell dragged by its row onto the workspace sidebar goes back there.
  const sidebarDrop = useMemo(
    () =>
      appPaneSidebarDrop(conversationId, {
        canMove: (key) =>
          terminalToSidebarRefusal(conversationId, key, moveScopeRef.current) === undefined,
        move: (key) =>
          moveTerminalToSidebar(conversationId, key, moveScopeRef.current) === undefined,
      }),
    [conversationId],
  );

  const { takeConnectFocus, focusStillHere } = useSplitPaneFocusGuards({
    conversationId,
    visible,
    split: savedSplit,
    shownPaneKeys: shownPanesOf(savedSplit, agentKey),
    panesRef,
  });

  const terminalActions = useTerminalPaneActions(target, terminals, {
    measureSizing: () => domCellSizing(panesRef.current),
  });
  // An app pane's close, the pane key's included, closes its tab as the sidebar's would.
  const actions: TerminalPaneActions = {
    ...terminalActions,
    closePane: (key) => {
      const tabId = appPaneTabId(key);
      const tabActions = tabId === null ? null : appPaneTabActions(conversationId);
      if (tabId !== null && tabActions) tabActions.close(tabId);
      else terminalActions.closePane(key);
    },
  };
  const paneName = (key: string) => {
    const tabId = appPaneTabId(key);
    if (tabId !== null) return appPaneTitles.get(tabId)?.label ?? key;
    const terminal = terminals.find((candidate) => terminalTabKey(candidate) === key);
    return terminal ? terminalDisplayLabel(terminal) : key;
  };
  const paneCommands = useTerminalPaneHotkeys({
    target,
    // Splits are offered in the agent's Terminal view, and from its chat in Chat.
    enabled: visible || chatShown,
    split,
    singlePaneKey: agentKey,
    chatPane: chatShown ? { key: agentKey, sessionId: conversationId } : null,
    defaultForPalette: true,
    actions,
    panesRef,
    paneName,
  });
  // Listed by the same rule as the pane commands: only while these panes last had the keyboard.
  const listsPaneCommands = usePaneCommandsListed(target.id, visible || chatShown, true);
  useMoveToSidebarCommand(conversationId, listsPaneCommands ? split : undefined, moveScopeRef);

  const paneMenuFor = (key: string): SessionPaneMenu | undefined =>
    visible && actions.shellOptions.length > 0
      ? {
          onSplit: (direction) =>
            actions.splitPane(key, direction === "right" ? "columns" : "rows"),
          splitting: actions.splitting || actions.closingSplit,
          commands: () => paneCommands(key),
        }
      : undefined;

  // The sidebar leaves out every terminal this view mounts.
  usePublishMainTerminalKeys(conversationId, shownPanesOf(split, agentKey));

  // Becoming the focused pane in a shown view takes the keyboard unless the person moved it.
  const wasVisibleRef = useRef(visible);
  useEffect(() => {
    wasVisibleRef.current = visible;
  });
  const focusOnActivate = () => wasVisibleRef.current && focusStillHere();
  const pendingFocus = usePendingTerminalFocus(conversationId, "main");
  // The pane a request is for, once the view shows it: the split's focused pane for the view's
  // own terminal or one of its panes, else the agent's lone pane.
  const focusRecipient = (() => {
    if (!pendingFocus || !visible) return null;
    const { tabKey } = pendingFocus;
    if (split) {
      return tabKey === null || terminalLayoutKeys(split.layout).includes(tabKey)
        ? split.focusedKey
        : null;
    }
    return tabKey === null || tabKey === agentKey ? agentKey : null;
  })();
  // Focus asked of the chat pane while it shows the web chat goes to the chat's composer.
  const chatTakesFocus = chatInPane && focusRecipient === agentKey;
  const chatFocusId = chatTakesFocus ? pendingFocus?.id : undefined;
  useEffect(() => {
    if (chatFocusId === undefined) return;
    const settle = () => settleTerminalFocus(conversationId, "main", chatFocusId);
    const host = chatBodyHostRef.current;
    if (focusChatComposer(host)) {
      settle();
      return;
    }
    if (!host) return;
    // The chat is portaled into the pane a render later: the request waits for its composer.
    const observer = new MutationObserver(() => {
      if (!focusChatComposer(host)) return;
      observer.disconnect();
      settle();
    });
    observer.observe(host, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [chatFocusId, conversationId]);
  // A focus move onto the chat pane from another pane's terminal lands in the chat's composer.
  const chatPaneFocused = chatInPane && split?.focusedKey === agentKey;
  useEffect(() => {
    if (!chatPaneFocused) return;
    const focused = document.activeElement;
    if (focused?.closest(TERMINAL_SURFACE) && panesRef.current?.contains(focused)) {
      focusChatComposer(chatBodyHostRef.current);
    }
  }, [chatPaneFocused]);
  const requestFor = (key: string) =>
    pendingFocus && focusRecipient === key && !chatTakesFocus ? pendingFocus.id : undefined;

  const setSurfaceElement = useCallback(
    (element: HTMLDivElement | null) => {
      // A hidden view keeps its terminals attached but out of the tab order.
      element?.toggleAttribute("inert", !visible);
      onSurfaceElement?.(element);
    },
    [onSurfaceElement, visible],
  );

  const stripPaneOf = (paneKey: string): PaneStripPane | null => {
    const appTabId = appPaneTabId(paneKey);
    if (appTabId !== null) {
      const title = appPaneTitles.get(appTabId);
      const row = sessionPaneStrip(title?.label ?? "", paneMenuFor(paneKey), {
        icon: appPaneIcon(title),
        ...(title?.tooltip ? { tooltip: title.tooltip } : {}),
        focus: () => focusAppPane(conversationId, appTabId),
      });
      // Close closes the tab, as the sidebar's would; Move to Sidebar keeps it.
      const tabActions = appPaneTabActions(conversationId);
      return {
        ...row,
        closeLabel: "Close Tab",
        commands: tabActions
          ? {
              ...row.commands,
              close: () => tabActions.close(appTabId),
              moveToSidebar: () => tabActions.moveToSidebar(appTabId),
            }
          : row.commands,
      };
    }
    const terminal = terminals.find((t) => terminalTabKey(t) === paneKey);
    if (!terminal) return null;
    const focus = () => givePaneKeyboard(paneKey);
    // The agent's pane is the session's chat pane: it reads a step stronger than the panes
    // beside it. It carries the Chat/Terminal switch only while a split keeps these panes up in
    // both views; otherwise the switch stays put in the header as the view changes.
    if (paneKey === agentKey) {
      return {
        ...sessionPaneStrip(
          chatInPane ? "Chat" : terminalDisplayLabel(terminal),
          paneMenuFor(paneKey),
          { icon: chatInPane ? <MessageSquareIcon /> : undefined, focus },
        ),
        primary: true,
        viewToggleHostRef: visible && splitBesideChat ? setViewToggleHost : undefined,
      };
    }
    const row = sessionPaneStrip(terminalDisplayLabel(terminal), paneMenuFor(paneKey), { focus });
    // A shell keeps running as it moves: its tab in the sidebar shows it again.
    if (!row.commands || terminalToSidebarRefusal(conversationId, paneKey, moveScopeRef.current)) {
      return row;
    }
    return {
      ...row,
      commands: {
        ...row.commands,
        moveToSidebar: () =>
          void moveTerminalToSidebar(conversationId, paneKey, moveScopeRef.current),
      },
    };
  };

  const runnerOffline = runnerOnline === false;
  // A lone pane is the chat card's content; only a split's panes float as cards of their own.
  const splitCards = cards && splitBesideChat;
  return (
    <div
      ref={setSurfaceElement}
      data-testid="split-panes-main-view"
      data-visible={visible}
      {...{ [TERMINAL_FOCUS_SURFACE_ATTRIBUTE]: terminalFocusSurfaceId(conversationId, "main") }}
      className={cn(
        // Floating panes' styling places a lone pane in the chat's card by this class.
        "split-panes-main-view flex min-h-0 flex-1 flex-col",
        BELOW_SESSION_HEADER,
        // A split's cards float on the shell's backdrop, a small margin from the column's edges;
        // the gaps fall back to the panes' own where the page's styles don't set them.
        splitCards &&
          "pr-[calc(var(--pane-gap,5px)-var(--pane-cell-gap,3px))] pb-[calc(var(--pane-gap,5px)-var(--pane-cell-gap,3px))] pl-[var(--pane-gap-start,var(--pane-gap,5px))]",
        className,
      )}
    >
      <SplitPanesLayout
        conversationId={conversationId}
        split={split}
        singlePaneKey={agentKey}
        paneIdOf={(key) => terminals.find((t) => terminalTabKey(t) === key)?.id ?? key}
        onFocusPane={focusPane}
        onResize={(resizes) =>
          target.update((current) =>
            current
              ? { ...current, layout: applyDividerResizes(current.layout, resizes) }
              : current,
          )
        }
        panesRef={panesRef}
        cards={splitCards}
        zoomedKey={zoomedKey}
        onUnzoom={(key) => {
          unzoomPane(target);
          requestFocus(key);
        }}
        onMovePane={
          split
            ? (move) => {
                moveSplitPane(target, move, domCellSizing(panesRef.current));
                requestFocus(move.key);
              }
            : undefined
        }
        refocusPane={requestFocus}
        paneDragOutside={sidebarDrop}
        bodySlot={chatInPane ? { paneKey: agentKey, ref: setChatBodyHost } : undefined}
        strip={{ paneOf: stripPaneOf }}
        renderPane={(paneKey, focused) => {
          const appTabId = appPaneTabId(paneKey);
          if (appTabId !== null) {
            return (
              <AppPaneHost
                conversationId={conversationId}
                tabId={appTabId}
                onFocusIn={() => focusPane(paneKey)}
                active={visible && active && focused}
                focusOnActivate={focusOnActivate}
                focusRequest={requestFor(paneKey)}
                onFocusRequestTaken={(id) => settleTerminalFocus(conversationId, "main", id)}
              />
            );
          }
          const terminal = terminals.find((t) => terminalTabKey(t) === paneKey);
          if (!terminal && paneKey === agentKey) {
            return (
              <MissingAgentPane
                conversationId={conversationId}
                onResume={onResume}
                visible={visible}
                readOnly={readOnly}
                listSettled={listComplete}
                allowance={agentRecovery}
              />
            );
          }
          if (!terminal) {
            return (
              <MissingTerminalPane
                target={target}
                paneKey={paneKey}
                focused={visible && active && focused}
                terminals={terminals}
                readOnly={readOnly}
                fallback="Terminal unavailable."
              />
            );
          }
          // Under the web chat, the agent's terminal keeps no keyboard.
          const underChat = chatInPane && paneKey === agentKey;
          return (
            <TerminalPaneSurface
              conversationId={conversationId}
              terminal={terminal}
              readOnly={readOnly}
              active={visible && active && focused && !underChat}
              focusOnConnect={() =>
                split !== undefined &&
                takeConnectFocus(paneKey) &&
                visible &&
                active &&
                focused &&
                focusStillHere()
              }
              focusRequest={requestFor(paneKey)}
              onFocusRequestTaken={(id) => settleTerminalFocus(conversationId, "main", id)}
              onStateChange={(state) => setTerminalConnectionState(terminal.id, state)}
              onActivity={() => markTerminalActive(terminal.id)}
              onResume={runnerOffline ? onResume : undefined}
            />
          );
        }}
      />
      {(visible || chatShown) && <PaneCloseDialog actions={actions} />}
    </div>
  );
}

/**
 * Keep the saved split to terminals that exist: a shell that ended leaves, and a stale agent
 * pane moves onto the live agent terminal. Workspace tabs and failed starts stay.
 */
function useReconcileMainSplit(
  conversationId: string,
  agentKey: string,
  terminals: readonly TerminalInfo[],
  listComplete: boolean,
  listCompleteAt: number,
  listGeneration: RefObject<TerminalListGeneration>,
  keyboardOwner: () => string | null,
): void {
  useEffect(() => {
    const generation = currentTerminalListGeneration(
      listGeneration,
      conversationId,
      terminals.length,
    );
    if (terminals.length === 0) return;
    const liveKeys = new Set(terminals.map(terminalTabKey));
    const complete = listComplete && listCompleteAt > generation.startedAt;
    const { seen } = generation;
    const target = mainSplitTarget(conversationId);
    const before = target.read();
    const owner = keyboardOwner();
    target.update(
      (current) =>
        current &&
        reconcileTerminalSplit(current, withAppPanes(liveKeys, current), {
          liveAgentKey: agentKey,
          isAgentKey: isAgentTerminalKey,
          removable: (key) =>
            !isFailedPaneStart(conversationId, key) && (complete || seen.has(key)),
        }),
    );
    for (const key of liveKeys) seen.add(key);
    // The pane with the keyboard lost its shell: the pane beside it takes the keyboard.
    if (owner === null || !before || liveKeys.has(owner)) return;
    const was = terminalLayoutKeys(before.layout);
    const after = target.read();
    const now = after ? terminalLayoutKeys(after.layout) : [];
    if (!was.includes(owner) || now.includes(owner)) return;
    const kept = withAppPanes(liveKeys, before);
    const survivor = was.find((key) => key !== owner && kept.has(key)) ?? null;
    giveNextPaneTheKeyboard(target, after?.focusedKey ?? survivor);
  }, [
    conversationId,
    agentKey,
    terminals,
    listComplete,
    listCompleteAt,
    listGeneration,
    keyboardOwner,
  ]);
}

/** ⌘K's Move Pane to Sidebar, for the split's focused workspace tab or shell. */
function useMoveToSidebarCommand(
  conversationId: string,
  split: TerminalSplitState | undefined,
  moveScopeRef: { current: TerminalMoveScope },
): void {
  const focusedKey = split?.focusedKey ?? null;
  const focusedAppTab = focusedKey === null ? null : appPaneTabId(focusedKey);
  const focusedShell =
    focusedKey !== null &&
    focusedAppTab === null &&
    terminalToSidebarRefusal(conversationId, focusedKey, moveScopeRef.current) === undefined
      ? focusedKey
      : null;
  usePaletteCommands(
    `pane-to-sidebar:${conversationId}`,
    "Panes",
    useMemo<PaletteCommand[]>(() => {
      const moved = focusedAppTab ?? focusedShell;
      if (moved === null) return [];
      const run =
        focusedAppTab !== null
          ? () => appPaneTabActions(conversationId)?.moveToSidebar(focusedAppTab)
          : () => {
              // Rechecked as it runs: the shell may have ended since the palette opened.
              const refusal = moveTerminalToSidebar(conversationId, moved, moveScopeRef.current);
              if (refusal) showToast(refusal);
            };
      return [
        {
          id: `pane-to-sidebar:${conversationId}:${moved}`,
          label: "Move Pane to Sidebar",
          icon: PanelRightIcon,
          keywords: ["pane", "sidebar", "tab", "shell", "move", "dock", "workspace"],
          // The sidebar's tab takes the keyboard once the palette has closed.
          movesFocus: true,
          run,
        },
      ];
    }, [conversationId, focusedAppTab, focusedShell, moveScopeRef]),
  );
}

/** Focus the composer of the chat rendered in ``host``, this view's chat pane body; false if none. */
function focusChatComposer(host: HTMLElement | null): boolean {
  const composer = host
    ?.querySelector(CHAT_PANE_BODY)
    ?.querySelector<HTMLElement>("textarea, [contenteditable='true']");
  if (!composer) return false;
  composer.focus();
  return document.activeElement === composer;
}

/** The live terminals' keys, with the workspace tabs `split` shows as panes, which stay. */
function withAppPanes(liveKeys: ReadonlySet<string>, split: TerminalSplitState): Set<string> {
  return new Set([...liveKeys, ...terminalLayoutKeys(split.layout).filter(isAppPaneKey)]);
}

/** An app pane's row icon: a file's type icon for a file, else its tab's own. */
function appPaneIcon(title: AppPaneTitle | undefined): ReactNode {
  if (!title) return undefined;
  if (title.filePath !== undefined) return <FileTypeIcon path={title.filePath} />;
  const Icon = title.icon;
  return <Icon />;
}
