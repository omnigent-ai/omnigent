import { useSyncExternalStore } from "react";

const pending = new Map<string, string>();
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((listener) => listener());

/** Queue text for the session's composer, including while it is unmounted. */
export function requestComposerInsertion(sessionId: string, text: string) {
  pending.set(sessionId, text);
  notify();
}

export function consumeComposerInsertion(sessionId: string) {
  pending.delete(sessionId);
  notify();
}

export function useComposerInsertion(sessionId: string | null) {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    () => (sessionId ? pending.get(sessionId) : undefined),
    () => undefined,
  );
}
