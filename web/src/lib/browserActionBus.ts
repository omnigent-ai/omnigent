/**
 * Tiny fan-out bus for `browser.action_request` SSE events, bridging the store
 * (`handleSessionEvent`) to the relay hook (`useBrowserAgentRelay`). Its own
 * module so neither imports the other (avoids a cycle). Set-based so Strict-Mode
 * double-mounts dedupe; no-op when nothing is registered (plain-browser renderers).
 */
import type { BrowserActionRequestEvent } from "./events";

export type BrowserActionListener = (event: BrowserActionRequestEvent) => void;

const listeners = new Set<BrowserActionListener>();

/** Subscribe to browser action requests; returns an unsubscribe. */
export function onBrowserActionRequest(listener: BrowserActionListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Fan a browser action request out to every listener; a throwing listener is
 *  isolated so it can't stop the others or the event pump. */
export function emitBrowserActionRequest(event: BrowserActionRequestEvent): void {
  for (const listener of listeners) {
    try {
      listener(event);
    } catch (err) {
      console.warn("[browser-relay] action listener threw:", err);
    }
  }
}
