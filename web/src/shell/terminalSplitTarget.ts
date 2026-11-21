// A split the pane commands act on: the main Terminal view's, or one sidebar
// group's. The commands, keys and menus are shared; a target says where its
// layout lives, what it shows unsplit, and which panes it may close.

import { isAgentTerminalKey, terminalTabKey, type TerminalInfo } from "@/hooks/useTerminals";
import type { TerminalSplitState } from "@/lib/paneSplitLayout";

import type { TerminalFocusSurface } from "./terminalFocusRequest";
import { readPaneSplit, updatePaneSplit } from "./paneSplitStore";

/** Where a split's layout lives, whatever shows its panes: all the layout commands need. */
export interface SplitTarget {
  /** Names this split, so a pending split or close stays bound to it. */
  id: string;
  read: () => TerminalSplitState | undefined;
  /** Apply ``update`` to the layout; an undefined result means unsplit. */
  update: (
    update: (current: TerminalSplitState | undefined) => TerminalSplitState | undefined,
  ) => void;
  /**
   * ``key``'s pane is about to zoom, or unzoom from ``zoomedKey``: a split that hands each
   * placement to a shell can take the key's own action for it here. ⌘K and the menu pass through
   * too, outside any key, and take none.
   */
  zoomed?: (key: string, zoomedKey: string | null) => void;
}

/** A split of a session's terminals. */
export interface TerminalSplitTarget extends SplitTarget {
  conversationId: string;
  /** The pane shown while unsplit, which a first split starts from. */
  unsplitKey: (agent: TerminalInfo | null) => string | null;
  /** Whether closing may remove ``key``'s pane here. */
  canClosePane: (key: string) => boolean;
  /** Where a focus request for one of its panes goes: the main view or the sidebar. */
  focusSurface: TerminalFocusSurface;
}

const mainTargets = new Map<string, TerminalSplitTarget>();

/** The main Terminal view's split: the agent's terminal unsplit, and never closable. */
export function mainSplitTarget(conversationId: string): TerminalSplitTarget {
  let target = mainTargets.get(conversationId);
  if (!target) {
    target = {
      id: `${conversationId}|main`,
      conversationId,
      read: () => readPaneSplit(conversationId),
      update: (update) => updatePaneSplit(conversationId, update),
      unsplitKey: (agent) => (agent ? terminalTabKey(agent) : null),
      canClosePane: (key) => !isAgentTerminalKey(key),
      focusSurface: "main",
    };
    mainTargets.set(conversationId, target);
  }
  return target;
}
