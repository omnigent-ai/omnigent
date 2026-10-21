// Poll-freshness plumbing for the open session's runner liveness: when the
// latest `/health` sample was requested, and a trigger to re-poll now. Kept
// apart from RunnerHealthProvider so consumers can read it without the
// provider's data hooks.

import { createContext, useContext, useState, useSyncExternalStore } from "react";
import type { SessionLiveness } from "@/hooks/useRunnerHealth";

// When each poll result map's request was issued (epoch ms). A side table so
// the `SessionLiveness` shape stays unchanged.
const sampleStartedAtByMap = new WeakMap<Map<string, SessionLiveness>, number>();

export function recordRunnerHealthSampleStart(
  samples: Map<string, SessionLiveness>,
  startedAt: number,
): void {
  sampleStartedAtByMap.set(samples, startedAt);
}

/** Epoch ms when the request behind `samples` was issued; undefined for an unpolled map. */
export function runnerHealthSampleStartedAt(
  samples: Map<string, SessionLiveness>,
): number | undefined {
  return sampleStartedAtByMap.get(samples);
}

/** Session id -> epoch ms when the `/health` request behind its sample was issued. */
export const RunnerSampleTimeContext = createContext<ReadonlyMap<string, number>>(new Map());

/** Re-polls `/health` immediately (no-op outside the provider). */
export const RunnerHealthRefreshContext = createContext<() => void>(() => {});

export function useSessionRunnerSampledAt(sessionId: string | undefined): number | undefined {
  const map = useContext(RunnerSampleTimeContext);
  return sessionId ? map.get(sessionId) : undefined;
}

export function useRequestRunnerHealthRefresh(): () => void {
  return useContext(RunnerHealthRefreshContext);
}

// Most recent turn end per session (epoch ms). Written silently during render
// by the liveness hook (so same-pass readers already see it) and announced in
// an effect.
const turnEndedAtBySession = new Map<string, number>();
const turnEndListeners = new Set<() => void>();

export function markTurnEnded(sessionId: string, endedAt: number): void {
  turnEndedAtBySession.set(sessionId, endedAt);
}

export function announceTurnEnded(): void {
  for (const listener of turnEndListeners) listener();
}

function subscribeTurnEnd(listener: () => void): () => void {
  turnEndListeners.add(listener);
  return () => turnEndListeners.delete(listener);
}

/**
 * Epoch ms of the most recent `active` true -> false transition for
 * `sessionId` as seen by THIS component, or undefined. Uses React's
 * "adjust state while rendering" pattern on the component's own state, so it
 * can be read in the very render where the turn ends without any write
 * outside the component.
 */
export function useTurnEndedAt(sessionId: string | undefined, active: boolean): number | undefined {
  const [tracked, setTracked] = useState<{
    id: string | undefined;
    active: boolean;
    endedAt: number | undefined;
  }>({ id: sessionId, active, endedAt: undefined });
  if (tracked.id !== sessionId) {
    setTracked({ id: sessionId, active, endedAt: undefined });
  } else if (tracked.active !== active) {
    setTracked({
      id: sessionId,
      active,
      endedAt: tracked.active && !active ? Date.now() : tracked.endedAt,
    });
  }
  return tracked.id === sessionId ? tracked.endedAt : undefined;
}

/**
 * Whether a "runner asleep" reading for `sessionId` can be trusted: false while
 * the latest `/health` sample predates the session's most recent turn end (a
 * short reply can finish before the runner registers), true otherwise. The turn
 * end is the later of the shared store's and the caller's own `turnEndedAt`
 * (see {@link useTurnEndedAt}), so a caller that just saw the turn end needs no
 * store round trip. Gates only the asleep UI (chat notice, open-session sidebar
 * badge); it never changes the liveness kind. `undefined` ids are always
 * confirmed.
 */
export function useRunnerAsleepConfirmed(
  sessionId: string | undefined,
  opts?: { turnEndedAt?: number },
): boolean {
  const storedEndedAt = useSyncExternalStore(subscribeTurnEnd, () =>
    sessionId ? turnEndedAtBySession.get(sessionId) : undefined,
  );
  const sampledAt = useSessionRunnerSampledAt(sessionId);
  if (!sessionId) return true;
  const endedAt = Math.max(storedEndedAt ?? -Infinity, opts?.turnEndedAt ?? -Infinity);
  if (endedAt === -Infinity) return true;
  return sampledAt !== undefined && sampledAt >= endedAt;
}
