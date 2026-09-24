// Route a chat link into the conversation's embedded in-app browser pane.
//
// Desktop-only, preference-gated: `maybeOpenLinkInApp` routes a plain click on
// a web link to the conversation's embedded WebContentsView (the Browser
// workspace tab) via the Electron bridge, instead of the shell's default
// window-open policy handing it to the external OS browser. The pane itself
// is not mounted here — AppShell subscribes via `onInAppLinkOpen` and
// surfaces the Browser tab, the same way it auto-surfaces on an agent-issued
// `browser_navigate`.

import { readOpenLinksInApp } from "./linkOpenPreferences";
import { supportsBrowser } from "./nativeBridge";

/** Subset of `window.omnigentDesktop` this module calls (typed locally, like
 *  the other browser-bridge consumers; the method is the browser-capability
 *  marker `supportsBrowser()` probes). */
interface InAppBrowserBridge {
  browserOpenOrNavigate?: (
    conversationId: string,
    url: string,
  ) => Promise<{ ok: boolean; created?: boolean; error?: string }>;
}

type InAppLinkListener = (conversationId: string) => void;

const listeners = new Set<InAppLinkListener>();

/** Subscribe to chat links routed in-app (AppShell surfaces the Browser tab);
 *  returns an unsubscribe. */
export function onInAppLinkOpen(listener: InAppLinkListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Open `href` in `conversationId`'s embedded browser view when the user opted
 * in, and notify listeners so the pane surfaces. Returns true when the link
 * was routed in-app (the caller must then cancel the default `_blank`
 * navigation), false when the default path should keep handling the click —
 * off-preference, off-desktop, no conversation to scope the view to, or a
 * non-web scheme (which stays with the shell's system-handler policy).
 */
export function maybeOpenLinkInApp(conversationId: string | undefined, href: string): boolean {
  if (!conversationId || !supportsBrowser() || !readOpenLinksInApp()) return false;
  let resolved;
  try {
    resolved = new URL(href, window.location.href);
  } catch {
    return false;
  }
  if (resolved.protocol !== "http:" && resolved.protocol !== "https:") return false;
  const bridge = (window as unknown as { omnigentDesktop?: InAppBrowserBridge }).omnigentDesktop;
  if (typeof bridge?.browserOpenOrNavigate !== "function") return false;
  bridge.browserOpenOrNavigate(conversationId, resolved.toString()).catch((err) => {
    console.warn("[openLinkInApp] browserOpenOrNavigate failed:", err);
  });
  for (const listener of listeners) {
    try {
      listener(conversationId);
    } catch (err) {
      console.warn("[openLinkInApp] listener threw:", err);
    }
  }
  return true;
}
