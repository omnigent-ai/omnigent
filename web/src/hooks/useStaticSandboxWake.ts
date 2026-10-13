import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { PausedSandbox } from "@/components/StaticSandboxWakeBanner";
import { showToast } from "@/components/ui/toast";
import { removeStaticSandbox, wakeSessionHost } from "@/lib/staticSandboxesApi";
import type { Session } from "@/lib/types";
import type { SessionLiveness } from "./useSessionLiveness";
import { useWakeOnFirstInput } from "./useWakeOnFirstInput";

/** A chat session's paused static sandbox: its banner and first-keystroke wake. */
export interface StaticSandboxWake {
  /** The banner's sandbox, or null when there is nothing to show. */
  pausedSandbox: PausedSandbox | null;
  /** Names the host's paused period; null when there is nothing to wake. */
  pausedWakeKey: string | null;
  wake: () => void;
}

/** Provided by ChatPage around the session surface; null elsewhere. */
export const StaticSandboxWakeContext = createContext<StaticSandboxWake | null>(null);

/**
 * The provider can pause a static sandbox's VM at any time. While it is paused,
 * the first keystroke (or the banner's Resume) wakes it in place; viewing alone
 * never does.
 */
export function useStaticSandboxWake(
  sessionId: string | null,
  liveness: SessionLiveness,
  session:
    | Pick<Session, "hostResumable" | "sandboxState" | "sandboxName" | "sandboxNameProvider">
    | null
    | undefined,
): StaticSandboxWake {
  const [wakingSessionId, setWakingSessionId] = useState<string | null>(null);
  const pausedWakeKey =
    sessionId !== null &&
    liveness.kind === "host_asleep" &&
    session?.hostResumable === true &&
    session.sandboxState !== "gone"
      ? sessionId
      : null;
  const wake = useCallback(() => {
    if (pausedWakeKey === null) return;
    const id = pausedWakeKey;
    setWakingSessionId(id);
    wakeSessionHost(id)
      .catch((err: unknown) =>
        showToast(err instanceof Error ? err.message : "Couldn't wake the sandbox"),
      )
      .finally(() => setWakingSessionId((current) => (current === id ? null : current)));
  }, [pausedWakeKey]);
  const sandboxName = session?.sandboxName ?? null;
  const sandboxProvider = session?.sandboxNameProvider ?? null;
  const sandboxState = session?.sandboxState ?? null;
  const hostAsleep = liveness.kind === "host_asleep";
  const waking = sessionId !== null && wakingSessionId === sessionId;
  const pausedSandbox = useMemo<PausedSandbox | null>(() => {
    if (
      sessionId === null ||
      sandboxName === null ||
      sandboxProvider === null ||
      sandboxState === null
    ) {
      return null;
    }
    if (sandboxState !== "gone" && !hostAsleep) return null;
    return {
      name: sandboxName,
      state: sandboxState,
      waking,
      onResume: wake,
      onRemove: () => {
        removeStaticSandbox(sandboxProvider, sandboxName).catch((err: unknown) =>
          showToast(err instanceof Error ? err.message : "Couldn't remove the entry"),
        );
      },
    };
  }, [sessionId, sandboxName, sandboxProvider, sandboxState, hostAsleep, waking, wake]);
  return useMemo(
    () => ({ pausedSandbox, pausedWakeKey, wake }),
    [pausedSandbox, pausedWakeKey, wake],
  );
}

/** The paused static sandbox the surrounding chat session shows, if any. */
export function usePausedStaticSandbox(): PausedSandbox | null {
  return useContext(StaticSandboxWakeContext)?.pausedSandbox ?? null;
}

/** A paused resume-in-place host wakes on the composer's first keystroke, not on Send. */
export function useStaticSandboxWakeOnInput(text: string): void {
  const ctx = useContext(StaticSandboxWakeContext);
  useWakeOnFirstInput(ctx?.pausedWakeKey ?? null, text, () => ctx?.wake());
}
