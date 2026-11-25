// A shell moves between the session's panes and the workspace sidebar as the same terminal:
// only which side shows it changes. Its key leaves or joins the main split's layout, and the
// placement that keeps a terminal attached in one place hands it to the other side; the shell
// itself is never ended, started or restarted. One guard per direction serves every entry
// point (the pane row and its menu, drag and drop, ⌘K) and is checked again as a move runs.

import { isAgentTerminalKey } from "@/hooks/useTerminals";
import { terminalLayoutKeys } from "@/lib/paneSplitLayout";

import {
  type AppPaneDrop,
  type AppPanePlacement,
  appPaneDropAt,
  dropPaneInSplit,
  isInMainSplit,
  openPaneInSplit,
  paneRoomRefusal,
} from "./appPanes";
import { requestTerminalFocus } from "./terminalFocusRequest";
import { requestTerminalOpen } from "./terminalOpenRequest";
import { focusSplitPane, removeSplitPane } from "./splitTargetActions";
import { mainSplitTarget } from "./terminalSplitTarget";

/** What decides whether a shell may move, read when asked so it's current. */
export interface TerminalMoveScope {
  /** The session's own terminal, which the split starts from; null while it isn't open. */
  agentKey: string | null;
  /** Whether panes may split here: a desktop owner with splittable terminals on. */
  splitsAllowed: boolean;
  /** Whether the session lists the shell keyed `key` as running now. */
  isLive: (key: string) => boolean;
}

export const TERMINAL_MOVE_REFUSALS = Object.freeze({
  splitsOff: "Panes aren't available in this session.",
  noAgent: "Open the session's terminal first, then try again.",
  agent: "The session's own terminal stays in its pane.",
  notShell: "Only a shell can move between the panes and the sidebar.",
  notInPane: "This shell isn't shown as a pane.",
  lastPane: "This shell is the only pane. Show the chat first.",
  ended: "This shell has ended.",
});

const TERMINAL_KEY_PREFIX = "terminal:";

/** Whether `key` names a session terminal, as panes and shell tabs key them. */
export const isTerminalPaneKey = (key: string): boolean => key.startsWith(TERMINAL_KEY_PREFIX);

function shellRefusal(key: string, scope: TerminalMoveScope): string | undefined {
  if (!scope.splitsAllowed) return TERMINAL_MOVE_REFUSALS.splitsOff;
  if (!isTerminalPaneKey(key)) return TERMINAL_MOVE_REFUSALS.notShell;
  if (key === scope.agentKey || isAgentTerminalKey(key)) return TERMINAL_MOVE_REFUSALS.agent;
  if (!scope.isLive(key)) return TERMINAL_MOVE_REFUSALS.ended;
  return undefined;
}

/** Why the shell pane keyed `key` can't move to the workspace sidebar now; undefined when it can. */
export function terminalToSidebarRefusal(
  conversationId: string,
  key: string,
  scope: TerminalMoveScope,
): string | undefined {
  const refusal = shellRefusal(key, scope);
  if (refusal) return refusal;
  const layout = mainSplitTarget(conversationId).read()?.layout;
  const keys = layout === undefined ? [] : terminalLayoutKeys(layout);
  if (!keys.includes(key)) return TERMINAL_MOVE_REFUSALS.notInPane;
  // The chat, or another pane, has to stay for the split to show.
  if (keys.length < 2) return TERMINAL_MOVE_REFUSALS.lastPane;
  return undefined;
}

/**
 * Move the shell pane keyed `key` to the workspace sidebar: its pane closes without ending the
 * shell, and the sidebar selects its tab and gives it the keyboard. The refusal when it can't.
 */
export function moveTerminalToSidebar(
  conversationId: string,
  key: string,
  scope: TerminalMoveScope,
): string | undefined {
  const refusal = terminalToSidebarRefusal(conversationId, key, scope);
  if (refusal) return refusal;
  removeSplitPane(mainSplitTarget(conversationId), key);
  requestTerminalOpen(conversationId, { key, focus: true });
  return undefined;
}

/**
 * Why the sidebar shell keyed `key` can't open as a pane now; undefined when it can, including
 * when the split already shows it, which then only takes focus.
 */
export function terminalToPaneRefusal(
  conversationId: string,
  key: string,
  scope: TerminalMoveScope,
  placement: Omit<AppPanePlacement, "agentKey"> = {},
): string | undefined {
  const refusal = shellRefusal(key, scope);
  if (refusal) return refusal;
  if (scope.agentKey === null) return TERMINAL_MOVE_REFUSALS.noAgent;
  return paneRoomRefusal(conversationId, key, { agentKey: scope.agentKey, ...placement });
}

/** Give the pane keyed `key` the split's focus and the keyboard. */
function focusTerminalPane(conversationId: string, key: string): void {
  focusSplitPane(mainSplitTarget(conversationId), key);
  requestTerminalFocus({ conversationId, surface: "main", tabKey: key });
}

/**
 * Open the sidebar shell keyed `key` as a pane beside the split's focused pane and give it the
 * keyboard; a shell the split already shows is focused, never added twice. The refusal when
 * it can't.
 */
export function openTerminalAsPane(
  conversationId: string,
  key: string,
  scope: TerminalMoveScope,
  placement: Omit<AppPanePlacement, "agentKey"> = {},
): string | undefined {
  const refusal = terminalToPaneRefusal(conversationId, key, scope, placement);
  if (refusal) return refusal;
  if (
    !isInMainSplit(conversationId, key) &&
    !openPaneInSplit(conversationId, key, { agentKey: scope.agentKey, ...placement })
  ) {
    return TERMINAL_MOVE_REFUSALS.noAgent;
  }
  focusTerminalPane(conversationId, key);
  return undefined;
}

/** Where the sidebar shell keyed `key`, dragged to `point`, would land among the panes; null if nowhere. */
export function terminalPaneDropAt(
  conversationId: string,
  key: string,
  point: { x: number; y: number },
  scope: TerminalMoveScope,
): AppPaneDrop | null {
  if (shellRefusal(key, scope) || scope.agentKey === null) return null;
  // Over the panes, a shell they already show has nowhere new to go.
  if (isInMainSplit(conversationId, key)) return null;
  return appPaneDropAt(conversationId, point, scope.agentKey);
}

/**
 * Open the sidebar shell keyed `key` on `drop`'s side of its target and give it the keyboard,
 * rechecking what may have changed since the preview. False when it can't land there.
 */
export function dropTerminalAsPane(
  conversationId: string,
  key: string,
  drop: Pick<AppPaneDrop, "targetKey" | "side">,
  scope: TerminalMoveScope,
): boolean {
  if (shellRefusal(key, scope) || scope.agentKey === null) return false;
  if (isInMainSplit(conversationId, key)) {
    focusTerminalPane(conversationId, key);
    return true;
  }
  if (!dropPaneInSplit(conversationId, key, drop, scope.agentKey)) return false;
  focusTerminalPane(conversationId, key);
  return true;
}
