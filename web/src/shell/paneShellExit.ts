// A split pane whose shell exits closes at once, as Close Pane does. Its attach reports the end
// within milliseconds of `exit`, seconds before the terminals list drops the shell.

import { useCallback, useRef } from "react";

import type { ConnectionState } from "@/components/blocks/TerminalSession";
import { terminalLayoutKeys } from "@/lib/paneSplitLayout";

import { removePaneFocusingNeighbor } from "./paneShellClose";
import type { TerminalSplitTarget } from "./terminalSplitTarget";

/** The server's close code for an attach whose terminal is gone (`WS_CLOSE_TERMINAL_NOT_FOUND`). */
const TERMINAL_GONE_CODE = 4404;
/**
 * The reason the runner's control bridge gives only once a live attach's pane is dead. Its other
 * 4404s refuse an attach after accepting it, which a passing hiccup can cause too.
 */
const SESSION_ENDED_REASON = "terminal session ended";

const endedShells = new Map<string, Set<string>>();

/** Record that the shell in ``conversationId``'s pane ``key`` has ended. */
export function markPaneShellEnded(conversationId: string, key: string): void {
  const keys = endedShells.get(conversationId) ?? new Set<string>();
  keys.add(key);
  endedShells.set(conversationId, keys);
}

/** Whether the shell in ``conversationId``'s pane ``key`` has ended, so closing it ends nothing. */
export function isPaneShellEnded(conversationId: string, key: string): boolean {
  return endedShells.get(conversationId)?.has(key) ?? false;
}

export function resetEndedPaneShellsForTests(): void {
  endedShells.clear();
}

/**
 * Whether a connection change means the pane's shell ended: the server ended an attach that had
 * connected because its pane died. Any other close, such as a refused attach or a transport drop,
 * leaves the pane to the terminals list, as before.
 */
export function shellEndedOnClose(state: ConnectionState | null, wasConnected: boolean): boolean {
  return (
    wasConnected &&
    state?.kind === "closed" &&
    state.code === TERMINAL_GONE_CODE &&
    state.reason === SESSION_ENDED_REASON
  );
}

/**
 * Watch the split's shell panes, given each connection change as ``observe(paneKey, state)``: a
 * pane whose shell ends leaves the split and the pane beside it takes the keyboard. Panes the
 * split can't close, such as the agent's own, stay.
 */
export function usePaneShellExit(
  target: TerminalSplitTarget,
): (paneKey: string, state: ConnectionState | null) => void {
  const connected = useRef(new Set<string>());
  const latestTarget = useRef(target);
  latestTarget.current = target;
  return useCallback((paneKey, state) => {
    if (state?.kind === "connected") {
      connected.current.add(paneKey);
      return;
    }
    if (!shellEndedOnClose(state, connected.current.has(paneKey))) return;
    connected.current.delete(paneKey);
    const current = latestTarget.current;
    if (!current.canClosePane(paneKey)) return;
    markPaneShellEnded(current.conversationId, paneKey);
    const split = current.read();
    if (split && terminalLayoutKeys(split.layout).includes(paneKey)) {
      removePaneFocusingNeighbor(current, paneKey);
    }
  }, []);
}
