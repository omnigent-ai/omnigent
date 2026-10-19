import { AlertTriangleIcon, XIcon } from "lucide-react";

import { useHostDbcert } from "@/hooks/useHostDbcert";
import { cn } from "@/lib/utils";

/**
 * Warns that the dbcert on this session's host has expired and
 * offers a one-click refresh (see `useHostDbcert`). Styled like
 * `StreamBudgetBanner` and stacked with it in a `FloatingBannerStack`.
 * Dismissable per expired certificate.
 */
export function DbcertBanner({
  conversationId,
  hostId,
}: {
  conversationId: string | null | undefined;
  hostId: string | null | undefined;
}) {
  const dbcert = useHostDbcert(conversationId, hostId);
  if (!dbcert.show) return null;
  const running = dbcert.phase === "running";

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "pointer-events-auto flex max-w-2xl items-center gap-2.5 rounded-xl px-3.5 py-2 text-ui shadow-lg",
        "border border-amber-300 bg-amber-100 text-amber-900",
        "dark:border-amber-700/60 dark:bg-amber-950 dark:text-amber-100",
        "animate-in fade-in-0 slide-in-from-top-1 duration-200",
      )}
    >
      <AlertTriangleIcon className="size-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p>
          The dbcert on {dbcert.hostName || "this host"} has expired. Certain internal tools will
          fail until you refresh it.
        </p>
        {running && (
          <p className="mt-0.5 opacity-80">Finish the sign-in in the browser tab that opened.</p>
        )}
        {dbcert.phase === "failed" && (
          <p className="mt-0.5 opacity-80">
            Couldn't refresh it from here. Click dbcert in the Arca Companion menu on your laptop.
          </p>
        )}
      </div>
      <button
        type="button"
        disabled={running}
        onClick={() => void dbcert.refresh()}
        className={cn(
          "shrink-0 rounded-md border border-amber-400 px-2 py-0.5 font-medium hover:bg-amber-200 disabled:opacity-60",
          "dark:border-amber-700 dark:hover:bg-amber-900",
        )}
      >
        {running ? "Refreshing…" : dbcert.phase === "failed" ? "Try again" : "Refresh dbcert"}
      </button>
      <button
        type="button"
        onClick={dbcert.dismiss}
        aria-label="Dismiss"
        className={cn(
          "-mr-1 shrink-0 rounded p-0.5 text-amber-900/60 hover:bg-amber-200 hover:text-amber-900",
          "dark:text-amber-100/60 dark:hover:bg-amber-900 dark:hover:text-amber-100",
        )}
      >
        <XIcon className="size-4" aria-hidden="true" />
      </button>
    </div>
  );
}
