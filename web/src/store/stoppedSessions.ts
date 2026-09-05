// Sessions this client explicitly stopped (kebab "Stop session" / bulk stop).
//
// The server deliberately writes no persistent stopped marker — the runner
// tunnel dropping flips `runner_online` honestly and the next message
// relaunches the session — so once a stop lands, the session is
// indistinguishable from an idle-asleep one (`runner_asleep`), for which the
// open view renders nothing. This tiny store remembers the confirmed stop so
// `useSessionLiveness` can surface an explicit `stopped` state the user can
// actually see, bridging the poll gap (the runner-liveness poll can read a
// stale `runner_online: true` for up to one interval after the stop).
//
// In-memory only, by design: it mirrors the server's non-sticky stop. A page
// reload forgets the marker and the session reads `runner_asleep` again.

import { create } from "zustand";

interface StoppedSessionsState {
  /** Session id → epoch ms when the server confirmed its stop landed. */
  stoppedAt: Record<string, number>;
}

export const useStoppedSessions = create<StoppedSessionsState>(() => ({
  stoppedAt: {},
}));

/** Record that `id`'s stop was confirmed by the server (mutation success). */
export function markSessionStopped(id: string): void {
  useStoppedSessions.setState((s) => ({ stoppedAt: { ...s.stoppedAt, [id]: Date.now() } }));
}

/**
 * Forget a stop marker — called once the runner is genuinely observed online
 * again (a relaunch), so a later natural runner drop reads `runner_asleep`
 * rather than replaying a stale "stopped".
 */
export function clearSessionStopped(id: string): void {
  useStoppedSessions.setState((s) => {
    if (!(id in s.stoppedAt)) return s;
    return {
      stoppedAt: Object.fromEntries(Object.entries(s.stoppedAt).filter(([k]) => k !== id)),
    };
  });
}
