// Remember confirmed stops because runner liveness alone cannot distinguish
// an explicit stop from idle sleep. The marker is intentionally not persisted.

import { create } from "zustand";

interface StoppedSessionsState {
  /** Session id → epoch ms when the server confirmed its stop landed. */
  stoppedAt: Record<string, number>;
}

export const useStoppedSessions = create<StoppedSessionsState>(() => ({
  stoppedAt: {},
}));

export function markSessionStopped(id: string): void {
  useStoppedSessions.setState((s) => ({ stoppedAt: { ...s.stoppedAt, [id]: Date.now() } }));
}

export function clearSessionStopped(id: string): void {
  useStoppedSessions.setState((s) => {
    if (!(id in s.stoppedAt)) return s;
    return {
      stoppedAt: Object.fromEntries(Object.entries(s.stoppedAt).filter(([k]) => k !== id)),
    };
  });
}
