// A split pane whose shell didn't start: keeping its slot, trying again into the same slot, and
// closing it.

import {
  type TerminalInfo,
  type TerminalLaunchCallbacks,
  terminalTabKey,
} from "@/hooks/useTerminals";
import { replaceTerminalLayoutKey, terminalLayoutKeys } from "@/lib/paneSplitLayout";
import { TERMINAL_PANE, TERMINAL_SURFACE } from "@/lib/paneSurfaces";
import {
  anyTerminalLaunching,
  subscribeTerminalLaunches,
  terminalLaunchMode,
} from "@/lib/terminals";

import { liveSplitMember, removePaneFocusingNeighbor } from "./paneShellClose";
import {
  clearPaneStartFailure,
  isFailedPaneStart,
  paneStartFailure,
  recordPaneStartFailure,
} from "./paneStartFailures";
import { requestTerminalFocus } from "./terminalFocusRequest";
import { requestTerminalOpen } from "./terminalOpenRequest";
import type { TerminalSplitTarget } from "./terminalSplitTarget";

/** Whether ``key`` is still one of ``target``'s panes. */
export function isSplitPane(target: TerminalSplitTarget, key: string): boolean {
  const layout = target.read()?.layout;
  return layout !== undefined && terminalLayoutKeys(layout).includes(key);
}

/** Starts a shell, as a session's ``useCreateTerminal().launch`` does. */
export type PaneShellLauncher = (terminal: string, callbacks: TerminalLaunchCallbacks) => void;

/** Keep the failed start's slot in ``target`` with why it failed, if the slot is still there. */
export function keepFailedPaneStart(
  target: TerminalSplitTarget,
  opened: TerminalInfo,
  error: Error,
): boolean {
  const key = terminalTabKey(opened);
  if (!isSplitPane(target, key)) return false;
  recordPaneStartFailure(target.conversationId, key, {
    terminal: opened.name,
    reason: error.message,
  });
  // The sidebar shows a split through a member's tab; with this one's gone, it shows another.
  if (target.focusSurface === "rail") {
    const shown = liveSplitMember(target);
    if (shown) requestTerminalOpen(target.conversationId, { key: shown, focus: false });
  }
  return true;
}

/**
 * Try Again: start the failed slot's shell again into the same slot. The new shell takes the
 * keyboard once it starts only if the slot had it and nothing moved it meanwhile. False while
 * another shell is starting in the session, which takes the one launch at a time.
 */
export function retryPaneStart(
  target: TerminalSplitTarget,
  key: string,
  launch: PaneShellLauncher,
): boolean {
  const { conversationId } = target;
  const failure = paneStartFailure(conversationId, key);
  if (!failure || anyTerminalLaunching(conversationId) || !isSplitPane(target, key)) return false;
  const hadFocus = target.read()?.focusedKey === key && focusInPaneOrNowhere(key);
  clearPaneStartFailure(conversationId, key);
  // The slot follows the shell's key: the launch's guess first, then the server's if it differs.
  let slotKey = key;
  launch(failure.terminal, {
    onOpen: (info) => {
      const newKey = terminalTabKey(info);
      const from = slotKey;
      target.update((current) =>
        current && terminalLayoutKeys(current.layout).includes(from)
          ? {
              layout: replaceTerminalLayoutKey(current.layout, from, newKey),
              focusedKey: current.focusedKey === from ? newKey : current.focusedKey,
            }
          : current,
      );
      slotKey = newKey;
      // Until the panes render the new key, the keyboard may still be in the slot's old one.
      if (hadFocus) focusWhenLaunched(target, newKey, info.id, from);
    },
    // Failed again, the slot says why; closed meanwhile, there's nothing left to tell.
    onError: (error, opened) => {
      if (opened) keepFailedPaneStart(target, opened, error);
    },
  });
  return true;
}

/** Close Pane: the failed slot goes, and the pane beside it takes the keyboard. */
export function closeFailedPaneStart(target: TerminalSplitTarget, key: string): void {
  removePaneFocusingNeighbor(target, key, () => clearPaneStartFailure(target.conversationId, key));
}

/**
 * A failed slot's Try Again takes the keyboard only from nowhere, its own pane, or a terminal in
 * the same split; never from a field, a menu or dialog, or a hidden view.
 */
export function failedPaneMayTakeFocus(control: HTMLElement): boolean {
  const pane = control.closest<HTMLElement>(TERMINAL_PANE);
  if (!pane || pane.hasAttribute("data-pane-hidden") || pane.closest("[aria-hidden='true']")) {
    return false;
  }
  const focused = document.activeElement;
  if (focused === null || focused === document.body || pane.contains(focused)) return true;
  const focusedPane = focused.closest(TERMINAL_SURFACE)?.closest(TERMINAL_PANE);
  return !!focusedPane && focusedPane.parentElement === pane.parentElement;
}

function focusInPaneOrNowhere(...keys: string[]): boolean {
  const focused = document.activeElement;
  if (focused === null || focused === document.body) return true;
  const paneKey = focused.closest<HTMLElement>(TERMINAL_PANE)?.dataset.terminalPane;
  return paneKey !== undefined && keys.includes(paneKey);
}

function focusWhenLaunched(
  target: TerminalSplitTarget,
  key: string,
  terminalId: string,
  previousKey: string,
): void {
  const request = () => {
    // Failed again, the slot shows why with Try Again: no terminal is there to take a request.
    if (
      target.read()?.focusedKey !== key ||
      isFailedPaneStart(target.conversationId, key) ||
      !focusInPaneOrNowhere(key, previousKey)
    ) {
      return;
    }
    requestTerminalFocus({
      conversationId: target.conversationId,
      surface: target.focusSurface,
      tabKey: key,
    });
  };
  if (terminalLaunchMode(terminalId) === null) return request();
  const unsubscribe = subscribeTerminalLaunches(() => {
    if (terminalLaunchMode(terminalId) !== null) return;
    unsubscribe();
    request();
  });
}
