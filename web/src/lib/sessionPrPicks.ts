// The pull request the user picked in each session's Pull Requests panel. It
// lives for the page's lifetime so switching sessions or rail tabs keeps it.

import { useSyncExternalStore } from "react";

const picks = new Map<string, string>();
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function notifyListeners(): void {
  for (const listener of listeners) listener();
}

/** The PR URL the user picked for a session, if any. */
export function useSessionPrPick(sessionId: string): string | undefined {
  return useSyncExternalStore(
    subscribe,
    () => picks.get(sessionId),
    () => undefined,
  );
}

/** Remember the PR picked for a session; `undefined` forgets it. */
export function setSessionPrPick(sessionId: string, url: string | undefined): void {
  if (picks.get(sessionId) === url) return;
  if (url) picks.set(sessionId, url);
  else picks.delete(sessionId);
  notifyListeners();
}

/** Forget every session's pick — for isolated tests. */
export function clearSessionPrPicks(): void {
  picks.clear();
  notifyListeners();
}
