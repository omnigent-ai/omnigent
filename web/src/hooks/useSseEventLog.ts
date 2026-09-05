import { useSyncExternalStore } from "react";
import { subscribeSseLog, snapshotSseLog, type SseLogEntry } from "@/lib/sseEventLog";

/**
 * Returns the live SSE event log for a session as a stable array.
 * Updates synchronously whenever a new event is pushed.
 *
 * Pass `null` to disable (returns an empty array).
 */
export function useSseEventLog(sessionId: string | null): readonly SseLogEntry[] {
  return useSyncExternalStore(
    (cb) => {
      if (sessionId === null) return () => {};
      return subscribeSseLog(sessionId, cb);
    },
    () => (sessionId === null ? EMPTY : snapshotSseLog(sessionId)),
    () => EMPTY,
  );
}

const EMPTY: readonly SseLogEntry[] = [];
