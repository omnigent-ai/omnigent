import { AlertTriangleIcon, XIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { useHostDbcert } from "@/hooks/useHostDbcert";
import {
  checkDbcert,
  getDesktopFeatures,
  hasDbcertRefreshBridge,
  refreshDbcert,
} from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";

/**
 * Warns that the dbcert on this session's host has expired and
 * offers a one-click refresh (see `useHostDbcert`). Styled like
 * `StreamBudgetBanner` and stacked with it in a `FloatingBannerStack`.
 * Dismissable per expired certificate.
 */
interface DbcertBannerProps {
  conversationId: string | null | undefined;
  hostId: string | null | undefined;
}

export function DbcertBanner({ conversationId, hostId }: DbcertBannerProps) {
  const [supported, setSupported] = useState(false);

  useEffect(() => {
    if (!hasDbcertRefreshBridge()) return;
    let active = true;
    void getDesktopFeatures().then((features) => {
      if (active) {
        setSupported(
          features?.databricksInternalFeatures === true && features.canRefreshDbcert === true,
        );
      }
    });
    return () => {
      active = false;
    };
  }, []);

  if (!supported) return null;
  return <SupportedDbcertBanner conversationId={conversationId} hostId={hostId} />;
}

function SupportedDbcertBanner({ conversationId, hostId }: DbcertBannerProps) {
  const dbcert = useHostDbcert(conversationId, hostId);
  if (!dbcert.show) return null;
  return (
    <DbcertRefreshBanner
      message={`The dbcert on ${dbcert.hostName || "this host"} has expired. Certain internal tools will fail until you refresh it.`}
      running={dbcert.phase === "running"}
      error={
        dbcert.phase === "failed"
          ? "Couldn't refresh it from here. Click dbcert in the Arca Companion menu on your laptop."
          : null
      }
      onRefresh={() => void dbcert.refresh()}
      onDismiss={dbcert.dismiss}
    />
  );
}

export function LaptopDbcertBanner({
  onReady,
  onDismiss,
}: {
  onReady: () => void;
  onDismiss: () => void;
}) {
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refreshAttemptRef = useRef<symbol | null>(null);
  useEffect(
    () => () => {
      refreshAttemptRef.current = null;
    },
    [],
  );

  async function refresh() {
    if (refreshAttemptRef.current !== null) return;
    const attempt = Symbol();
    refreshAttemptRef.current = attempt;
    setRunning(true);
    setError(null);
    const result = await refreshDbcert({ skipArcaSync: true });
    if (refreshAttemptRef.current !== attempt) return;
    if (!result.ok) {
      setError(result.error);
    } else {
      const status = await checkDbcert();
      if (refreshAttemptRef.current !== attempt) return;
      if (!status.ok) setError(status.error);
      else if (!status.valid)
        setError("Your laptop's dbcert is still invalid after sign-in. Try refreshing it again.");
      else onReady();
    }
    refreshAttemptRef.current = null;
    setRunning(false);
  }

  return (
    <DbcertRefreshBanner
      message="Your laptop's dbcert is missing or expired. Refresh it, then try again."
      running={running}
      error={error}
      onRefresh={() => void refresh()}
      onDismiss={onDismiss}
    />
  );
}

function DbcertRefreshBanner({
  message,
  running,
  error,
  onRefresh,
  onDismiss,
}: {
  message: string;
  running: boolean;
  error: string | null;
  onRefresh: () => void;
  onDismiss: () => void;
}) {
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
        <p>{message}</p>
        {running && (
          <p className="mt-0.5 opacity-80">
            Opening browser sign-in. Complete sign-in to refresh your dbcert.
          </p>
        )}
        {error && <p className="mt-0.5 opacity-80">{error}</p>}
      </div>
      <button
        type="button"
        disabled={running}
        onClick={onRefresh}
        className={cn(
          "shrink-0 rounded-md border border-amber-400 px-2 py-0.5 font-medium hover:bg-amber-200 disabled:opacity-60",
          "dark:border-amber-700 dark:hover:bg-amber-900",
        )}
      >
        {running ? "Refreshing…" : error ? "Try again" : "Refresh dbcert"}
      </button>
      <button
        type="button"
        onClick={onDismiss}
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
