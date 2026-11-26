// Click handling for external links in chat and file previews. On the desktop,
// an opted-in click routes into the conversation's embedded browser: AppShell
// listens via `onInAppLinkOpen` to surface a Browser soft tab, and if the view
// refuses the link, it reopens externally with a toast.

import type { MouseEvent } from "react";
import { showToast } from "@/components/ui/toast";
import { AGENT_BROWSER_TAB_ID, browserViewId } from "./browserTabs";
import { readOpenLinksInApp } from "./linkOpenPreferences";
import { isNativeShell, supportsBrowser, type BrowserHostOptions } from "./nativeBridge";
import { fetchSessionHostId } from "./fetchSessionHostId";
import { previewHostOptions, withPreviewHostRetry } from "./previewHostOptions";
import { randomUUID } from "./randomUUID";
import { readSessionWorkspaceState } from "./sessionWorkspaceState";

type OpenOrNavigate = (
  conversationId: string,
  url: string,
  bounds: undefined,
  opts: BrowserHostOptions & { agent: boolean },
) => Promise<{ ok: boolean; error?: string }>;

interface BrowserBridge {
  browserOpenOrNavigate: OpenOrNavigate;
  browserClose?: (viewId: string) => Promise<{ ok: boolean }>;
  browserHasView?: (
    viewId: string,
  ) => Promise<{ exists: boolean; url?: string; pendingUrl?: string }>;
}

const listeners = new Set<(conversationId: string, tabId: string) => void>();
const pendingLinks = new Set<string>();

/** Subscribe to links the embedded browser accepted; returns an unsubscribe. */
export function onInAppLinkOpen(
  listener: (conversationId: string, tabId: string) => void,
): () => void {
  listeners.add(listener);
  return () => void listeners.delete(listener);
}

async function openInApp(conversationId: string, url: string): Promise<void> {
  const bridge = (window as unknown as { omnigentDesktop: BrowserBridge }).omnigentDesktop;
  let error: string | undefined;
  let newViewId: string | undefined;
  try {
    // The reserved agent view may navigate independently of the user's tabs.
    const tabs = (readSessionWorkspaceState(conversationId).openBrowsers ?? []).filter(
      (tabId) => tabId !== AGENT_BROWSER_TAB_ID,
    );
    if (bridge.browserHasView && tabs.length > 0) {
      const probes = await Promise.allSettled(
        tabs.map(async (tabId) => {
          const view = await bridge.browserHasView!(browserViewId(conversationId, tabId));
          const currentUrl = view.url && view.url !== "about:blank" ? view.url : view.pendingUrl;
          return view.exists && currentUrl && parseHttpUrl(currentUrl)?.href === url ? tabId : null;
        }),
      );
      const stillOpen = readSessionWorkspaceState(conversationId).openBrowsers ?? [];
      const match = probes.find(
        (probe) => probe.status === "fulfilled" && probe.value && stillOpen.includes(probe.value),
      );
      if (match?.status === "fulfilled" && match.value) {
        for (const listener of listeners) listener(conversationId, match.value);
        return;
      }
    }
    const tabId = randomUUID();
    newViewId = browserViewId(conversationId, tabId);
    const hostId = await fetchSessionHostId(conversationId);
    const options = await previewHostOptions(hostId, url);
    // Chat and file content can come from an agent, so their links use the
    // agent policy even when a user clicks them.
    const viewId = newViewId;
    const result = await withPreviewHostRetry(conversationId, hostId, (retryOptions) =>
      bridge.browserOpenOrNavigate(viewId, url, undefined, {
        agent: true,
        ...options,
        ...retryOptions,
      }),
    );
    if (result?.ok) {
      newViewId = undefined;
      for (const listener of listeners) listener(conversationId, tabId);
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
  if (newViewId) {
    try {
      await bridge.browserClose?.(newViewId);
    } catch (err) {
      console.warn("[openLinkInApp] browserClose failed:", err);
    }
  }
}

/** Resolve `href` against the page, or null unless it is an http(s) URL — so a
 *  link from untrusted data can't carry a `javascript:` or `data:` scheme. */
export function parseHttpUrl(href: string): URL | null {
  let url: URL;
  try {
    url = new URL(href, window.location.href);
  } catch {
    return null;
  }
  return url.protocol === "http:" || url.protocol === "https:" ? url : null;
}

/** Open `href` where links open: the in-app browser when the user opted in, else a new window. */
export function openLink(conversationId: string | undefined, href: string): void {
  if (!routeLink(conversationId, href)) window.open(href, "_blank", "noopener,noreferrer");
}

/**
 * Hand `href` to the in-app browser when the user opted in, else to the desktop shell, whose
 * window-open policy opens it and answers null by design. False in a plain browser.
 */
function routeLink(conversationId: string | undefined, href: string): boolean {
  if (maybeOpenLinkInApp(conversationId, href)) return true;
  if (!isNativeShell()) return false;
  window.open(href, "_blank", "noopener,noreferrer");
  return true;
}

/**
 * How opening a link went: handed to the in-app browser or the desktop shell, which open it on
 * their own; opened in a new browser tab; or refused by the browser, e.g. by its pop-up blocker.
 */
export type LinkOpenOutcome = "routed" | "opened" | "blocked";

/**
 * Open `href` as {@link openLink} does, saying how it went, for a caller opening several that
 * must report the ones that didn't open. Call it during the click, so a refused pop-up is
 * reported; most browsers allow one per click. It opens a blank tab first and leads it to
 * `href`, because a `noopener` window.open answers null even when it opens. A link that isn't
 * http(s) isn't opened, and reads as blocked.
 */
export function openLinkWithOutcome(
  conversationId: string | undefined,
  href: string,
): LinkOpenOutcome {
  const url = parseHttpUrl(href);
  if (!url) return "blocked";
  if (routeLink(conversationId, url.href)) return "routed";
  const tab = window.open("about:blank", "_blank");
  if (!tab) return "blocked";
  try {
    tab.opener = null;
    const link = tab.document.createElement("a");
    link.href = url.href;
    link.target = "_self";
    link.rel = "noopener noreferrer";
    tab.document.body.append(link);
    link.click();
    return "opened";
  } catch {
    // Don't leave an empty tab behind when it can't be led to the link.
    tab.close();
    return "blocked";
  }
}

/**
 * Open an http(s) `href` in a matching or new in-app browser tab when the user
 * opted in. Returns true when routed (the caller cancels the default click).
 */
export function maybeOpenLinkInApp(conversationId: string | undefined, href: string): boolean {
  if (!readOpenLinksInApp()) return false;
  return openLinkInSidebar(conversationId, href);
}

/** Whether {@link openLinkInSidebar} can open `href` here: an http(s) link in a session's chat, on the desktop. */
export function canOpenLinkInSidebar(conversationId: string | undefined, href: string): boolean {
  return !!conversationId && supportsBrowser() && parseHttpUrl(href) !== null;
}

/**
 * Open an http(s) `href` in a matching or new in-app browser tab in the session's sidebar,
 * whatever the link-opening preference, as a link menu's Open in Sidebar asks. False when it can't.
 */
export function openLinkInSidebar(conversationId: string | undefined, href: string): boolean {
  const url = parseHttpUrl(href);
  if (!conversationId || !supportsBrowser() || !url) return false;
  const key = JSON.stringify([conversationId, url.href]);
  if (!pendingLinks.has(key)) {
    pendingLinks.add(key);
    void openInApp(conversationId, url.href).finally(() => pendingLinks.delete(key));
  }
  return true;
}

/**
 * Follows an external link (`target="_blank"`) on a plain click, with a
 * same-tab fallback where popup creation is withheld. In a normal browser tab
 * the link still opens a new tab, but in an embedding host pane (or under a
 * popup blocker) a bare `_blank` click is silently swallowed — nothing opens
 * and nothing navigates — so a click must fall back to navigating in place.
 * Modified clicks keep their native semantics outside the in-app browser.
 * In a native shell the default path is the
 * shell's window-open policy (external browser / system handlers) — except
 * when the user opted in to the in-app browser: plain and Cmd/Ctrl clicks open
 * matching or new in-app tabs, while Shift-click opens externally. The web
 * popup fallback never runs natively: the shell's policy reports `null` from
 * window.open regardless, so it would navigate twice.
 */
export function followLinkWithPopupFallback(
  // React's or the DOM's: a menu row passes the native click, so the menu still sees it unhandled.
  event: Pick<
    MouseEvent,
    "defaultPrevented" | "button" | "shiftKey" | "altKey" | "metaKey" | "ctrlKey" | "preventDefault"
  >,
  href: string,
  conversationId: string | undefined,
): void {
  if (event.defaultPrevented || event.button !== 0) return;
  if (event.shiftKey || event.altKey) return;
  if (isNativeShell()) {
    if (maybeOpenLinkInApp(conversationId, href)) event.preventDefault();
    return;
  }
  if (event.metaKey || event.ctrlKey) return;
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
