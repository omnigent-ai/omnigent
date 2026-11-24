// Closing a split pane: it leaves the layout, the pane beside it takes the keyboard, and a shell
// it ran ends in the background. A shell that won't end stays as its own tab, outside the split.

import { closeTerminalPane, terminalLayoutKeys } from "@/lib/paneSplitLayout";

import { clearPaneStartFailure, isFailedPaneStart } from "./paneStartFailures";
import { requestTerminalFocus } from "./terminalFocusRequest";
import { requestTerminalOpen } from "./terminalOpenRequest";
import type { TerminalSplitTarget } from "./terminalSplitTarget";

/**
 * Remove ``key``'s pane from ``target`` and give the pane the layout focuses next the keyboard;
 * ``onRemoved`` runs between the two.
 */
export function removePaneFocusingNeighbor(
  target: TerminalSplitTarget,
  key: string,
  onRemoved?: () => void,
): void {
  const { conversationId } = target;
  const before = target.read();
  const survivors = before ? terminalLayoutKeys(before.layout).filter((pane) => pane !== key) : [];
  // Read before the failures below clear, so a failed start never counts as a shell to open.
  const liveSurvivor = survivors.find((pane) => !isFailedPaneStart(conversationId, pane)) ?? null;
  target.update((current) => current && closeTerminalPane(current, key));
  // A failed start has no tab of its own: a split left with only failed starts ends.
  if (target.read() && liveSplitMember(target) === undefined) target.update(() => undefined);
  const split = target.read();
  const kept = new Set(split ? terminalLayoutKeys(split.layout) : []);
  for (const survivor of survivors) {
    if (!kept.has(survivor)) clearPaneStartFailure(conversationId, survivor);
  }
  onRemoved?.();
  giveNextPaneTheKeyboard(target, split?.focusedKey ?? liveSurvivor);
}

/** After ``target`` lost a pane, give ``nextFocus``'s pane the keyboard, as closing a pane does. */
export function giveNextPaneTheKeyboard(
  target: TerminalSplitTarget,
  nextFocus: string | null,
): void {
  const { conversationId } = target;
  if (nextFocus === null) return;
  // A failed start's slot takes the keyboard itself, on its Try Again.
  const failed = isFailedPaneStart(conversationId, nextFocus);
  // The sidebar shows a split through one of its shells, which may be the one that closed.
  const shown = failed ? liveSplitMember(target) : nextFocus;
  if (target.focusSurface === "rail" && shown !== undefined) {
    requestTerminalOpen(conversationId, { key: shown, focus: !failed });
  }
  if (failed) return;
  requestTerminalFocus({ conversationId, surface: target.focusSurface, tabKey: nextFocus });
}

/** The split's member to show it through, which has a shell: the focused one if it can be. */
export function liveSplitMember(target: TerminalSplitTarget): string | undefined {
  const split = target.read();
  if (!split) return undefined;
  const live = (key: string) => !isFailedPaneStart(target.conversationId, key);
  return live(split.focusedKey) ? split.focusedKey : terminalLayoutKeys(split.layout).find(live);
}

/** Close ``key``'s pane and end its shell, ``terminalId``, with ``endShell``. */
export function closePaneShell(
  target: TerminalSplitTarget,
  key: string,
  terminalId: string,
  endShell: (terminalId: string) => void,
): void {
  removePaneFocusingNeighbor(target, key);
  endShell(terminalId);
}
