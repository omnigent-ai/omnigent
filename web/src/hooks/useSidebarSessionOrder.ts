// The sessions the sidebar lists, top to bottom (visible sections only), and the open one.
// The sidebar publishes them so ⌘K's Next session and Previous session step through the
// same rows as the numbered session shortcuts.

import { useEffect } from "react";
import { create } from "zustand";

import { useLocation } from "@/lib/routing";

interface SidebarSessionOrderState {
  /** A sidebar is mounted and publishing, even while it lists no rows or shows no session. */
  published: boolean;
  /** Visible rows in render order, without duplicates; empty while no sidebar lists any. */
  orderedSessionIds: readonly string[];
  /**
   * The session the route shows (a sub-agent's root), whether or not the list still holds its
   * row; null on a page that shows none, such as a new session or the inbox.
   */
  activeSessionId: string | null;
  /**
   * A session a step opened that the route hasn't rendered yet. Navigations render in a
   * transition that waits for the session to load, so a quick next step starts from here.
   */
  pendingSessionId: string | null;
  /** When `pendingSessionId` was set, so a navigation that never commits stops counting. */
  pendingSince: number;
}

/** How long a step's session counts as current before the route has rendered it. */
export const PENDING_SESSION_MS = 5_000;

const NO_SESSIONS: readonly string[] = [];

export const useSidebarSessionOrderStore = create<SidebarSessionOrderState>(() => ({
  published: false,
  orderedSessionIds: NO_SESSIONS,
  activeSessionId: null,
  pendingSessionId: null,
  pendingSince: 0,
}));

/** Publishes the sidebar's rows while it's mounted, and clears them once it unmounts. */
export function usePublishSidebarSessionOrder(
  orderedSessionIds: readonly string[],
  activeSessionId: string | null,
): void {
  // Any committed navigation, including one that replaced a step's, makes the route current.
  const { key: locationKey } = useLocation();
  useEffect(() => {
    useSidebarSessionOrderStore.setState({ pendingSessionId: null });
  }, [locationKey]);
  useEffect(() => {
    useSidebarSessionOrderStore.setState({ published: true, orderedSessionIds, activeSessionId });
  }, [orderedSessionIds, activeSessionId]);
  useEffect(
    () => () =>
      useSidebarSessionOrderStore.setState({
        published: false,
        orderedSessionIds: NO_SESSIONS,
        activeSessionId: null,
        pendingSessionId: null,
      }),
    [],
  );
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

/**
 * The session a step starts from: the one a step last opened, else the rendered one. A step's
 * session stops counting once its navigation has gone uncommitted for `PENDING_SESSION_MS`,
 * as when a guard cancels it.
 */
export function currentSidebarSessionId(now = Date.now()): string | null {
  const { pendingSessionId, pendingSince, activeSessionId } =
    useSidebarSessionOrderStore.getState();
  const pendingCurrent = pendingSessionId !== null && now - pendingSince < PENDING_SESSION_MS;
  return pendingCurrent ? pendingSessionId : activeSessionId;
}

/** Opens `sessionId` at `path`, counting it as current until the route renders it. */
export function navigateToPendingSession(
  sessionId: string,
  path: string,
  navigate: (path: string) => void,
): void {
  // Recorded first, so the commit that renders it clears it rather than running before it.
  useSidebarSessionOrderStore.setState({ pendingSessionId: sessionId, pendingSince: Date.now() });
  navigate(path);
}

/**
 * Opens the listed row one step from the current session, reading the list when it runs.
 * Returns false when there is nowhere to go.
 */
export function stepSidebarSession(
  direction: SessionStepDirection,
  navigate: (path: string) => void,
): boolean {
  const { orderedSessionIds } = useSidebarSessionOrderStore.getState();
  const target = steppedSessionId(orderedSessionIds, currentSidebarSessionId(), direction);
  if (!target) return false;
  openSidebarSession(target, navigate);
  return true;
}

/** Opens `sessionId` from a shortcut, unless it's already the current one; see `pendingSessionId`. */
export function openSidebarSession(sessionId: string, navigate: (path: string) => void): void {
  if (sessionId === currentSidebarSessionId()) return;
  navigateToPendingSession(sessionId, `/c/${sessionId}`, navigate);
}
