// Route a chat link into the conversation's embedded in-app browser pane.
//
// Desktop-only, preference-gated: `maybeOpenLinkInApp` routes a plain click on
// a web link to the conversation's embedded WebContentsView (the Browser
// workspace tab) via the Electron bridge, instead of the shell's default
// window-open policy handing it to the external OS browser. The pane itself
// is not mounted here — AppShell subscribes via `onInAppLinkOpen` and
// surfaces the Browser tab once the view accepted the URL, the same way it
// auto-surfaces on an agent-issued `browser_navigate`. A view that refuses
// the URL (the per-window view cap, a failed load) or a bridge call that
// throws must not swallow the click: the link falls back to the shell's
// default path (the external browser) and a toast says why.

import { showToast } from "@/components/ui/toast";
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

/** Subscribe to chat links the embedded browser accepted (AppShell surfaces
 *  the Browser tab); returns an unsubscribe. */
export function onInAppLinkOpen(listener: InAppLinkListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function notifyListeners(conversationId: string): void {
  for (const listener of listeners) {
    try {
      listener(conversationId);
    } catch (err) {
      console.warn("[openLinkInApp] listener threw:", err);
    }
  }
}

/**
 * The in-app browser could not take the link: hand it to the shell's default
 * window-open path (the external browser) so the click still lands somewhere,
 * and tell the user why it left the app.
 */
function fallBackToExternalBrowser(url: string, reason: string | undefined): void {
  window.open(url, "_blank", "noopener,noreferrer");
  showToast(
    `Couldn't open this link in the in-app browser (${reason?.trim() || "unknown error"}). ` +
      "It opened in your default browser instead.",
  );
}

/**
 * Open `href` in `conversationId`'s embedded browser view when the user opted
 * in. Returns true when the link was routed in-app (the caller must then
 * cancel the default `_blank` navigation), false when the default path should
 * keep handling the click — off-preference, off-desktop, no conversation to
 * scope the view to, or a non-web scheme (which stays with the shell's
 * system-handler policy). Listeners are notified only once the view accepted
 * the URL; a refusal or failure reopens the link externally with a toast.
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
  const url = resolved.toString();
  let request: Promise<{ ok: boolean; error?: string } | undefined>;
  try {
    request = Promise.resolve(bridge.browserOpenOrNavigate(conversationId, url));
  } catch (err) {
    request = Promise.reject(err);
  }
  void request.then(
    (result) => {
      if (result?.ok) notifyListeners(conversationId);
      else fallBackToExternalBrowser(url, result?.error);
    },
    (err: unknown) => {
      console.warn("[openLinkInApp] browserOpenOrNavigate failed:", err);
      fallBackToExternalBrowser(url, err instanceof Error ? err.message : String(err));
    },
  );
  return true;
}
