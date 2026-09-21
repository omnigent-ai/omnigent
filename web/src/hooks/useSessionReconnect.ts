import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { controlHost, getHostIdentity, isElectronShell } from "@/lib/nativeBridge";

/** Reconnect this desktop's host directly; use the dialog for fallback and retry. */
export function useSessionReconnect({
  sessionId,
  hostId,
  isOwner,
}: {
  sessionId: string | null;
  hostId: string | null;
  isOwner: boolean;
}) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [canReconnectThisMachine, setCanReconnectThisMachine] = useState(false);
  const inFlight = useRef<{ progressToast?: string | number } | null>(null);

  useEffect(() => {
    setDialogOpen(false);
    setError(null);
    setCanReconnectThisMachine(false);
    setReconnecting(false);
    return () => {
      if (inFlight.current?.progressToast !== undefined) {
        toast.dismiss(inFlight.current.progressToast);
      }
      inFlight.current = null;
    };
  }, [sessionId, hostId, isOwner]);

  const reconnect = useCallback(async () => {
    if (inFlight.current) return;
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
      toast.success("Host start requested.");
    } finally {
      if (inFlight.current === operation) {
        if (operation.progressToast !== undefined) toast.dismiss(operation.progressToast);
        inFlight.current = null;
        setReconnecting(false);
      }
    }
  }, [hostId, isOwner]);

  return {
    reconnect,
    dialogOpen,
    setDialogOpen,
    localReconnect: canReconnectThisMachine
      ? { reconnecting, error, onReconnect: () => void reconnect() }
      : undefined,
  };
}
