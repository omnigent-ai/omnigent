import type { UiOpenFileEvent } from "./events";

type Listener = (event: UiOpenFileEvent, conversationId: string | null) => void;
const listeners = new Set<Listener>();

/** Transient requests: no stored state to replay after a session switch. */
export function onUiOpenFile(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function emitUiOpenFile(event: UiOpenFileEvent, conversationId: string | null): void {
  for (const listener of listeners) {
    try {
      listener(event, conversationId);
    } catch (error) {
      console.warn("[ui-open-file] listener failed:", error);
    }
  }
}
