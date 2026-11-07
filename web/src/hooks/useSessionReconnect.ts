import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { toast } from "sonner";

import { fetchHosts } from "@/hooks/useHosts";
import { onRunnerHealthPolled, refreshRunnerHealth } from "@/hooks/useRunnerHealth";
import { isArcaHost, readArcaHostId, writeArcaHostId } from "@/lib/arcaHost";
import {
  connectArcaHost,
  controlHost,
  getHostIdentity,
  isElectronShell,
  supportsArcaConnect,
} from "@/lib/nativeBridge";
import { retrySession } from "@/lib/sessionsApi";
import { isSandboxLaunchStopped } from "@/lib/sandboxStatus";
import type { SandboxStatus } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";

/** How many scheduled health polls may report a woken sandbox's host offline before it gives up. */
export const SANDBOX_WAKE_ONLINE_GRACE_POLLS = 3;

/** How long a wake may follow a launch stage that never finishes before Reconnect host returns. */
export const SANDBOX_WAKE_STAGE_LIMIT_MS = 5 * 60_000;

/** How long a wake can count as running at all, for a session nobody has watched since. */
export const SANDBOX_WAKE_MAX_MS = 30 * 60_000;

/** Identifies a reported launch stage by value; snapshots rebuild the object. */
function launchKey(launch: SandboxStatus | null): string {
  return launch ? JSON.stringify([launch.stage, launch.error, launch.details]) : "";
}

interface SandboxWake {
  phase: "waking" | "awaiting_online";
  /** The launch stage when the wake started, and whether a newer one was reported since. */
  launch: string;
  stageChanged: boolean;
  timer: ReturnType<typeof setTimeout>;
  /** The server's answer, which callers joining while it is `waking` wait for. */
  request: Promise<boolean>;
  /** When a launch stage that never finishes lets Reconnect host return, once one is seen. */
  stageDeadline?: number;
  /** Ends the wake once a health poll sees its host online, whichever session is shown. */
  stopWatchingHost?: () => void;
}

// Per session and outside any view, so every way of waking a sandbox (Reconnect
// host, opening the session, the Files tab, the terminal's Resume) joins one wake,
// including after switching to another session and back.
const sandboxWakes = new Map<string, SandboxWake>();
const sandboxWakeListeners = new Set<() => void>();
// Sessions a Reconnect host view is showing now, so a failure elsewhere stays quiet.
const shownSessions = new Map<string, number>();

function subscribeSandboxWakes(listener: () => void): () => void {
  sandboxWakeListeners.add(listener);
  return () => sandboxWakeListeners.delete(listener);
}

function notifySandboxWakes(): void {
  for (const listener of sandboxWakeListeners) listener();
}

function endSandboxWake(sessionId: string, wake = sandboxWakes.get(sessionId)): void {
  if (!wake || sandboxWakes.get(sessionId) !== wake) return;
  clearTimeout(wake.timer);
  wake.stopWatchingHost?.();
  sandboxWakes.delete(sessionId);
  notifySandboxWakes();
}

/** Forget every session's sandbox wake. */
export function resetSandboxWakesForTesting(): void {
  for (const wake of sandboxWakes.values()) {
    clearTimeout(wake.timer);
    wake.stopWatchingHost?.();
  }
  sandboxWakes.clear();
  notifySandboxWakes();
}

/**
 * Wake a session's resumable sandbox as Reconnect host does, unless a wake of it
 * is already running. The server resumes the sandbox and starts the agent, as
 * it does for a message, so this works for anyone who can send one.
 *
 * @returns False when the server refused the wake (already reported in a toast).
 */
export function startSandboxWake(sessionId: string): Promise<boolean> {
  const running = sandboxWakes.get(sessionId);
  if (running) return running.phase === "waking" ? running.request : Promise.resolve(true);
  const store = useChatStore.getState();
  const startLaunch = store.conversationId === sessionId ? store.sandboxStatus : null;
  const wake = {
    phase: "waking",
    launch: launchKey(startLaunch),
    stageChanged: false,
  } as SandboxWake;
  wake.timer = setTimeout(() => endSandboxWake(sessionId, wake), SANDBOX_WAKE_MAX_MS);
  sandboxWakes.set(sessionId, wake);
  wake.request = requestSandboxWake(sessionId, wake);
  notifySandboxWakes();
  return wake.request;
}

async function requestSandboxWake(sessionId: string, wake: SandboxWake): Promise<boolean> {
  try {
    await retrySession(sessionId);
    if (sandboxWakes.get(sessionId) !== wake) return true;
    wake.phase = "awaiting_online";
    // A wake left running behind another session still ends when its host is up,
    // so a later sleep isn't mistaken for this wake.
    wake.stopWatchingHost = onRunnerHealthPolled((liveness) => {
      if (liveness.get(sessionId)?.host_online === true) endSandboxWake(sessionId, wake);
    });
    notifySandboxWakes();
    refreshRunnerHealth();
    return true;
  } catch (err) {
    if (sandboxWakes.get(sessionId) !== wake) return false;
    endSandboxWake(sessionId, wake);
    // Another session is on screen now; this one shows Reconnect host again on return.
    if (!shownSessions.has(sessionId)) return false;
    toast.error(
      err instanceof Error && err.message
        ? `Couldn't reconnect the sandbox: ${err.message}`
        : "Couldn't reconnect the sandbox. Try again.",
    );
    return false;
  }
}

/** The running wake of *sessionId*'s sandbox, if any. */
export function useSandboxWakePhase(
  sessionId: string | null | undefined,
): SandboxWake["phase"] | null {
  const read = useCallback(
    () => (sessionId ? (sandboxWakes.get(sessionId)?.phase ?? null) : null),
    [sessionId],
  );
  return useSyncExternalStore(subscribeSandboxWakes, read);
}

/**
 * Reconnect this desktop's host directly; use the dialog for fallback and retry.
 * A resumable sandbox host is woken by the server instead, with its progress
 * shown by the composer's host status dot rather than a toast.
 */
export function useSessionReconnect({
  sessionId,
  hostId,
  hostName,
  hostResumable = false,
  hostOnline,
  isOwner,
}: {
  sessionId: string | null;
  hostId: string | null;
  hostName?: string | null;
  hostResumable?: boolean;
  /** The session's host liveness; a woken sandbox counts as starting until it is `true`. */
  hostOnline?: boolean | null;
  isOwner: boolean;
}) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [canReconnectThisMachine, setCanReconnectThisMachine] = useState(false);
  const [canReconnectArca, setCanReconnectArca] = useState(false);
  const sandboxWake = useSandboxWakePhase(sessionId);
  const inFlight = useRef<{ progressToast?: string | number } | null>(null);
  // The open session's launch stage, compared against its value at the wake's click.
  // `undefined` while the store still holds another session (just after a
  // switch): this session's stage isn't known then, which isn't "no stage".
  const launch = useChatStore((s) =>
    s.conversationId === sessionId ? s.sandboxStatus : undefined,
  );
  const launchInProgress = launch != null && !isSandboxLaunchStopped(launch);
  useEffect(() => {
    if (!sessionId) return;
    shownSessions.set(sessionId, (shownSessions.get(sessionId) ?? 0) + 1);
    return () => {
      const remaining = (shownSessions.get(sessionId) ?? 1) - 1;
      if (remaining > 0) shownSessions.set(sessionId, remaining);
      else shownSessions.delete(sessionId);
    };
  }, [sessionId]);
  const isSessionArcaHost = useCallback(
    () =>
      hostId !== null && isArcaHost({ host_id: hostId, name: hostName ?? "" }, readArcaHostId()),
    [hostId, hostName],
  );
  const runsArcaConnect = useCallback(
    () => !!hostId && isOwner && supportsArcaConnect() && isSessionArcaHost(),
    [hostId, isOwner, isSessionArcaHost],
  );

  useEffect(() => {
    setDialogOpen(false);
    setError(null);
    setCanReconnectThisMachine(false);
    setCanReconnectArca(false);
    setReconnecting(false);
    return () => {
      if (inFlight.current?.progressToast !== undefined) {
        toast.dismiss(inFlight.current.progressToast);
      }
      inFlight.current = null;
    };
  }, [sessionId, hostId, isOwner]);

  const reconnectArca = useCallback(async () => {
    if (inFlight.current) return;
    if (!runsArcaConnect()) {
      setCanReconnectArca(false);
      setDialogOpen(true);
      return;
    }

    const operation: { progressToast?: string | number } = {};
    inFlight.current = operation;
    // A failed attempt opens the dialog, which needs the Arca action for its retry.
    setCanReconnectArca(true);
    setReconnecting(true);
    setError(null);
    try {
      operation.progressToast = toast.loading("Reconnecting Arca…");
      const result = await connectArcaHost();
      if (inFlight.current !== operation) return;
      if (!result.ok) {
        if (!result.canceled && !result.shownInConsole) {
          setError(result.error ?? "Couldn't reconnect Arca. Try again.");
        }
        setDialogOpen(true);
        return;
      }
      if (result.identity) writeArcaHostId(result.identity.hostId);
      // The connect starts whichever Arca the user has now, which may not be this
      // session's host, so only the server's view of this host decides success.
      const hosts = await fetchHosts(false).catch(() => null);
      if (inFlight.current !== operation) return;
      if (hosts === null) {
        setError("Couldn't check whether this session's host is back online. Try again.");
        setDialogOpen(true);
        return;
      }
      if (!hosts.some((host) => host.host_id === hostId && host.status === "online")) {
        setError(
          "This session's host is still offline. If Arca came back as a different host, use Switch host to move the session.",
        );
        setDialogOpen(true);
        return;
      }
      setDialogOpen(false);
      toast.success(result.alreadyRunning ? "Arca is already online." : "Arca is back online.");
    } finally {
      if (inFlight.current === operation) {
        if (operation.progressToast !== undefined) toast.dismiss(operation.progressToast);
        inFlight.current = null;
        setReconnecting(false);
      }
    }
  }, [hostId, runsArcaConnect]);

  // Liveness polls lag the wake, so stay "starting" until the host reads online.
  useEffect(() => {
    if (sessionId && sandboxWake === "awaiting_online" && hostOnline === true) {
      endSandboxWake(sessionId);
    }
  }, [sandboxWake, hostOnline, sessionId]);

  // Count the interval's polls, not time, since `/health` backs off on failures; a
  // refresh or dialog's extra polls could use up the count in seconds. The count
  // waits for the launch to stop reporting progress, and starts from zero then.
  useEffect(() => {
    if (sandboxWake !== "awaiting_online" || launchInProgress || !sessionId) return;
    let offlinePolls = 0;
    return onRunnerHealthPolled((liveness, scheduled) => {
      const session = liveness.get(sessionId);
      if (!scheduled || !session || session.host_online === true) return;
      offlinePolls += 1;
      if (offlinePolls !== SANDBOX_WAKE_ONLINE_GRACE_POLLS) return;
      endSandboxWake(sessionId);
      toast.error("The sandbox didn't come online. Try Reconnect host again.");
    });
  }, [sandboxWake, launchInProgress, sessionId]);

  // The count above waits on the launch, so a stage that never finishes would spin
  // with Reconnect host hidden. Let the wake go quietly: the server's stage keeps the
  // dot spinning, and Reconnect host returns as it does after a reload.
  // The deadline belongs to the wake, so the server answering mid-stage or the
  // user switching sessions and back keeps it.
  const wakeRunning = sandboxWake !== null;
  useEffect(() => {
    const wake = sessionId ? sandboxWakes.get(sessionId) : undefined;
    if (!wakeRunning || !launchInProgress || !sessionId || !wake) return;
    wake.stageDeadline ??= Date.now() + SANDBOX_WAKE_STAGE_LIMIT_MS;
    const timer = setTimeout(
      () => endSandboxWake(sessionId, wake),
      Math.max(0, wake.stageDeadline - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [wakeRunning, launchInProgress, sessionId]);

  // A failed or waiting stage left over from before the click mustn't end the
  // wake; one reported since then does, even if it repeats the old one.
  useEffect(() => {
    const wake = sessionId ? sandboxWakes.get(sessionId) : undefined;
    if (!sessionId || sandboxWake === null || !wake || launch === undefined) return;
    if (launchKey(launch) !== wake.launch) wake.stageChanged = true;
    if (wake.stageChanged && isSandboxLaunchStopped(launch)) endSandboxWake(sessionId, wake);
  }, [sandboxWake, launch, sessionId]);

  // A press during a running wake has nothing to wait for; the dot shows its progress.
  const wakeSandbox = useCallback(async () => {
    if (sessionId && !sandboxWakes.has(sessionId)) await startSandboxWake(sessionId);
  }, [sessionId]);

  const reconnect = useCallback(async () => {
    if (inFlight.current) return;
    if (hostId && hostResumable) {
      await wakeSandbox();
      return;
    }
    if (!hostId || !isOwner || !isElectronShell()) {
      setDialogOpen(true);
      return;
    }
    // Arca is a remote machine, so it can never match the desktop host
    // identity below. Starting it here still runs nothing on its own: the
    // desktop's Arca console shows the command and waits for confirmation.
    if (isSessionArcaHost()) {
      await reconnectArca();
      return;
    }

    const operation: { progressToast?: string | number } = {};
    inFlight.current = operation;
    setReconnecting(true);
    setError(null);
    try {
      // Recheck identity on each click, including retries after re-enrollment.
      const identity = await getHostIdentity();
      if (inFlight.current !== operation) return;
      if (!identity?.cliInstalled || identity.hostId !== hostId) {
        setCanReconnectThisMachine(false);
        setCanReconnectArca(false);
        setDialogOpen(true);
        return;
      }
      setCanReconnectThisMachine(true);
      operation.progressToast = toast.loading("Reconnecting this machine…");
      const result = await controlHost("start");
      if (inFlight.current !== operation) return;
      if (!result.ok) {
        setError(
          result.error ??
            (result.authError
              ? "Sign-in didn't complete. A browser should have opened. Finish signing in, then try again."
              : "Couldn't reconnect this machine. Try again or run the command below from a terminal."),
        );
        setDialogOpen(true);
        return;
      }
      setDialogOpen(false);
      toast.success("This machine is back online.");
    } finally {
      if (inFlight.current === operation) {
        if (operation.progressToast !== undefined) toast.dismiss(operation.progressToast);
        inFlight.current = null;
        setReconnecting(false);
      }
    }
  }, [hostId, hostResumable, isOwner, isSessionArcaHost, reconnectArca, wakeSandbox]);

  return {
    reconnect,
    /** Whether `reconnect` runs the desktop's Arca connect for this session's host. */
    reconnectsArca: runsArcaConnect(),
    /** A sandbox wake is running or its host has not yet been seen online. */
    sandboxStarting: sandboxWake !== null,
    dialogOpen,
    setDialogOpen,
    localReconnect: canReconnectThisMachine
      ? { reconnecting, error, onReconnect: () => void reconnect() }
      : undefined,
    arcaReconnect: canReconnectArca
      ? { reconnecting, error, onReconnect: () => void reconnectArca() }
      : undefined,
  };
}
