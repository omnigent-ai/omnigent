// Route an opted-in chat link click into the conversation's embedded browser
// (desktop only). AppShell listens via `onInAppLinkOpen` to surface a Browser
// soft tab; if the view refuses the link, it reopens externally with a toast.

import { showToast } from "@/components/ui/toast";
import { readOpenLinksInApp } from "./linkOpenPreferences";
import { supportsBrowser } from "./nativeBridge";

type OpenOrNavigate = (
  conversationId: string,
  url: string,
  bounds: undefined,
  opts: { agent: boolean },
) => Promise<{ ok: boolean; error?: string }>;

const listeners = new Set<(conversationId: string) => void>();

/** Subscribe to links the embedded browser accepted; returns an unsubscribe. */
export function onInAppLinkOpen(listener: (conversationId: string) => void): () => void {
  listeners.add(listener);
  return () => void listeners.delete(listener);
}

async function openInApp(conversationId: string, url: string): Promise<void> {
  const bridge = (
    window as unknown as { omnigentDesktop: { browserOpenOrNavigate: OpenOrNavigate } }
  ).omnigentDesktop;
  let error: string | undefined;
  try {
    // Chat links are model-authored and the agent can read the view, so they get
    // the agent's navigation policy (no loopback/private hosts, even via redirect).
    const result = await bridge.browserOpenOrNavigate(conversationId, url, undefined, {
      agent: true,
    });
    if (result?.ok) {
      for (const listener of listeners) listener(conversationId);
      return;
    }
    error = result?.error;
  } catch (err) {
    console.warn("[openLinkInApp] browserOpenOrNavigate failed:", err);
    error = err instanceof Error ? err.message : String(err);
  }
  // Runs after the click's user gesture has expired; safe only because the
  // desktop shell's window-open handler sends it to the OS browser anyway.
  window.open(url, "_blank", "noopener,noreferrer");
  showToast(
    `Couldn't open this link in the in-app browser (${error?.trim() || "unknown error"}). ` +
      "It opened in your default browser instead.",
  );
}

/**
 * Open an http(s) `href` in the conversation's embedded browser when the user
 * opted in. Returns true when routed (the caller cancels the default click).
 */
export function maybeOpenLinkInApp(conversationId: string | undefined, href: string): boolean {
  if (!conversationId || !supportsBrowser() || !readOpenLinksInApp()) return false;
  let url: URL;
  try {
    url = new URL(href, window.location.href);
  } catch {
    return false;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return false;
  void openInApp(conversationId, url.toString());
  return true;
}
