/**
 * Claude Code `/reload-skills` and `/reload-plugins` runs reported by a conversation's
 * live stream, delivered to that conversation's composer so it can rescan the `/`
 * menu's skills. A run while no composer listens (e.g. the Terminal view is open)
 * waits until one subscribes.
 */

const RELOAD_COMMAND_NAMES: ReadonlySet<string> = new Set(["reload-skills", "reload-plugins"]);

const listenersByConversation = new Map<string, Set<() => void>>();
const undeliveredRuns = new Set<string>();

/** Whether a surfaced Claude Code command is one that reloads skills or plugins. */
export function isReloadCommand(kind: string, name: string): boolean {
  return kind === "command" && RELOAD_COMMAND_NAMES.has(name);
}

/** Record that a reload command ran in *conversationId*. */
export function noteReloadCommandRun(conversationId: string): void {
  const listeners = listenersByConversation.get(conversationId);
  if (listeners === undefined || listeners.size === 0) {
    undeliveredRuns.add(conversationId);
    return;
  }
  for (const listener of listeners) listener();
}

/**
 * Call *listener* for each reload run in *conversationId*, starting with one that
 * arrived while nothing listened. Returns the unsubscribe function.
 */
export function subscribeReloadCommandRuns(
  conversationId: string,
  listener: () => void,
): () => void {
  let listeners = listenersByConversation.get(conversationId);
  if (listeners === undefined) {
    listeners = new Set();
    listenersByConversation.set(conversationId, listeners);
  }
  listeners.add(listener);
  if (undeliveredRuns.delete(conversationId)) listener();
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && listenersByConversation.get(conversationId) === listeners)
      listenersByConversation.delete(conversationId);
  };
}

/** Forget undelivered runs; for test isolation. */
export function clearUndeliveredReloadCommandRuns(): void {
  undeliveredRuns.clear();
}
