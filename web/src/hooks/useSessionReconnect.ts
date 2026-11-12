import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { toast } from "sonner";

import { onRunnerHealthPolled, refreshRunnerHealth } from "@/hooks/useRunnerHealth";
import { controlHost, getHostIdentity, isElectronShell } from "@/lib/nativeBridge";
import { type PostEventResponse, retrySession } from "@/lib/sessionsApi";
import { isSandboxLaunchStopped } from "@/lib/sandboxStatus";
import type { SandboxStatus } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";

/** How many scheduled health polls may report a woken sandbox's host offline before it gives up. */
export const SANDBOX_WAKE_ONLINE_GRACE_POLLS = 3;

/** How long a wake may follow a launch stage that never finishes before Reconnect host returns. */
export const SANDBOX_WAKE_STAGE_LIMIT_MS = 5 * 60_000;

// One sandbox wake request per session, shared by everything that wakes it
// (Reconnect host, opening the session, the Files and Changes tabs, the
// terminal's Resume): a caller during the request joins it and gets its answer.
const sandboxWakeRequests = new Map<string, Promise<PostEventResponse>>();

/** Send `retry_session` to wake *sessionId*'s sandbox, or join the one in flight. */
export function wakeSandboxSession(sessionId: string): Promise<PostEventResponse> {
  const running = sandboxWakeRequests.get(sessionId);
  if (running) return running;
  const request = retrySession(sessionId).finally(() => {
    if (sandboxWakeRequests.get(sessionId) === request) sandboxWakeRequests.delete(sessionId);
  });
  sandboxWakeRequests.set(sessionId, request);
  return request;
}

// Sessions whose shown view is waking their sandbox, for other panels to say so.
const startingSandboxes = new Map<string, number>();
const startingListeners = new Set<() => void>();

function subscribeStartingSandboxes(listener: () => void): () => void {
  startingListeners.add(listener);
  return () => startingListeners.delete(listener);
}

function markSandboxStarting(sessionId: string, delta: 1 | -1): void {
  const count = (startingSandboxes.get(sessionId) ?? 0) + delta;
  if (count > 0) startingSandboxes.set(sessionId, count);
  else startingSandboxes.delete(sessionId);
  for (const listener of startingListeners) listener();
}

/**
 * Whether *sessionId*'s sandbox is starting, as its host dot shows: Reconnect
 * host is waking it, or the server reports its launch in progress (a wake from
 * before a session switch, another tab, or a sent message).
 */
export function useSandboxStarting(sessionId: string | null | undefined): boolean {
  const read = useCallback(
    () => (sessionId ? startingSandboxes.has(sessionId) : false),
    [sessionId],
  );
  const waking = useSyncExternalStore(subscribeStartingSandboxes, read);
  const launching = useChatStore((s) =>
    s.conversationId === sessionId && s.sandboxStatus !== null
      ? !isSandboxLaunchStopped(s.sandboxStatus)
      : false,
  );
  return waking || launching;
}

/** Identifies a reported launch stage by value; snapshots rebuild the object. */
function launchKey(launch: SandboxStatus | null): string {
  return launch ? JSON.stringify([launch.stage, launch.error, launch.details]) : "";
}

/**
 * Reconnect this desktop's host directly; use the dialog for fallback and retry.
 * A resumable sandbox host is woken by the server instead, with its progress
 * shown by the composer's host status dot rather than a toast.
 */
export function useSessionReconnect({
  sessionId,
  hostId,
  hostResumable = false,
  hostOnline,
  isOwner,
}: {
  sessionId: string | null;
  hostId: string | null;
  hostResumable?: boolean;
  /** The session's host liveness; a woken sandbox counts as starting until it is `true`. */
  hostOnline?: boolean | null;
  isOwner: boolean;
}) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [canReconnectThisMachine, setCanReconnectThisMachine] = useState(false);
  const [sandboxWake, setSandboxWake] = useState<"waking" | "awaiting_online" | null>(null);
  const inFlight = useRef<{ progressToast?: string | number } | null>(null);
  // The open session's launch stage, compared against its value at the wake's click.
  const launch = useChatStore((s) => (s.conversationId === sessionId ? s.sandboxStatus : null));
  const launchInProgress = launch !== null && !isSandboxLaunchStopped(launch);
  const wakeStart = useRef<{ operation: object; launch: string; stageChanged: boolean } | null>(
    null,
  );
  useEffect(() => {
    setDialogOpen(false);
    setError(null);
    setCanReconnectThisMachine(false);
    setReconnecting(false);
    setSandboxWake(null);
    return () => {
      if (inFlight.current?.progressToast !== undefined) {
        toast.dismiss(inFlight.current.progressToast);
      }
      inFlight.current = null;
    };
  }, [sessionId, hostId, isOwner]);

  const starting = sandboxWake !== null;
  useEffect(() => {
    if (!sessionId || !starting) return;
    markSandboxStarting(sessionId, 1);
    return () => markSandboxStarting(sessionId, -1);
  }, [sessionId, starting]);

  // Liveness polls lag the wake, so stay "starting" until the host reads online.
  useEffect(() => {
    if (sandboxWake === "awaiting_online" && hostOnline === true) setSandboxWake(null);
  }, [sandboxWake, hostOnline]);

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
      setSandboxWake(null);
      toast.error("The sandbox didn't come online. Try Reconnect host again.");
    });
  }, [sandboxWake, launchInProgress, sessionId]);

  // The count above waits on the launch, so a stage that never finishes would spin
  // with Reconnect host hidden. Let the wake go quietly: the server's stage keeps the
  // dot spinning, and Reconnect host returns as it does after a reload.
  // Keyed on whether a wake is running, so the server answering mid-stage keeps the deadline.
  const wakeRunning = sandboxWake !== null;
  useEffect(() => {
    if (!wakeRunning || !launchInProgress) return;
    const timer = setTimeout(() => {
      const start = wakeStart.current;
      if (start && inFlight.current === start.operation) inFlight.current = null;
      wakeStart.current = null;
      setSandboxWake(null);
      // A request whose answer never comes mustn't capture the next Reconnect host.
      if (sessionId) sandboxWakeRequests.delete(sessionId);
    }, SANDBOX_WAKE_STAGE_LIMIT_MS);
    return () => clearTimeout(timer);
  }, [wakeRunning, launchInProgress, sessionId]);

  // A failed or waiting stage left over from before the click mustn't end the
  // wake; one reported since then does, even if it repeats the old one.
  useEffect(() => {
    const start = wakeStart.current;
    if (sandboxWake === null || !start) return;
    if (launchKey(launch) !== start.launch) start.stageChanged = true;
    if (!start.stageChanged || !isSandboxLaunchStopped(launch)) return;
    if (inFlight.current === start.operation) inFlight.current = null;
    wakeStart.current = null;
    setSandboxWake(null);
  }, [sandboxWake, launch]);

  // The server resumes the sandbox and starts the agent, as it does for a
  // message, so this works in the browser and for anyone who can send one.
  const wakeSandbox = useCallback(async () => {
    if (inFlight.current || !sessionId) return;
    const operation = {};
    inFlight.current = operation;
    const store = useChatStore.getState();
    const clickLaunch = store.conversationId === sessionId ? store.sandboxStatus : null;
    wakeStart.current = { operation, launch: launchKey(clickLaunch), stageChanged: false };
    setSandboxWake("waking");
    try {
      await wakeSandboxSession(sessionId);
      if (inFlight.current !== operation) return;
      setSandboxWake("awaiting_online");
      refreshRunnerHealth();
    } catch (err) {
      if (inFlight.current !== operation) return;
      setSandboxWake(null);
      toast.error(
        err instanceof Error && err.message
          ? `Couldn't reconnect the sandbox: ${err.message}`
          : "Couldn't reconnect the sandbox. Try again.",
      );
    } finally {
      if (inFlight.current === operation) inFlight.current = null;
    }
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
  }, [hostId, hostResumable, isOwner, wakeSandbox]);

  return {
    reconnect,
    /** A sandbox wake is running or its host has not yet been seen online. */
    sandboxStarting: sandboxWake !== null,
    dialogOpen,
    setDialogOpen,
    localReconnect: canReconnectThisMachine
      ? { reconnecting, error, onReconnect: () => void reconnect() }
      : undefined,
  };
}
