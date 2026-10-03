import { useEffect, useRef } from "react";
import type { SessionStatus } from "@/lib/types";

const ACTIVE_STATUSES: ReadonlySet<SessionStatus> = new Set(["running", "waiting"]);

/**
 * Call ``onRefresh`` when a turn starts or ends, so a goal set from a typed
 * ``/goal`` shows up as work begins and its status updates when the turn ends.
 * Session switches never trigger a refresh.
 */
export function useGoalRefreshOnTurnChange(
  sessionId: string | null,
  sessionStatus: SessionStatus,
  enabled: boolean,
  onRefresh: () => void,
): void {
  const prevRef = useRef<{ sessionId: string | null; status: SessionStatus }>({
    sessionId,
    status: sessionStatus,
  });
  useEffect(() => {
    const prev = prevRef.current;
    prevRef.current = { sessionId, status: sessionStatus };
    if (!enabled || sessionId === null || sessionId !== prev.sessionId) return;
    const turnStarted = prev.status === "idle" && ACTIVE_STATUSES.has(sessionStatus);
    const turnEnded = ACTIVE_STATUSES.has(prev.status) && sessionStatus === "idle";
    if (turnStarted || turnEnded) onRefresh();
  }, [sessionStatus, sessionId, enabled, onRefresh]);
}
