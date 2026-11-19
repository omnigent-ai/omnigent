// Whether each session's event stream is open, for consumers of its
// transient events: the stream never replays, so they catch up from REST
// when it reopens and poll only while it's down.

type Listener = (conversationId: string, reopened: boolean) => void;

const openStreams = new Map<string, number>();
const listeners = new Set<Listener>();

/** Record that a session's stream opened; ``reopened`` after a drop or reconnect. */
export function noteSessionStreamOpen(conversationId: string, reopened: boolean): void {
  openStreams.set(conversationId, (openStreams.get(conversationId) ?? 0) + 1);
  for (const listener of listeners) listener(conversationId, reopened);
}

/** Record that one of a session's stream connections closed. */
export function noteSessionStreamClosed(conversationId: string): void {
  const count = (openStreams.get(conversationId) ?? 0) - 1;
  if (count > 0) openStreams.set(conversationId, count);
  else openStreams.delete(conversationId);
  for (const listener of listeners) listener(conversationId, false);
}

export function isSessionStreamOpen(conversationId: string): boolean {
  return (openStreams.get(conversationId) ?? 0) > 0;
}

/** Subscribe to opens and closes; returns the unsubscribe function. */
export function subscribeSessionStreams(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
