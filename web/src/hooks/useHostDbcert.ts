import { useCallback, useEffect, useRef, useState } from "react";
import { useHosts } from "@/hooks/useHosts";
import { useNowSelector } from "@/hooks/useNow";
import { isDatabricksWorkspace } from "@/lib/host";
import {
  HOST_DBCERT_REFRESH_MS,
  readHostDbcertExpiry,
  startHostDbcertRefresh,
} from "@/lib/hostDbcert";
import { getDesktopFeatures, hasDbcertRefreshBridge, refreshDbcert } from "@/lib/nativeBridge";

/** How often to re-read a valid cert, since Companion may replace it early. */
const CHECK_INTERVAL_MS = 5 * 60_000;
/** How often to re-read an expired cert, so a refresh made elsewhere clears the banner. */
const EXPIRED_CHECK_INTERVAL_MS = 60_000;
/** How often to re-read the cert while a refresh runs. */
const REFRESH_POLL_MS = 4_000;
/** How long Companion may take to copy a laptop refresh to the host. */
const LAPTOP_SYNC_WAIT_MS = 2 * 60_000;

export type DbcertRefreshPhase = "idle" | "running" | "failed";

/**
 * Track the dbcert on the session's host, if it has one, and renew it. In the
 * desktop app a refresh renews the laptop's dbcert, which Arca Companion copies
 * to every box; elsewhere it renews the host's own copy.
 */
export function useHostDbcert(
  conversationId: string | null | undefined,
  hostId: string | null | undefined,
) {
  // dbcert is Databricks-internal, so only Databricks deployments ask hosts for one.
  const onDatabricks = isDatabricksWorkspace();
  // Arclets are server-managed sandbox hosts, which the default host list hides.
  const { data: hosts } = useHosts({
    includeSandbox: true,
    enabled: onDatabricks && Boolean(conversationId && hostId),
  });
  const host = hosts?.find((candidate) => candidate.host_id === hostId);
  const sessionId =
    onDatabricks && conversationId && host?.status === "online" ? conversationId : null;
  const [cert, setCert] = useState<{ sessionId: string; expiresAt: number } | null>(null);
  const expiresAt = cert?.sessionId === sessionId ? cert.expiresAt : null;
  const expired = useNowSelector((now) => expiresAt !== null && expiresAt <= now.getTime(), {
    enabled: expiresAt !== null,
  });
  const [refreshState, setRefreshState] = useState<{
    sessionId: string;
    phase: DbcertRefreshPhase;
  } | null>(null);
  const phase = refreshState?.sessionId === sessionId ? refreshState.phase : "idle";
  const [dismissedExpiry, setDismissedExpiry] = useState<number | null>(null);
  // The session a pending refresh loop belongs to; null once this unmounts.
  const currentSession = useRef(sessionId);
  currentSession.current = sessionId;
  useEffect(
    () => () => {
      currentSession.current = null;
    },
    [],
  );

  const check = useCallback(async () => {
    if (!sessionId) return undefined;
    const next = await readHostDbcertExpiry(sessionId);
    if (next !== undefined && currentSession.current === sessionId) {
      setCert(next === null ? null : { sessionId, expiresAt: next });
    }
    return next;
  }, [sessionId]);

  // Re-read on open, when the tab comes back, and periodically. `expired` is a
  // dependency so the expiry moment itself triggers a read: Companion may have
  // already replaced the cert.
  useEffect(() => {
    if (!sessionId) return;
    const checkIfVisible = () => {
      if (document.visibilityState === "visible") void check();
    };
    checkIfVisible();
    document.addEventListener("visibilitychange", checkIfVisible);
    const timer = setInterval(
      checkIfVisible,
      expired ? EXPIRED_CHECK_INTERVAL_MS : CHECK_INTERVAL_MS,
    );
    return () => {
      document.removeEventListener("visibilitychange", checkIfVisible);
      clearInterval(timer);
    };
  }, [sessionId, check, expired]);

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    setRefreshState({ sessionId, phase: "running" });
    const onLaptop =
      hasDbcertRefreshBridge() && (await getDesktopFeatures())?.canRefreshDbcert === true;
    const started = onLaptop ? (await refreshDbcert()).ok : await startHostDbcertRefresh(sessionId);
    const deadline = Date.now() + (onLaptop ? LAPTOP_SYNC_WAIT_MS : HOST_DBCERT_REFRESH_MS);
    // Poll the host until the renewed cert lands, the wait runs out, or the view moves on.
    const renewed = async (): Promise<boolean> => {
      if (currentSession.current !== sessionId || Date.now() >= deadline) return false;
      const next = await check();
      if (typeof next === "number" && next > Date.now()) return true;
      await new Promise<void>((resolve) => {
        setTimeout(resolve, REFRESH_POLL_MS);
      });
      return renewed();
    };
    const outcome = started && (await renewed()) ? "idle" : "failed";
    if (currentSession.current === sessionId) setRefreshState({ sessionId, phase: outcome });
  }, [sessionId, check]);

  return {
    show: expired && expiresAt !== dismissedExpiry,
    hostName: host?.name ?? "",
    phase,
    refresh,
    dismiss: () => setDismissedExpiry(expiresAt),
  };
}
