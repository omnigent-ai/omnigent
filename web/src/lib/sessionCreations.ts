// Sessions this tab created, with the first message each was created with. A page that
// starts work in a new session (a gadget's New session) checks here that the session it
// lands on is the one it created, not one the viewer navigated to instead, and which of
// its requests that first message carried.
const created = new Map<string, { firstMessage: string | null }>();
// Enough for any session a page in this tab is still waiting on.
const MAX_REMEMBERED = 50;

/** Records that this tab created `sessionId`, with its first message when it had one. */
export function noteSessionCreatedHere(sessionId: string, firstMessage: string | null): void {
  const known = created.get(sessionId);
  created.delete(sessionId);
  created.set(sessionId, { firstMessage: firstMessage ?? known?.firstMessage ?? null });
  while (created.size > MAX_REMEMBERED) {
    const oldest = created.keys().next().value;
    if (oldest === undefined) break;
    created.delete(oldest);
  }
}

/** The session this tab created, or `undefined` for one it didn't. */
export function sessionCreatedHere(sessionId: string): { firstMessage: string | null } | undefined {
  return created.get(sessionId);
}

/** Forgets every created session; for tests. */
export function resetSessionCreations(): void {
  created.clear();
}
