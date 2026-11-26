// A split pane whose shell exits closes at once, as Close Pane does. Its attach reports the end
// within milliseconds of `exit`, seconds before the terminals list drops the shell.

import { useCallback, useRef } from "react";

import type { ConnectionState } from "@/components/blocks/TerminalSession";
import { terminalLayoutKeys } from "@/lib/paneSplitLayout";

import { removePaneFocusingNeighbor, removeSplitPane } from "./paneShellClose";
import type { TerminalSplitTarget } from "./terminalSplitTarget";

/** The server's close code for an attach whose terminal is gone (`WS_CLOSE_TERMINAL_NOT_FOUND`). */
const TERMINAL_GONE_CODE = 4404;
/**
 * The reason the runner's control bridge gives only once a live attach's pane is dead. Its other
 * 4404s refuse an attach after accepting it, which a passing hiccup can cause too.
 */
const SESSION_ENDED_REASON = "terminal session ended";

/**
 * How long an ended shell counts as ended: past the seconds the terminals list takes to drop it.
 * A shell relaunched later under the same name gets the same key, and must be asked about again.
 */
export const ENDED_SHELL_MEMORY_MS = 10_000;

/** When each pane's shell ended, by conversation and pane key. */
const endedShells = new Map<string, Map<string, number>>();

/** Record that the shell in ``conversationId``'s pane ``key`` has ended. */
export function markPaneShellEnded(conversationId: string, key: string): void {
  const now = performance.now();
  for (const [conversation, keys] of endedShells) {
    for (const [endedKey, endedAt] of keys) {
      if (now - endedAt >= ENDED_SHELL_MEMORY_MS) keys.delete(endedKey);
    }
    if (keys.size === 0) endedShells.delete(conversation);
  }
  const keys = endedShells.get(conversationId) ?? new Map<string, number>();
  keys.set(key, now);
  endedShells.set(conversationId, keys);
}

/** Forget an ended shell once its pane connects again, to a shell that is running. */
export function forgetPaneShellEnded(conversationId: string, key: string): void {
  endedShells.get(conversationId)?.delete(key);
}

/** Whether the shell in ``conversationId``'s pane ``key`` just ended, so closing it ends nothing. */
export function isPaneShellEnded(conversationId: string, key: string): boolean {
  const endedAt = endedShells.get(conversationId)?.get(key);
  return endedAt !== undefined && performance.now() - endedAt < ENDED_SHELL_MEMORY_MS;
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
 * Watch the split's shell panes, given each connection change as ``observe(paneKey, state)``. A
 * pane that connects is no longer taken as ended, on every surface. With `removesEndedPanes`, a
 * pane whose shell ends leaves the split; panes the split can't close, such as the agent's own,
 * stay. The pane beside it takes the keyboard only if ``keyboardOwner`` says the ended pane had
 * it, so typing elsewhere, such as in a sidebar editor, keeps its place.
 */
export function usePaneShellExit(
  target: TerminalSplitTarget,
  removesEndedPanes: boolean,
  keyboardOwner: () => string | null,
): (paneKey: string, state: ConnectionState | null) => void {
  const connected = useRef(new Set<string>());
  const latestTarget = useRef(target);
  latestTarget.current = target;
  const latestRemoves = useRef(removesEndedPanes);
  latestRemoves.current = removesEndedPanes;
  const latestOwner = useRef(keyboardOwner);
  latestOwner.current = keyboardOwner;
  return useCallback((paneKey, state) => {
    if (state?.kind === "connected") {
      connected.current.add(paneKey);
      forgetPaneShellEnded(latestTarget.current.conversationId, paneKey);
      return;
    }
    if (!shellEndedOnClose(state, connected.current.has(paneKey))) return;
    if (!latestRemoves.current) return;
    connected.current.delete(paneKey);
    const current = latestTarget.current;
    if (!current.canClosePane(paneKey)) return;
    markPaneShellEnded(current.conversationId, paneKey);
    const split = current.read();
    if (!split || !terminalLayoutKeys(split.layout).includes(paneKey)) return;
    if (latestOwner.current() === paneKey) removePaneFocusingNeighbor(current, paneKey);
    else removeSplitPane(current, paneKey);
  }, []);
}
