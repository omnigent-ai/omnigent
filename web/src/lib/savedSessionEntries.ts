// Per-session entries saved under one storage key, kept to the most recently written sessions so
// abandoned sessions can't grow the key until writes start failing.

/** How many sessions' entries a key keeps; the least recently written go first. */
export const MAX_SAVED_SESSIONS = 100;

/**
 * ``sessions`` with ``sessionId``'s entry set to ``entry``, as its most recent, or removed when
 * ``entry`` is undefined; past ``limit`` sessions, the least recently written are dropped.
 */
export function withSavedSessionEntry(
  sessions: Readonly<Record<string, unknown>>,
  sessionId: string,
  entry: unknown,
  limit: number = MAX_SAVED_SESSIONS,
): Record<string, unknown> {
  const next = { ...sessions };
  // Deleting first moves a rewritten session to the end, since keys keep insertion order.
  Reflect.deleteProperty(next, sessionId);
  if (entry === undefined) return next;
  next[sessionId] = entry;
  const ids = Object.keys(next);
  for (const oldest of ids.slice(0, Math.max(0, ids.length - limit))) {
    Reflect.deleteProperty(next, oldest);
  }
  return next;
}
