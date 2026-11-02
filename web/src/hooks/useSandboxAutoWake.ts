import { useCallback, useEffect, useRef, useState } from "react";

import { retrySession } from "@/lib/sessionsApi";

/**
 * How long to keep showing "Starting sandbox…" after the server reports the
 * wake, while liveness catches up with the reconnected runner.
 */
export const SANDBOX_WAKE_SETTLE_MS = 30_000;

export type SandboxWakePhase = "idle" | "waking" | "failed";

export interface SandboxAutoWake {
  phase: SandboxWakePhase;
  /** Why the last wake failed; null unless `phase` is "failed". */
  error: string | null;
  /** Wake the sandbox now, e.g. from a "Try again" button. */
  wake: () => Promise<void>;
}

interface WakeState {
  conversationId: string | undefined;
  /** `requesting`: the server is resuming it; `settling`: waiting for liveness to agree. */
  step: "idle" | "requesting" | "settling" | "failed";
  error: string | null;
}

interface Opening {
  conversationId: string | undefined;
  /** This opening already woke, or joined an in-flight wake. */
  attempted: boolean;
  pending: boolean;
}

const PHASE_BY_STEP: Record<WakeState["step"], SandboxWakePhase> = {
  idle: "idle",
  requesting: "waking",
  settling: "waking",
  failed: "failed",
};

/**
 * Wake a session's asleep managed sandbox when a view that needs it opens.
 *
 * Uses the same server recovery as **Reconnect host** (`retry_session`) in
 * wake-only mode, so the sandbox resumes and its runner starts without a chat
 * message, a resumed turn, or a retried startup input. Each
 * opening (`active` turning true) wakes at most once: a sandbox that idles
 * back to sleep while the view stays open waits for `wake()` or a reopen, so
 * an open panel never keeps a sandbox awake.
 *
 * @param conversationId Session whose sandbox to wake.
 * @param asleep Whether the session's sandbox is asleep (`host_asleep`).
 * @param active Whether the view is open and visible to the user.
 * @param canWake Whether the user may restart the session (edit access).
 */
export function useSandboxAutoWake({
  conversationId,
  asleep,
  active,
  canWake,
}: {
  conversationId: string | undefined;
  asleep: boolean;
  active: boolean;
  canWake: boolean;
}): SandboxAutoWake {
  const [state, setState] = useState<WakeState>({ conversationId, step: "idle", error: null });
  const current: WakeState =
    state.conversationId === conversationId ? state : { conversationId, step: "idle", error: null };
  const openingRef = useRef<Opening>({ conversationId, attempted: false, pending: false });
  // Read outside render: another session's view starts a fresh opening.
  const openingFor = useCallback((id: string | undefined): Opening => {
    if (openingRef.current.conversationId !== id) {
      openingRef.current = { conversationId: id, attempted: false, pending: false };
    }
    return openingRef.current;
  }, []);
  const activeRef = useRef(active);
  useEffect(() => {
    activeRef.current = active;
  }, [active]);

  const wake = useCallback(async () => {
    const opening = openingFor(conversationId);
    if (!conversationId || opening.pending) return;
    opening.attempted = true;
    opening.pending = true;
    setState({ conversationId, step: "requesting", error: null });
    try {
      // "already_connected" also settles: liveness can lag a runner that is up.
      await retrySession(conversationId, { wakeOnly: true });
      if (openingRef.current === opening) {
        setState({ conversationId, step: "settling", error: null });
      }
    } catch (error) {
      if (openingRef.current === opening) {
        setState({
          conversationId,
          step: "failed",
          error:
            error instanceof Error && error.message
              ? `Couldn't start the sandbox: ${error.message}`
              : "Couldn't start the sandbox.",
        });
      }
    } finally {
      opening.pending = false;
      // The view closed mid-wake; let its next opening try again.
      if (!activeRef.current) opening.attempted = false;
    }
  }, [conversationId, openingFor]);

  // Closing the view ends this opening; the next one may wake again.
  useEffect(() => {
    const opening = openingFor(conversationId);
    if (!active && !opening.pending) opening.attempted = false;
  }, [active, conversationId, openingFor]);

  const step = current.step;
  useEffect(() => {
    const opening = openingFor(conversationId);
    if (!active || !asleep || !canWake || opening.attempted) return;
    // Reopened while an earlier wake is still in flight: that wake counts.
    if (step === "requesting" || step === "settling") {
      opening.attempted = true;
      return;
    }
    void wake();
  }, [active, asleep, canWake, conversationId, openingFor, step, wake]);

  useEffect(() => {
    // Back online, by this wake or any other: forget the attempt's outcome.
    if ((step === "settling" || step === "failed") && !asleep) {
      setState({ conversationId, step: "idle", error: null });
      return;
    }
    if (step !== "settling") return;
    const timeout = window.setTimeout(() => {
      setState({ conversationId, step: "failed", error: "The sandbox didn't come back online." });
    }, SANDBOX_WAKE_SETTLE_MS);
    return () => window.clearTimeout(timeout);
  }, [asleep, conversationId, step]);

  return { phase: PHASE_BY_STEP[step], error: current.error, wake };
}
