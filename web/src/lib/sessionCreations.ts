// Sessions this tab created, with the first message each was created with. A page that
// starts work in a new session (a gadget's New session) checks here that the session it
// lands on is the one it created, not one the viewer navigated to instead, and which of
// its requests that first message carried. Each creation gets a sequence number, so a page
// can tell a session created after it began waiting from one created earlier.
//
// A create is an attempt: `beginSessionCreation` numbers it before its request goes out,
// and it ends either as a created session (`noteSessionCreatedHere` with that number, once
// the session took its first message) or as a failure (`noteSessionCreationFailed`). A page
// can bind its work to the attempt it saw begin, so another create, or a failed one, is
// never taken for it.
type CreatedListener = (
  sessionId: string,
  firstMessage: string | null,
  seq: number,
  attempt: number | null,
) => void;
const created = new Map<string, { firstMessage: string | null; seq: number }>();
const listeners = new Set<CreatedListener>();
const attemptListeners = new Set<(attempt: number, outcome: "begun" | "failed") => void>();
let lastSeq = 0;
let lastAttempt = 0;
// Enough for any session a page in this tab is still waiting on.
const MAX_REMEMBERED = 50;

/** Numbers a create this tab is about to send, and tells the attempt listeners it began. */
export function beginSessionCreation(): number {
  const attempt = ++lastAttempt;
  for (const listener of [...attemptListeners]) listener(attempt, "begun");
  return attempt;
}

/** Records that the create numbered `attempt` made no session that took its first message. */
export function noteSessionCreationFailed(attempt: number): void {
  for (const listener of [...attemptListeners]) listener(attempt, "failed");
}

/** Calls `listener` as creates begin and fail. Returns an unsubscribe. */
export function onSessionCreationAttempt(
  listener: (attempt: number, outcome: "begun" | "failed") => void,
): () => void {
  attemptListeners.add(listener);
  return () => attemptListeners.delete(listener);
}

/**
 * Records that this tab created `sessionId`, with its first message when it had one, as the
 * create numbered `attempt` when it was begun with {@link beginSessionCreation}.
 */
export function noteSessionCreatedHere(
  sessionId: string,
  firstMessage: string | null,
  attempt: number | null = null,
): void {
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
    for (const listener of [...listeners]) {
      listener(sessionId, record.firstMessage, record.seq, attempt);
    }
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
export function onSessionCreatedHere(listener: CreatedListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Forgets every created session; for tests. */
export function resetSessionCreations(): void {
  created.clear();
  lastSeq = 0;
  lastAttempt = 0;
}
