/**
 * Tiny fan-out bus for `browser.action_request` SSE events.
 *
 * The parsed `BrowserActionRequestEvent` arrives on the active conversation's
 * stream and is seen by `handleSessionEvent` (store side). The embedded-browser
 * relay (`useBrowserAgentRelay`) is a React hook that lives outside the store,
 * so it registers a listener here and the store forwards every browser action
 * to it. Kept in its own module (not the store, not the hook) so neither has to
 * import the other — avoids an import cycle.
 *
 * Set-based registry: registering the same listener twice (React Strict Mode's
 * double-mount) is deduped, and each `onBrowserActionRequest` returns its own
 * unsubscribe. No-op when nothing is registered (plain-browser renderers never
 * mount the relay, so the events are simply dropped).
 */
import type { BrowserActionRequestEvent } from "./events";

export type BrowserActionListener = (event: BrowserActionRequestEvent) => void;

const listeners = new Set<BrowserActionListener>();

/**
 * Subscribe to browser action requests. Returns an unsubscribe function.
 * Register unconditionally — the returned cleanup removes exactly this
 * listener.
 */
export function onBrowserActionRequest(listener: BrowserActionListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Fan a parsed browser action request out to every registered listener. Called
 * by the store's `handleSessionEvent`. A throwing listener must not stop the
 * others (or the event pump), so each call is isolated.
 */
export function emitBrowserActionRequest(event: BrowserActionRequestEvent): void {
  for (const listener of listeners) {
    try {
      listener(event);
    } catch (err) {
      console.warn("[browser-relay] action listener threw:", err);
    }
  }
}
