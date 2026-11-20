// Each open browser tab's last page, kept in the per-session workspace store.
// The shell destroys its native views on sign-out, a session-expiry reconnect
// and quit, so a tab that outlives its view reopens this page instead.

import { parseBrowserViewId } from "./browserTabs";
import {
  readSessionWorkspaceState,
  type SavedBrowserPage,
  sanitizeSavedBrowserPage,
  writeSessionWorkspaceState,
} from "./sessionWorkspaceState";

interface BrowserUrlChange {
  conversationId: string;
  url: string;
  title?: string;
}

interface BrowserPagesBridge {
  onBrowserUrlChanged?: (callback: (change: BrowserUrlChange) => void) => () => void;
}

/** The saved page of a browser view's tab, or null when none was recorded. */
export function readSavedBrowserPage(viewId: string): SavedBrowserPage | null {
  const { conversationId, tabId } = parseBrowserViewId(viewId);
  return readSessionWorkspaceState(conversationId).browserPages?.[tabId] ?? null;
}

/** Save a view's page while its tab is open; other pages and closed tabs are ignored. */
export function saveBrowserPage(viewId: string, url: string | undefined, title?: string): void {
  const page = sanitizeSavedBrowserPage({ url, title });
  if (!page) return;
  const { conversationId, tabId } = parseBrowserViewId(viewId);
  const state = readSessionWorkspaceState(conversationId);
  if (!state.openBrowsers?.includes(tabId)) return;
  const saved = state.browserPages?.[tabId];
  if (saved?.url === page.url && saved.title === page.title) return;
  writeSessionWorkspaceState(conversationId, {
    browserPages: { ...state.browserPages, [tabId]: page },
  });
}

/** Save every view's navigations, background tabs and agent navigations included; returns the unsubscribe. */
export function startSavingBrowserPages(): () => void {
  if (typeof window === "undefined") return () => {};
  const bridge = (window as unknown as { omnigentDesktop?: BrowserPagesBridge }).omnigentDesktop;
  const unsubscribe = bridge?.onBrowserUrlChanged?.((change) => {
    saveBrowserPage(change.conversationId, change.url, change.title);
  });
  return () => unsubscribe?.();
}
