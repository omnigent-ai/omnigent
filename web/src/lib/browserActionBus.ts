/**
 * Tiny fan-out bus for `browser.action_request` SSE events, bridging the store
 * (`handleSessionEvent`) to the relay hook (`useBrowserAgentRelay`). Its own
 * module so neither imports the other (avoids a cycle). Set-based so Strict-Mode
 * double-mounts dedupe; no-op when nothing is registered (plain-browser renderers).
 */
import type { BrowserActionRequestEvent } from "./events";

/**
 * `conversationId` is the conversation whose STREAM delivered the action — the
 * session the relay must claim/dispatch/post against. The event payload carries
 * no conversation id, and with background streams the delivering conversation
 * may not be the one the relay was mounted for, so it has to ride alongside.
 */
export type BrowserActionListener = (
  event: BrowserActionRequestEvent,
  conversationId: string | null,
) => void;

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
export function emitBrowserActionRequest(
  event: BrowserActionRequestEvent,
  conversationId: string | null,
): void {
  for (const listener of listeners) {
    try {
      listener(event, conversationId);
    } catch (err) {
      console.warn("[browser-relay] action listener threw:", err);
    }
  }
}

/** How a browser action this window ran ended, as the relay posts it back. */
export interface BrowserActionOutcome {
  ok: boolean;
  error?: string;
  data?: Record<string, unknown>;
}

export type BrowserActionSettledListener = (
  event: BrowserActionRequestEvent,
  conversationId: string,
  outcome: BrowserActionOutcome,
) => void;

const settledListeners = new Set<BrowserActionSettledListener>();

/**
 * Subscribe to the actions this window ran, once each has its result. Only the window
 * that claimed an action runs it, so other windows never hear of its outcome.
 */
export function onBrowserActionSettled(listener: BrowserActionSettledListener): () => void {
  settledListeners.add(listener);
  return () => {
    settledListeners.delete(listener);
  };
}

/** Report an action's outcome to this window's listeners; a throwing one can't stop the others. */
export function emitBrowserActionSettled(
  event: BrowserActionRequestEvent,
  conversationId: string,
  outcome: BrowserActionOutcome,
): void {
  for (const listener of settledListeners) {
    try {
      listener(event, conversationId, outcome);
    } catch (err) {
      console.warn("[browser-relay] settled listener threw:", err);
    }
  }
}
