// Remember confirmed stops because runner liveness alone cannot distinguish
// an explicit stop from idle sleep. The marker is intentionally not persisted.

import { create } from "zustand";

interface StoppedSessionsState {
  /** Session id → epoch ms when the server confirmed its stop landed. */
  stoppedAt: Record<string, number>;
  /**
   * Session id → count of confirmed relaunches. Bumped by `recordRelaunch` so a
   * stop completion or failed-send restore that captured an older value can see
   * a newer relaunch happened and decline to re-stamp the marker.
   */
  relaunchSeq: Record<string, number>;
}

export const useStoppedSessions = create<StoppedSessionsState>(() => ({
  stoppedAt: {},
  relaunchSeq: {},
}));

/**
 * Mark a session stopped. Pass `sinceSeq` (captured with `relaunchSeqOf` when
 * the stop was initiated or the marker was cleared) to no-op when a relaunch
 * has since bumped the sequence — an older write must not outrank it.
 */
export function markSessionStopped(id: string, sinceSeq?: number): void {
  useStoppedSessions.setState((s) => {
    if (sinceSeq !== undefined && (s.relaunchSeq[id] ?? 0) !== sinceSeq) return s;
    return { stoppedAt: { ...s.stoppedAt, [id]: Date.now() } };
  });
}

export function clearSessionStopped(id: string): void {
  useStoppedSessions.setState((s) => {
    if (!(id in s.stoppedAt)) return s;
    return {
      stoppedAt: Object.fromEntries(Object.entries(s.stoppedAt).filter(([k]) => k !== id)),
    };
  });
}

/** The relaunch sequence for a session; capture before an async stop/relaunch. */
export function relaunchSeqOf(id: string): number {
  return useStoppedSessions.getState().relaunchSeq[id] ?? 0;
}

/**
 * Record a confirmed relaunch: drop any stop marker and bump the sequence so a
 * later stop completion or failed-send restore holding an older sequence backs
 * off instead of resurrecting a stale "stopped" band over a live session.
 */
export function recordRelaunch(id: string): void {
  useStoppedSessions.setState((s) => {
    const stoppedAt =
      id in s.stoppedAt
        ? Object.fromEntries(Object.entries(s.stoppedAt).filter(([k]) => k !== id))
        : s.stoppedAt;
    return { stoppedAt, relaunchSeq: { ...s.relaunchSeq, [id]: (s.relaunchSeq[id] ?? 0) + 1 } };
  });
}
