// Tracks sessions created by this tab and acceptance of their first input.
type CreatedListener = (
  sessionId: string,
  firstMessage: string | null,
  seq: number,
  attempt: number | null,
) => void;
const created = new Map<string, { firstMessage: string | null; seq: number }>();
const listeners = new Set<CreatedListener>();
const attemptListeners = new Set<(attempt: number, outcome: "begun" | "failed") => void>();
// Created sessions whose first message the server accepted, with that message's text.
const accepted = new Map<string, string | null>();
const acceptedListeners = new Set<(sessionId: string, text: string | null) => void>();
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
    accepted.delete(oldest);
  }
  if (known === undefined) {
    for (const listener of [...listeners]) {
      listener(sessionId, record.firstMessage, record.seq, attempt);
    }
  }
}

/**
 * Records that the server accepted the first message of `sessionId`, a session this tab
 * created, with `text`, the message it accepted: a create that carried it, or the first send
 * to it. That may differ from the text the session was created with, as when a first send
 * failed and the viewer sent something else. Later calls do nothing.
 */
export function noteFirstInputAccepted(sessionId: string, text: string | null): void {
  if (!created.has(sessionId) || accepted.has(sessionId)) return;
  accepted.set(sessionId, text);
  for (const listener of [...acceptedListeners]) listener(sessionId, text);
}

/** Whether the server accepted the first message of `sessionId`, a session this tab created. */
export function firstInputAccepted(sessionId: string): boolean {
  return accepted.has(sessionId);
}

/** The first message the server accepted for `sessionId`, or `undefined` before it did. */
export function acceptedFirstInput(sessionId: string): string | null | undefined {
  return accepted.has(sessionId) ? accepted.get(sessionId) : undefined;
}

/**
 * Calls `listener` as created sessions' first messages are accepted, with the text accepted.
 * Returns an unsubscribe.
 */
export function onFirstInputAccepted(
  listener: (sessionId: string, text: string | null) => void,
): () => void {
  acceptedListeners.add(listener);
  return () => acceptedListeners.delete(listener);
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
  accepted.clear();
  lastSeq = 0;
  lastAttempt = 0;
}
