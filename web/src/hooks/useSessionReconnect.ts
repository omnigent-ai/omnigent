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
  const inFlight = useRef(false);
  const generation = useRef(0);

  useEffect(() => {
    setDialogOpen(false);
    setError(null);
    setCanReconnectThisMachine(false);
    return () => {
      generation.current += 1;
    };
  }, [sessionId, hostId, isOwner]);

  const reconnect = useCallback(async () => {
    if (inFlight.current) return;
    if (!hostId || !isOwner || !isElectronShell()) {
      setDialogOpen(true);
      return;
    }

    inFlight.current = true;
    setReconnecting(true);
    setError(null);
    const startedGeneration = generation.current;
    let progressToast: string | number | undefined;
    try {
      // Recheck identity on each click, including retries after re-enrollment.
      const identity = await getHostIdentity();
      if (generation.current !== startedGeneration) return;
      if (!identity?.cliInstalled || identity.hostId !== hostId) {
        setCanReconnectThisMachine(false);
        setDialogOpen(true);
        return;
      }
      setCanReconnectThisMachine(true);
      progressToast = toast.loading("Reconnecting this machine…");
      const result = await controlHost("start");
      if (generation.current !== startedGeneration) return;
      if (!result.ok) {
        setError(
          result.error ??
            (result.authError
              ? "Sign-in didn't complete. A browser should have opened — finish signing in, then try again."
              : "Couldn't reconnect this machine. Try again or run the command below from a terminal."),
        );
        setDialogOpen(true);
        return;
      }
      setDialogOpen(false);
      toast.success("Host reconnected.");
    } finally {
      if (progressToast !== undefined) toast.dismiss(progressToast);
      inFlight.current = false;
      setReconnecting(false);
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
