// Sessions this tab created, with the first message each was created with. A page that
// starts work in a new session (a gadget's New session) checks here that the session it
// lands on is the one it created, not one the viewer navigated to instead, and which of
// its requests that first message carried. Each creation gets a sequence number, so a page
// can tell a session created after it began waiting from one created earlier.
const created = new Map<string, { firstMessage: string | null; seq: number }>();
const listeners = new Set<(sessionId: string, firstMessage: string | null, seq: number) => void>();
let lastSeq = 0;
// Enough for any session a page in this tab is still waiting on.
const MAX_REMEMBERED = 50;

/** Records that this tab created `sessionId`, with its first message when it had one. */
export function noteSessionCreatedHere(sessionId: string, firstMessage: string | null): void {
  const known = created.get(sessionId);
  created.delete(sessionId);
  const record = {
    firstMessage: firstMessage ?? known?.firstMessage ?? null,
    seq: known?.seq ?? ++lastSeq,
  };
  created.set(sessionId, record);
  while (created.size > MAX_REMEMBERED) {
    const oldest = created.keys().next().value;
    if (oldest === undefined) break;
    created.delete(oldest);
  }
  if (known === undefined) {
    for (const listener of [...listeners]) listener(sessionId, record.firstMessage, record.seq);
  }
}

/** The session this tab created, or `undefined` for one it didn't. */
export function sessionCreatedHere(
  sessionId: string,
): { firstMessage: string | null; seq: number } | undefined {
  return created.get(sessionId);
}

/** The sequence number of the latest session this tab created; 0 before the first. */
export function latestSessionCreation(): number {
  return lastSeq;
}

/** Calls `listener` for each session this tab creates from now on. Returns an unsubscribe. */
export function onSessionCreatedHere(
  listener: (sessionId: string, firstMessage: string | null, seq: number) => void,
): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Forgets every created session; for tests. */
export function resetSessionCreations(): void {
  created.clear();
  lastSeq = 0;
}
