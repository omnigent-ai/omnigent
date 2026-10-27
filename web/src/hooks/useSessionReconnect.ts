import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { fetchHosts } from "@/hooks/useHosts";
import { isArcaHost, readArcaHostId, writeArcaHostId } from "@/lib/arcaHost";
import {
  connectArcaHost,
  controlHost,
  getHostIdentity,
  isElectronShell,
  supportsArcaConnect,
} from "@/lib/nativeBridge";

/** Reconnect this desktop's host directly; use the dialog for fallback and retry. */
export function useSessionReconnect({
  sessionId,
  hostId,
  hostName,
  isOwner,
}: {
  sessionId: string | null;
  hostId: string | null;
  hostName?: string | null;
  isOwner: boolean;
}) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [canReconnectThisMachine, setCanReconnectThisMachine] = useState(false);
  const [canReconnectArca, setCanReconnectArca] = useState(false);
  const inFlight = useRef<{ progressToast?: string | number } | null>(null);
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

  const reconnect = useCallback(async () => {
    if (inFlight.current) return;
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
  }, [hostId, isOwner, isSessionArcaHost, reconnectArca]);

  return {
    reconnect,
    /** Whether `reconnect` runs the desktop's Arca connect for this session's host. */
    reconnectsArca: runsArcaConnect(),
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
