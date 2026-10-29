// Remembers each open browser tab's last page in the per-session workspace
// store, so tabs reopen where they were after a relaunch. Only the page is kept;
// cookies and sign-ins live in the shell's persistent browser profile.

import { parseBrowserViewId } from "./browserTabs";
import {
  type BrowserTabPage,
  readSessionWorkspaceState,
  sanitizeBrowserTabPage,
  writeSessionWorkspaceState,
} from "./sessionWorkspaceState";

interface BrowserUrlChange {
  conversationId: string;
  url: string;
  title?: string;
}

interface BrowserTabPagesBridge {
  onBrowserUrlChanged?: (callback: (page: BrowserUrlChange) => void) => () => void;
}

/** The saved page for a browser view, or null when none was recorded. */
export function readBrowserTabPage(viewId: string): BrowserTabPage | null {
  const { conversationId, tabId } = parseBrowserViewId(viewId);
  return readSessionWorkspaceState(conversationId).browserTabPages?.[tabId] ?? null;
}

/** Save a view's page while its tab is open; other pages are ignored. */
export function recordBrowserTabPage(viewId: string, url: string, title?: string): void {
  const page = sanitizeBrowserTabPage({ url, title });
  if (!page) return;
  const { conversationId, tabId } = parseBrowserViewId(viewId);
  const state = readSessionWorkspaceState(conversationId);
  if (!state.openBrowsers?.includes(tabId)) return;
  const saved = state.browserTabPages?.[tabId];
  if (saved?.url === page.url && saved.title === page.title) return;
  writeSessionWorkspaceState(conversationId, {
    browserTabPages: { ...state.browserTabPages, [tabId]: page },
  });
}

/**
 * Record every browser view's navigations, including background sessions and
 * tabs whose pane is not mounted. No-op on a shell without the URL event.
 */
export function startBrowserTabPagePersistence(): () => void {
  if (typeof window === "undefined") return () => {};
  const bridge = (window as unknown as { omnigentDesktop?: BrowserTabPagesBridge }).omnigentDesktop;
  const unsubscribe = bridge?.onBrowserUrlChanged?.((change) => {
    recordBrowserTabPage(change.conversationId, change.url, change.title);
  });
  return () => unsubscribe?.();
}
