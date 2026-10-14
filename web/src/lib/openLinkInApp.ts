// Click handling for external links in chat and file previews. On the desktop,
// an opted-in click routes into the conversation's embedded browser: AppShell
// listens via `onInAppLinkOpen` to surface a Browser soft tab, and if the view
// refuses the link, it reopens externally with a toast.

import type { MouseEvent } from "react";
import { showToast } from "@/components/ui/toast";
import { readOpenLinksInApp } from "./linkOpenPreferences";
import { isNativeShell, supportsBrowser } from "./nativeBridge";

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
    // Chat and workspace-file links are agent- or repo-authored and the agent can
    // read the view, so they get the agent's navigation policy (no loopback/private
    // hosts, even via redirect).
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

/**
 * Follows an external link (`target="_blank"`) on a plain click, with a
 * same-tab fallback where popup creation is withheld. In a normal browser tab
 * the link still opens a new tab, but in an embedding host pane (or under a
 * popup blocker) a bare `_blank` click is silently swallowed — nothing opens
 * and nothing navigates — so a click must fall back to navigating in place.
 * Modified clicks (cmd/ctrl/shift/alt, non-primary buttons) keep their native
 * open-in-new-tab / menu semantics. In a native shell the default path is the
 * shell's window-open policy (external browser / system handlers) — except
 * when the user opted in to the in-app browser, where a plain click on a web
 * link routes to the conversation's embedded browser pane instead. The web
 * popup fallback never runs natively: the shell's policy reports `null` from
 * window.open regardless, so it would navigate twice.
 */
export function followLinkWithPopupFallback(
  event: MouseEvent<HTMLAnchorElement>,
  href: string,
  conversationId: string | undefined,
): void {
  if (event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  if (isNativeShell()) {
    if (maybeOpenLinkInApp(conversationId, href)) event.preventDefault();
    return;
  }
  if (!/^(?:https?:)?\/\//i.test(href)) return;
  event.preventDefault();
  const popup = window.open("about:blank", "_blank");
  if (popup) {
    popup.opener = null;
  }
  const link = (popup?.document ?? document).createElement("a");
  link.href = href;
  link.target = "_self";
  link.rel = "noopener noreferrer";
  link.click();
}
