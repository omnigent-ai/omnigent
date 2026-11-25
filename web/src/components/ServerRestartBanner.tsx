import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Spinner } from "@/components/ui/spinner";
import { authenticatedFetch } from "@/lib/identity";
import {
  isServerRestartError,
  SERVER_RESTART_EVENT,
  SERVER_RESTART_MESSAGE,
} from "@/lib/serverRestart";

/** How often to check whether a restarting server is back. */
const RESTART_POLL_MS = 2_000;

async function serverIsBack(): Promise<boolean> {
  try {
    const response = await authenticatedFetch("/health", { cache: "no-store" });
    return response.ok;
  } catch {
    return false;
  }
}

/**
 * While the server is restarting, says so above the app. It waits for the server
 * to answer again, then refreshes the app's data and clears itself, with no reload.
 */
export function ServerRestartBanner() {
  const queryClient = useQueryClient();
  const [restarting, setRestarting] = useState(false);

  useEffect(() => {
    const onRestart = () => setRestarting(true);
    window.addEventListener(SERVER_RESTART_EVENT, onRestart);
    const unsubscribeQueries = queryClient.getQueryCache().subscribe((event) => {
      if (event.type === "updated" && event.action.type === "error") {
        if (isServerRestartError(event.action.error)) onRestart();
      }
    });
    const unsubscribeMutations = queryClient.getMutationCache().subscribe((event) => {
      if (event.type === "updated" && event.action.type === "error") {
        if (isServerRestartError(event.action.error)) onRestart();
      }
    });
    return () => {
      window.removeEventListener(SERVER_RESTART_EVENT, onRestart);
      unsubscribeQueries();
      unsubscribeMutations();
    };
  }, [queryClient]);

  useEffect(() => {
    if (!restarting) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void serverIsBack().then((back) => {
        if (!back || cancelled) return;
        cancelled = true;
        window.clearInterval(timer);
        setRestarting(false);
        void queryClient.invalidateQueries();
      });
    }, RESTART_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [restarting, queryClient]);

  if (!restarting) return null;
  return (
    <div
      role="status"
      data-testid="server-restart-banner"
      className="fixed top-3 left-1/2 z-50 flex -translate-x-1/2 items-center gap-2 rounded-full border border-border bg-background px-4 py-2 text-sm shadow-md"
    >
      <Spinner aria-hidden className="size-3.5" />
      {SERVER_RESTART_MESSAGE}
    </div>
  );
}
