// Module-level ring buffer for raw SSE events captured in debug mode
// (?debug=1). Only populated when the debug flag is active at stream time —
// zero overhead in normal use.
//
// Each session gets its own capped list (MAX_EVENTS). Older entries are
// dropped from the front once the cap is reached. Listeners are notified
// synchronously after each push so React can re-render without polling.

import type { StreamEvent } from "@/lib/events";

export interface SseLogEntry {
  /** Monotonically increasing index within this session's log. */
  index: number;
  /** Wall-clock timestamp when the event was captured (ms since epoch). */
  ts: number;
  event: StreamEvent;
}

const MAX_EVENTS = 500;

// Per-session log state.
interface SessionLog {
  entries: SseLogEntry[];
  nextIndex: number;
  listeners: Set<() => void>;
}

const logs = new Map<string, SessionLog>();

function getOrCreate(sessionId: string): SessionLog {
  let log = logs.get(sessionId);
  if (!log) {
    log = { entries: [], nextIndex: 0, listeners: new Set() };
    logs.set(sessionId, log);
  }
  return log;
}

// Re-read on every call: localStorage and window.location.search are both
// fast O(1) reads. Caching against popstate was unreliable because React
// Router uses pushState/replaceState which do not fire popstate.
function isDebugMode(): boolean {
  if (typeof window === "undefined") return false;
  if (new URLSearchParams(window.location.search).get("debug") === "1") return true;
  try {
    return localStorage.getItem("debug") === "1";
  } catch {
    return false;
  }
}

const EMPTY: readonly SseLogEntry[] = [];

/** Push one event into the session's ring buffer. No-op when debug is off. */
export function pushSseEvent(sessionId: string, event: StreamEvent): void {
  if (!isDebugMode()) return;
  const log = getOrCreate(sessionId);
  const entry: SseLogEntry = { index: log.nextIndex++, ts: Date.now(), event };
  // Always produce a new array reference so useSyncExternalStore's Object.is
  // check detects the change and triggers a re-render.
  const prev = log.entries.length >= MAX_EVENTS ? log.entries.slice(1) : log.entries;
  log.entries = [...prev, entry];
  for (const l of log.listeners) l();
}

/** Clear a session's log (e.g. on new stream bind). */
export function clearSseLog(sessionId: string): void {
  const log = logs.get(sessionId);
  if (!log) return;
  log.entries = [];
  log.nextIndex = 0;
  for (const l of log.listeners) l();
}

/** Subscribe to changes for a session. Returns an unsubscribe function. */
export function subscribeSseLog(sessionId: string, cb: () => void): () => void {
  const log = getOrCreate(sessionId);
  log.listeners.add(cb);
  return () => log.listeners.delete(cb);
}

/** Snapshot of entries for a session (stable empty array when none). */
export function snapshotSseLog(sessionId: string): readonly SseLogEntry[] {
  return logs.get(sessionId)?.entries ?? EMPTY;
}
