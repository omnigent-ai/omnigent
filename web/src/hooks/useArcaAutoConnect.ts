// Follows Arca auto-connect status from the desktop shell. The host menu shows
// the status itself; this hook keeps the host list fresh and records which
// host is the Arca instance once a connect succeeds.

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { type ArcaStatus, getArcaStatus, onArcaStatusChanged } from "@/lib/nativeBridge";
import { fetchHosts } from "@/hooks/useHosts";
import { writeArcaHostId } from "@/lib/arcaHost";

export type { ArcaStatus };

// How long to wait for the new host to appear in the host list after online.
const POLL_DEADLINE_MS = 30_000;
const POLL_INTERVAL_MS = 1_500;

/**
 * Subscribe to live Arca auto-connect status and expose it as React state.
 * The initial value is fetched once on mount; subsequent changes arrive via
 * the shell's push subscription.
 *
 * Returns null while loading (first fetch pending) or outside Electron.
 */
export function useArcaStatus(): ArcaStatus | null {
  const [status, setStatus] = useState<ArcaStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getArcaStatus().then((s) => {
      if (!cancelled) setStatus(s);
    });
    const unsubscribe = onArcaStatusChanged((s) => {
      if (!cancelled) setStatus(s);
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  return status;
}

/**
 * Mount once in AppShell. Refreshes the host list when Arca comes online and,
 * after a fresh connect, records the newly-online host as the Arca instance.
 * Never selects a host.
 */
export function useArcaAutoConnect(): void {
  const queryClient = useQueryClient();
  // Online host ids when "starting" was first seen, to spot the new Arca host;
  // "pending" while that first fetch is in flight.
  const onlineSnapshotRef = useRef<Set<string> | "pending" | null>(null);

  useEffect(() => {
    let cancelled = false;

    const fetchOnlineHosts = () =>
      queryClient
        .fetchQuery({
          queryKey: ["hosts", { includeSandbox: false }],
          queryFn: () => fetchHosts(false),
          staleTime: 0,
        })
        .then((hosts) => hosts.filter((h) => h.status === "online"))
        .catch(() => null);

    async function handleStatus(status: ArcaStatus) {
      if (cancelled) return;

      if (status.state === "starting") {
        if (onlineSnapshotRef.current !== null) return;
        onlineSnapshotRef.current = "pending";
        const hosts = await fetchOnlineHosts();
        if (cancelled || onlineSnapshotRef.current !== "pending") return;
        // Without a baseline any online host would look new, so skip discovery.
        onlineSnapshotRef.current = hosts ? new Set(hosts.map((h) => h.host_id)) : null;
        return;
      }

      const snapshot = onlineSnapshotRef.current;
      onlineSnapshotRef.current = null;
      if (status.state !== "online") return;

      await queryClient.invalidateQueries({ queryKey: ["hosts"] });
      // A daemon that was already running brings no new host to discover.
      if (status.alreadyRunning || !(snapshot instanceof Set)) return;

      const deadline = Date.now() + POLL_DEADLINE_MS;
      /* oxlint-disable no-await-in-loop */
      while (Date.now() < deadline) {
        if (cancelled) return;
        const hosts = await fetchOnlineHosts();
        const fresh = hosts?.find((h) => !snapshot.has(h.host_id));
        if (fresh) {
          writeArcaHostId(fresh.host_id);
          return;
        }
        await new Promise<void>((resolve) => {
          setTimeout(resolve, POLL_INTERVAL_MS);
        });
      }
      /* oxlint-enable no-await-in-loop */
    }

    void getArcaStatus().then((s) => {
      if (!cancelled && s) void handleStatus(s);
    });
    const unsubscribe = onArcaStatusChanged((s) => {
      void handleStatus(s);
    });

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [queryClient]);
}
