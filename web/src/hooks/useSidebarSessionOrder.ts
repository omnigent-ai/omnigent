// The sessions the sidebar lists, top to bottom (visible sections only), and the open one.
// The sidebar publishes them so ⌘K's Next session and Previous session step through the
// same rows as the numbered session shortcuts.

import { useEffect } from "react";
import { create } from "zustand";

interface SidebarSessionOrderState {
  /** Visible rows in render order, without duplicates; empty while no sidebar lists any. */
  orderedSessionIds: readonly string[];
  /** The open session's row, or null off-list (a new session, the inbox). */
  activeSessionId: string | null;
}

const NO_SESSIONS: readonly string[] = [];

export const useSidebarSessionOrderStore = create<SidebarSessionOrderState>(() => ({
  orderedSessionIds: NO_SESSIONS,
  activeSessionId: null,
}));

/**
 * Publishes the sidebar's rows while it's mounted and `enabled`, and clears them once it
 * unmounts or stops publishing. Only ⌘K's QuickPick reads them.
 */
export function usePublishSidebarSessionOrder(
  orderedSessionIds: readonly string[],
  activeSessionId: string | null,
  enabled: boolean,
): void {
  useEffect(() => {
    if (enabled) useSidebarSessionOrderStore.setState({ orderedSessionIds, activeSessionId });
  }, [enabled, orderedSessionIds, activeSessionId]);
  useEffect(() => {
    if (!enabled) return;
    return () =>
      useSidebarSessionOrderStore.setState({
        orderedSessionIds: NO_SESSIONS,
        activeSessionId: null,
      });
  }, [enabled]);
}

/** One step down (1) or up (-1) the sidebar. */
export type SessionStepDirection = 1 | -1;

/**
 * The row one step from the open one, wrapping at the ends. Off-list, stepping down enters at
 * the top and stepping up at the bottom. Undefined when the sidebar lists nothing.
 */
export function steppedSessionId(
  orderedSessionIds: readonly string[],
  activeSessionId: string | null,
  direction: SessionStepDirection,
): string | undefined {
  const count = orderedSessionIds.length;
  if (count === 0) return undefined;
  const current = activeSessionId ? orderedSessionIds.indexOf(activeSessionId) : -1;
  if (current === -1) return orderedSessionIds[direction === 1 ? 0 : count - 1];
  return orderedSessionIds[(current + direction + count) % count];
}
