// What a split's pane commands do, shared by the pane keys, ⌘K and each pane's row so they act on
// the same pane with the same rules: split with a new shell, and close a pane, asking first.

import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";

import { showToast } from "@/components/ui/toast";
import { useSessionAgent } from "@/hooks/useAgents";
import {
  findAgentTerminal,
  terminalTabKey,
  type TerminalInfo,
  useCreateTerminal,
  useDeleteTerminal,
} from "@/hooks/useTerminals";
import {
  replaceTerminalLayoutKey,
  terminalLayoutKeys,
  type TerminalPanePlacement,
  type TerminalSplitAxis,
} from "@/lib/paneSplitLayout";
import { APP_PANE_HOST, CHAT_PANE_BODY, TERMINAL_PANE, TERMINAL_SURFACE } from "@/lib/paneSurfaces";
import {
  anyTerminalLaunching,
  subscribeTerminalLaunches,
  terminalLaunchMode,
} from "@/lib/terminals";

import { closePaneShell, removePaneFocusingNeighbor } from "./paneShellClose";
import {
  closeFailedPaneStart,
  isSplitPane,
  keepFailedPaneStart,
  retryPaneStart,
} from "./paneShellStart";
import { NO_ROOM_MESSAGE, type PaneSizing } from "./paneSizing";
import { isFailedPaneStart } from "./paneStartFailures";
import { resolveDefaultShell, writePreferredShell } from "./preferredShell";
import {
  insertSplitPane,
  measureSplitSizing,
  PANE_CLOSED_MESSAGE,
  registerSplitSizing,
  roomToSplit,
  SPLIT_CLOSING_MESSAGE,
} from "./splitTargetActions";
import { isClosingShell, useShellsClosing } from "./terminalGroupsStore";
import type { TerminalSplitTarget } from "./terminalSplitTarget";

/** A shell pane waiting on the person to confirm its shell may end. */
export interface PendingPaneClose {
  key: string;
  terminal: TerminalInfo;
}

// The close each split is asking about, shared by its hosts so one dialog answers it.
const pendingCloses = new Map<string, PendingPaneClose>();
const listeners = new Set<() => void>();

function setPendingClose(targetId: string, pending: PendingPaneClose | null): void {
  if ((pendingCloses.get(targetId) ?? null) === pending) return;
  if (pending) pendingCloses.set(targetId, pending);
  else pendingCloses.delete(targetId);
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function usePendingClose(targetId: string): PendingPaneClose | null {
  const getSnapshot = useCallback(() => pendingCloses.get(targetId) ?? null, [targetId]);
  return useSyncExternalStore(subscribe, getSnapshot);
}

/** Forget every pending close, for tests. */
export function resetPendingPaneClosesForTests(): void {
  pendingCloses.clear();
  for (const listener of listeners) listener();
}

/** A shell is starting in the session, from any entry point, so a split waits for it. */
function useShellLaunching(conversationId: string): boolean {
  return useSyncExternalStore(subscribeTerminalLaunches, () =>
    anyTerminalLaunching(conversationId),
  );
}

/** Whether ``key`` is a pane the view shows now: in the split, or the lone unsplit pane. */
function isLivePane(target: TerminalSplitTarget, key: string, agent: TerminalInfo | null): boolean {
  const current = target.read();
  if (current) return terminalLayoutKeys(current.layout).includes(key);
  return target.unsplitKey(agent) === key;
}

/**
 * Focus is where it was when the split was asked for, back in the source pane (where a menu that
 * asked returns it), or nowhere (the menu closed).
 */
function focusUnmoved(focusAtRequest: Element | null, sourceKey: string): boolean {
  const focused = document.activeElement;
  if (focused === null || focused === document.body || focused === focusAtRequest) return true;
  const inPane =
    focused.closest(TERMINAL_SURFACE) ??
    focused.closest(CHAT_PANE_BODY) ??
    focused.closest(APP_PANE_HOST);
  return inPane?.closest<HTMLElement>(TERMINAL_PANE)?.dataset.terminalPane === sourceKey;
}

/** A new pane whose shell is still starting takes the keyboard once it can, if still wanted. */
function focusSplitPaneWhenLaunched(
  target: TerminalSplitTarget,
  { terminalId, sourceKey, newKey }: { terminalId: string; sourceKey: string; newKey: string },
  stillWanted: () => boolean,
): void {
  const unsubscribe = subscribeTerminalLaunches(() => {
    if (terminalLaunchMode(terminalId) !== null) return;
    unsubscribe();
    // A shell that failed to start leaves its slot saying why; the keyboard doesn't move there.
    if (!stillWanted() || isFailedPaneStart(target.conversationId, newKey)) return;
    target.update((current) =>
      current &&
      current.focusedKey === sourceKey &&
      terminalLayoutKeys(current.layout).includes(newKey)
        ? { ...current, focusedKey: newKey }
        : current,
    );
  });
}

/** What the pane keys and ⌘K need from whatever starts and ends a split's panes. */
export interface PaneCommands {
  /** A pane can be split: the agent declares a shell to start. */
  canSplit: boolean;
  /** A shell is starting, so another split waits. */
  splitting: boolean;
  /** A close is ending this split's panes, so it can't split until that finishes. */
  closingSplit: boolean;
  splitPane: (sourceKey: string, axis: TerminalSplitAxis) => void;
  canClosePane: (key: string) => boolean;
  closePane: (key: string) => void;
}

export interface TerminalPaneActions extends PaneCommands {
  agentTerminal: TerminalInfo | null;
  /** Declared shells, the default first; empty when the agent declares none. */
  shellOptions: string[];
  /** Split ``sourceKey`` with ``shell``, the default shell when omitted. */
  splitPane: (
    sourceKey: string,
    axis: TerminalSplitAxis,
    shell?: string,
    placement?: TerminalPanePlacement,
  ) => void;
  /** Close ``key``'s pane: a shell's after the person confirms, a failed start's at once. */
  closePane: (key: string) => void;
  pendingClose: PendingPaneClose | null;
  confirmClose: () => void;
  cancelClose: () => void;
  /** Start a failed slot's shell again, into the same slot. */
  retryPaneStart: (key: string) => void;
}

/** The pane commands for one split and its session's live ``terminals``, wherever offered. */
export function useTerminalPaneActions(
  target: TerminalSplitTarget,
  terminals: readonly TerminalInfo[],
  { measureSizing }: { measureSizing?: () => PaneSizing | null } = {},
): TerminalPaneActions {
  const { conversationId } = target;
  // The host showing the panes measures them; every other entry point checks against it.
  const latestMeasure = useRef(measureSizing);
  latestMeasure.current = measureSizing;
  const hostsView = measureSizing !== undefined;
  useEffect(() => {
    if (!hostsView) return;
    return registerSplitSizing(target.id, () => latestMeasure.current?.() ?? null);
  }, [hostsView, target.id]);
  const { data: agent } = useSessionAgent(conversationId);
  const latestTerminals = useRef(terminals);
  latestTerminals.current = terminals;
  const create = useCreateTerminal(conversationId);
  const endShell = useDeleteTerminal(conversationId);
  const pendingClose = usePendingClose(target.id);
  const splitting = useShellLaunching(conversationId);
  const splitKeys = () => {
    const layout = target.read()?.layout;
    if (layout) return terminalLayoutKeys(layout);
    const lone = target.unsplitKey(findAgentTerminal([...latestTerminals.current]));
    return lone === null ? [] : [lone];
  };
  const closingSplit = useShellsClosing(conversationId, splitKeys);
  const declaredShells = agent?.terminals ?? [];
  const defaultShell = resolveDefaultShell(declaredShells);
  const shellOptions =
    defaultShell === null
      ? []
      : [defaultShell, ...declaredShells.filter((name) => name !== defaultShell)];

  const splitPane = (
    sourceKey: string,
    axis: TerminalSplitAxis,
    shell = shellOptions[0],
    placement: TerminalPanePlacement = "after",
  ) => {
    if (shell === undefined || anyTerminalLaunching(conversationId)) return;
    // A menu or ⌘K row can outlive its pane; don't start a shell nothing will show.
    const agentNow = findAgentTerminal([...latestTerminals.current]);
    if (!isLivePane(target, sourceKey, agentNow)) {
      showToast(PANE_CLOSED_MESSAGE);
      return;
    }
    if (splitKeys().some((key) => isClosingShell(conversationId, key))) {
      showToast(SPLIT_CLOSING_MESSAGE);
      return;
    }
    // Every pane of the layout the split would make keeps the minimum, not just the two halves.
    if (!roomToSplit(target, sourceKey, axis, placement, target.unsplitKey(agentNow))) {
      showToast(measureSplitSizing(target)?.refusal(axis) ?? NO_ROOM_MESSAGE);
      return;
    }
    // Only a shell picked over the default is remembered, as the header's split menu does.
    if (shell !== shellOptions[0]) writePreferredShell(shell);
    const focusAtRequest = document.activeElement;
    let openedInSplit = false;
    // The pane's key: the launch's guess first, then the server's if its id differs.
    let slotKey: string | null = null;
    let firstOpenSeen = false;
    create.launch(shell, {
      onOpen: (info) => {
        const newKey = terminalTabKey(info);
        // The server named the shell differently: its pane takes the new key in place.
        if (firstOpenSeen) {
          if (slotKey === null || slotKey === newKey || !isSplitPane(target, slotKey)) return;
          const from = slotKey;
          const takeFocus =
            terminalLaunchMode(info.id) === null && focusUnmoved(focusAtRequest, sourceKey);
          target.update(
            (current) =>
              current && {
                layout: replaceTerminalLayoutKey(current.layout, from, newKey),
                focusedKey:
                  current.focusedKey === from || (takeFocus && current.focusedKey === sourceKey)
                    ? newKey
                    : current.focusedKey,
              },
          );
          slotKey = newKey;
          return;
        }
        firstOpenSeen = true;
        // Read as the shell arrives: the agent terminal may have been replaced meanwhile.
        const agentTerminal = findAgentTerminal([...latestTerminals.current]);
        const unsplitKey = target.unsplitKey(agentTerminal);
        // The view may have shrunk while the shell started: it then stays its own tab.
        if (!roomToSplit(target, sourceKey, axis, placement, unsplitKey)) {
          showToast(measureSplitSizing(target)?.refusal(axis) ?? NO_ROOM_MESSAGE);
          return;
        }
        // A pane whose shell is still starting can't take the keyboard yet; it does once it can.
        const launching = terminalLaunchMode(info.id) !== null;
        const inserted = insertSplitPane(target, {
          sourceKey,
          newKey,
          axis,
          placement,
          unsplitKey,
          takeFocus: !launching && focusUnmoved(focusAtRequest, sourceKey),
        });
        openedInSplit = inserted || openedInSplit;
        if (inserted) slotKey = newKey;
        if (launching) {
          focusSplitPaneWhenLaunched(target, { terminalId: info.id, sourceKey, newKey }, () =>
            focusUnmoved(focusAtRequest, sourceKey),
          );
        }
      },
      onError: (error, opened) => {
        // A pane opened before the create failed keeps its slot, saying why, with Try Again.
        if (opened && keepFailedPaneStart(target, opened, error)) return;
        if (opened && openedInSplit) return;
        showToast(`Couldn't open a shell: ${error.message}`);
      },
    });
  };

  const canClosePane = (key: string) => target.canClosePane(key);

  /** ``key`` is still a pane the view shows; a menu, ⌘K row or dialog can outlive its pane. */
  const paneStillShown = (key: string) => {
    if (isLivePane(target, key, findAgentTerminal([...latestTerminals.current]))) return true;
    showToast(PANE_CLOSED_MESSAGE);
    return false;
  };

  const closePane = (key: string) => {
    if (!canClosePane(key) || !paneStillShown(key)) return;
    // Only ending a shell asks first; a pane with no shell running closes at once.
    if (isFailedPaneStart(conversationId, key)) {
      closeFailedPaneStart(target, key);
      return;
    }
    const terminal = latestTerminals.current.find((candidate) => terminalTabKey(candidate) === key);
    if (!terminal) removePaneFocusingNeighbor(target, key);
    else setPendingClose(target.id, { key, terminal });
  };

  const confirmClose = () => {
    const pending = pendingCloses.get(target.id) ?? null;
    setPendingClose(target.id, null);
    if (!pending || !target.canClosePane(pending.key) || !paneStillShown(pending.key)) return;
    // The pane goes now; a shell that won't end says so and stays as its own tab.
    closePaneShell(target, pending.key, pending.terminal.id, (id) => endShell.mutate(id));
  };

  return {
    agentTerminal: findAgentTerminal([...terminals]),
    shellOptions,
    canSplit: shellOptions.length > 0,
    splitting,
    closingSplit,
    splitPane,
    canClosePane,
    closePane,
    pendingClose,
    confirmClose,
    cancelClose: () => setPendingClose(target.id, null),
    retryPaneStart: (key) => void retryPaneStart(target, key, create.launch),
  };
}
