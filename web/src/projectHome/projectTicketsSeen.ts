import { useEffect, useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getTicketCreationSummary } from "@/lib/projectTicketsApi";

// Per-device marker of the newest ticket creation time (epoch seconds) the
// viewer has seen on each project's board, keyed by project id.
const STORAGE_KEY = "omnigent:project-tickets-seen";
const SIDEBAR_POLL_MS = 30_000;
const BOARD_POLL_MS = 5_000;

const listeners = new Set<() => void>();

function readAll(): Record<string, number> {
  if (typeof window === "undefined") return {};
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}");
    return parsed && typeof parsed === "object" ? (parsed as Record<string, number>) : {};
  } catch {
    return {};
  }
}

export function readProjectTicketsSeen(projectId: string): number | undefined {
  const value = readAll()[projectId];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/** Advance a project's seen marker; it never moves backwards. */
export function markProjectTicketsSeen(projectId: string, createdAt: number): void {
  const current = readProjectTicketsSeen(projectId);
  if (current !== undefined && current >= createdAt) return;
  try {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ ...readAll(), [projectId]: createdAt }),
    );
  } catch {
    // Storage quota or access errors only cost the badge.
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    if (event.key === STORAGE_KEY) listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

function useProjectTicketsSeen(projectId: string | null): number | undefined {
  return useSyncExternalStore(
    subscribe,
    () => (projectId ? readProjectTicketsSeen(projectId) : undefined),
    () => undefined,
  );
}

function useSummarySupported() {
  const info = useServerInfo();
  return info !== "loading" && info.project_ticket_summary === true;
}

/**
 * Tickets created on a project's board since the viewer last had it open. The
 * first read records the current newest ticket as seen, so existing tickets
 * never count as new.
 */
export function useNewTicketCount(projectId: string | null, enabled = true): number {
  const supported = useSummarySupported();
  const seen = useProjectTicketsSeen(projectId);
  const query = useQuery({
    queryKey: ["project-tickets", projectId, "summary", "created-after", seen ?? null],
    queryFn: () => getTicketCreationSummary(projectId!, seen),
    enabled: projectId !== null && supported && enabled,
    refetchInterval: SIDEBAR_POLL_MS,
  });
  const latest = query.data?.latest_created_at;
  useEffect(() => {
    // Older servers omit the field; only a definite answer sets the baseline.
    if (projectId && seen === undefined && latest !== undefined) {
      markProjectTicketsSeen(projectId, latest ?? 0);
    }
  }, [projectId, seen, latest]);
  if (!enabled || seen === undefined) return 0;
  return query.data?.created_after_count ?? 0;
}

/** While the board is open, keep its seen marker at the newest ticket. */
export function useMarkProjectTicketsSeen(projectId: string | null, active: boolean): void {
  const supported = useSummarySupported();
  const query = useQuery({
    queryKey: ["project-tickets", projectId, "summary", "created-after", "board"],
    queryFn: () => getTicketCreationSummary(projectId!),
    enabled: projectId !== null && supported && active,
    refetchInterval: BOARD_POLL_MS,
  });
  const latest = query.data?.latest_created_at;
  useEffect(() => {
    if (projectId && active && latest !== undefined) markProjectTicketsSeen(projectId, latest ?? 0);
  }, [projectId, active, latest]);
}
