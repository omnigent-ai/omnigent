import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";

import { retrySession } from "@/lib/sessionsApi";

/**
 * How long to keep showing "Starting sandbox…" after the server reports the
 * wake, while liveness catches up with the reconnected runner.
 */
export const SANDBOX_WAKE_SETTLE_MS = 30_000;

/** How long the server may take to answer a wake before it counts as failed. */
export const SANDBOX_WAKE_REQUEST_MS = 5 * 60_000;

export type SandboxWakePhase = "idle" | "waking" | "failed";

export interface SandboxAutoWake {
  phase: SandboxWakePhase;
  /** Why the last wake failed; null unless `phase` is "failed". */
  error: string | null;
  /** Wake the sandbox now, e.g. from a "Try again" button. */
  wake: () => Promise<void>;
}

interface WakeAttempt {
  /** `requesting`: the server is resuming it; `settling`: waiting for liveness to agree. */
  step: "requesting" | "settling" | "failed";
  error: string | null;
  /** Fails an attempt that never answers or that liveness never confirms. */
  timer?: number;
}

const PHASE_BY_STEP: Record<WakeAttempt["step"], SandboxWakePhase> = {
  requesting: "waking",
  settling: "waking",
  failed: "failed",
};

// Per session, so an attempt outlives the view that started it: a remounted
// view or a switch back to the session joins it instead of starting another.
const attempts = new Map<string, WakeAttempt>();
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function setAttempt(conversationId: string, attempt: WakeAttempt | undefined): void {
  const previous = attempts.get(conversationId);
  if (previous?.timer !== undefined) window.clearTimeout(previous.timer);
  if (attempt) attempts.set(conversationId, attempt);
  else attempts.delete(conversationId);
  for (const listener of listeners) listener();
}

/** Store *attempt* for the session, failing it if it is still current after *ms*. */
function setAttemptWithDeadline(conversationId: string, attempt: WakeAttempt, ms: number): void {
  attempt.timer = window.setTimeout(() => {
    if (attempts.get(conversationId) !== attempt) return;
    setAttempt(conversationId, { step: "failed", error: "The sandbox didn't come back online." });
  }, ms);
  setAttempt(conversationId, attempt);
}

/** Wake a session's sandbox, joining its attempt that is still in flight. */
async function startSandboxWake(conversationId: string): Promise<void> {
  const existing = attempts.get(conversationId);
  if (existing && existing.step !== "failed") return;
  const attempt: WakeAttempt = { step: "requesting", error: null };
  setAttemptWithDeadline(conversationId, attempt, SANDBOX_WAKE_REQUEST_MS);
  try {
    // "already_connected" also settles: liveness can lag a runner that is up.
    await retrySession(conversationId, { wakeOnly: true });
    if (attempts.get(conversationId) !== attempt) return;
    setAttemptWithDeadline(
      conversationId,
      { step: "settling", error: null },
      SANDBOX_WAKE_SETTLE_MS,
    );
  } catch (error) {
    if (attempts.get(conversationId) !== attempt) return;
    setAttempt(conversationId, {
      step: "failed",
      error:
        error instanceof Error && error.message
          ? `Couldn't start the sandbox: ${error.message}`
          : "Couldn't start the sandbox.",
    });
  }
}

/** Forget every session's wake attempt and its pending timeout. */
export function resetSandboxWakesForTesting(): void {
  for (const attempt of attempts.values()) {
    if (attempt.timer !== undefined) window.clearTimeout(attempt.timer);
  }
  attempts.clear();
  for (const listener of listeners) listener();
}

/**
 * Wake a session's asleep (`host_asleep`) sandbox once per visible opening, sharing
 * attempts across remounts. A view with cached content needs no wake; the wake-only
 * `retry_session` it sends never resumes agent work.
 */
export function useSandboxAutoWake({
  conversationId,
  asleep,
  needsSandbox,
  awake,
  active,
  canWake,
}: {
  conversationId: string | undefined;
  asleep: boolean;
  needsSandbox: boolean;
  awake: boolean;
  active: boolean;
  canWake: boolean;
}): SandboxAutoWake {
  const getAttempt = useCallback(
    () => (conversationId ? attempts.get(conversationId) : undefined),
    [conversationId],
  );
  const attempt = useSyncExternalStore(subscribe, getAttempt);
  // Whether the current opening has already woken, or found the sandbox awake.
  const openingRef = useRef<{ conversationId: string | undefined; consumed: boolean }>({
    conversationId,
    consumed: false,
  });

  const wake = useCallback(async () => {
    if (conversationId) await startSandboxWake(conversationId);
  }, [conversationId]);

  useEffect(() => {
    if (!active || openingRef.current.conversationId !== conversationId) {
      openingRef.current = { conversationId, consumed: false };
    }
    const opening = openingRef.current;
    if (!active || opening.consumed || !conversationId) return;
    if (awake) {
      opening.consumed = true;
      return;
    }
    if (!asleep || !needsSandbox || !canWake) return;
    opening.consumed = true;
    void startSandboxWake(conversationId);
  }, [active, asleep, awake, canWake, conversationId, needsSandbox]);

  const step = attempt?.step;
  useEffect(() => {
    // Runner online, by this wake or any other: forget the attempt, even one still in flight.
    if (conversationId && awake && step !== undefined) {
      setAttempt(conversationId, undefined);
    }
  }, [awake, conversationId, step]);

  return {
    phase: attempt ? PHASE_BY_STEP[attempt.step] : "idle",
    error: attempt?.error ?? null,
    wake,
  };
}
