import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { isArcaHost, readArcaHostId } from "@/lib/arcaHost";
import { connectArcaHost, controlHost, getHostIdentity, isElectronShell } from "@/lib/nativeBridge";
import type { HostIdentity } from "@/lib/nativeBridge";

/** Reconnect this desktop's host directly; use the dialog for fallback and retry. */
export function useSessionReconnect({
  sessionId,
  hostId,
  hostName,
  isOwner,
  hasHostHeldMessages = false,
  onAutomaticReconnect,
}: {
  sessionId: string | null;
  hostId: string | null;
  hostName?: string | null;
  isOwner: boolean;
  hasHostHeldMessages?: boolean;
  onAutomaticReconnect?: () => void;
}) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [reconnected, setReconnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [canReconnectThisMachine, setCanReconnectThisMachine] = useState(false);
  const [canReconnectArca, setCanReconnectArca] = useState(false);
  const [localHostId, setLocalHostId] = useState<string | null>(null);
  const inFlight = useRef<{ progressToast?: string | number } | null>(null);
  const identityLookup = useRef<Promise<HostIdentity | null> | null>(null);
  const automaticAttempt = useRef(false);
  const isSessionArcaHost = useCallback(
    () =>
      hostId !== null && isArcaHost({ host_id: hostId, name: hostName ?? "" }, readArcaHostId()),
    [hostId, hostName],
  );
  const desktopOwner = Boolean(hostId && isOwner && isElectronShell());
  const arcaHost = isSessionArcaHost();
  const canReconnectOnSend = desktopOwner && (arcaHost || localHostId === hostId);
  const lookupIdentity = useCallback(() => {
    identityLookup.current ??= getHostIdentity().finally(() => {
      identityLookup.current = null;
    });
    return identityLookup.current;
  }, []);

  useEffect(() => {
    let active = true;
    setLocalHostId(null);
    if (desktopOwner && !arcaHost) {
      void lookupIdentity().then((identity) => {
        if (active) setLocalHostId(identity?.cliInstalled ? (identity.hostId ?? null) : null);
      });
    }
    return () => {
      active = false;
    };
  }, [desktopOwner, arcaHost, hostId, lookupIdentity]);

  useEffect(() => {
    setDialogOpen(false);
    setError(null);
    setCanReconnectThisMachine(false);
    setCanReconnectArca(false);
    setReconnecting(false);
    setReconnected(false);
    automaticAttempt.current = false;
    return () => {
      if (inFlight.current?.progressToast !== undefined) {
        toast.dismiss(inFlight.current.progressToast);
      }
      inFlight.current = null;
    };
  }, [sessionId, hostId, isOwner]);

  const reconnectArca = useCallback(async () => {
    if (inFlight.current) return;
    if (!hostId || !isOwner || !isElectronShell() || !isSessionArcaHost()) {
      setCanReconnectArca(false);
      setDialogOpen(true);
      return;
    }

    const operation: { progressToast?: string | number } = {};
    inFlight.current = operation;
    setCanReconnectArca(true);
    setReconnecting(true);
    setReconnected(false);
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
      setDialogOpen(false);
      setReconnected(true);
      toast.success(
        result.alreadyRunning ? "Arca host is connected." : "Arca reconnect requested.",
      );
    } finally {
      if (inFlight.current === operation) {
        if (operation.progressToast !== undefined) toast.dismiss(operation.progressToast);
        inFlight.current = null;
        setReconnecting(false);
      }
    }
  }, [hostId, isOwner, isSessionArcaHost]);

  const reconnect = useCallback(async () => {
    if (inFlight.current) return;
    if (!hostId || !isOwner || !isElectronShell()) {
      setDialogOpen(true);
      return;
    }
    // Arca cannot match this machine's desktop identity.
    if (isSessionArcaHost()) {
      await reconnectArca();
      return;
    }

    const operation: { progressToast?: string | number } = {};
    inFlight.current = operation;
    setReconnecting(true);
    setReconnected(false);
    setError(null);
    try {
      // Recheck identity on each click, including retries after re-enrollment.
      const identity = await lookupIdentity();
      if (inFlight.current !== operation) return;
      setLocalHostId(identity?.cliInstalled ? (identity.hostId ?? null) : null);
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
      setReconnected(true);
      toast.success("Host start requested.");
    } finally {
      if (inFlight.current === operation) {
        if (operation.progressToast !== undefined) toast.dismiss(operation.progressToast);
        inFlight.current = null;
        setReconnecting(false);
      }
    }
  }, [hostId, isOwner, isSessionArcaHost, reconnectArca, lookupIdentity]);

  // A send can discover the outage before health polling reports it.
  useEffect(() => {
    if (!hasHostHeldMessages) automaticAttempt.current = false;
    else if (canReconnectOnSend && !automaticAttempt.current) {
      let active = true;
      queueMicrotask(() => {
        if (!active) return;
        automaticAttempt.current = true;
        onAutomaticReconnect?.();
        void reconnect();
      });
      return () => {
        active = false;
      };
    }
    return undefined;
  }, [sessionId, hasHostHeldMessages, canReconnectOnSend, reconnect, onAutomaticReconnect]);

  return {
    reconnect,
    reconnected,
    canReconnectOnSend,
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
