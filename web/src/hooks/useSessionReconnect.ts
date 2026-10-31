import { useCallback, useEffect, useRef, useState } from "react";
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
import type { SandboxStatus } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";

/** How many health polls may report a woken sandbox's host offline before the wake gives up. */
export const SANDBOX_WAKE_ONLINE_GRACE_POLLS = 3;

/** Identifies a reported launch stage by value; snapshots rebuild the object. */
function launchKey(launch: SandboxStatus | null): string {
  return launch ? JSON.stringify([launch.stage, launch.error, launch.details]) : "";
}

/** A launch that failed or waits on the user is no longer making progress. */
function launchStopped(launch: SandboxStatus | null): boolean {
  return launch?.stage === "failed" || launch?.stage === "action_required";
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
  const [sandboxWake, setSandboxWake] = useState<"waking" | "awaiting_online" | null>(null);
  const inFlight = useRef<{ progressToast?: string | number } | null>(null);
  // The open session's launch stage, compared against its value at the wake's click.
  const launch = useChatStore((s) => (s.conversationId === sessionId ? s.sandboxStatus : null));
  const launchInProgress = launch !== null && !launchStopped(launch);
  const wakeStart = useRef<{ operation: object; launch: string; stageChanged: boolean } | null>(
    null,
  );
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
    setSandboxWake(null);
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
    if (sandboxWake === "awaiting_online" && hostOnline === true) setSandboxWake(null);
  }, [sandboxWake, hostOnline]);

  // Count polls, not time, since `/health` backs off on failures. The count waits
  // for the launch to stop reporting progress, and starts from zero then.
  useEffect(() => {
    if (sandboxWake !== "awaiting_online" || launchInProgress || !sessionId) return;
    let offlinePolls = 0;
    return onRunnerHealthPolled((liveness) => {
      const session = liveness.get(sessionId);
      if (!session || session.host_online === true) return;
      offlinePolls += 1;
      if (offlinePolls !== SANDBOX_WAKE_ONLINE_GRACE_POLLS) return;
      setSandboxWake(null);
      toast.error("The sandbox didn't come online. Try Reconnect host again.");
    });
  }, [sandboxWake, launchInProgress, sessionId]);

  // A failed or waiting stage left over from before the click mustn't end the
  // wake; one reported since then does, even if it repeats the old one.
  useEffect(() => {
    const start = wakeStart.current;
    if (sandboxWake === null || !start) return;
    if (launchKey(launch) !== start.launch) start.stageChanged = true;
    if (!start.stageChanged || !launchStopped(launch)) return;
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
      await retrySession(sessionId);
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
