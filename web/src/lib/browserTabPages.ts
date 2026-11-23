// Each open browser tab's last page, kept in the per-session workspace store.
// The shell destroys its native views on sign-out, a session-expiry reconnect
// and quit; a tab that outlives its view retains the address for manual navigation.

import { mainBrowserTabs } from "@/components/BrowserPane/agentTools/tabStore";
import { parseBrowserViewId } from "./browserTabs";
import { getCurrentUserId, resolveIdentity, subscribeIdentity } from "./identity";
import {
  isPersistableBrowserUrl,
  readSessionWorkspaceState,
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
  onBrowserViewClosed?: (callback: (view: { conversationId: string }) => void) => () => void;
}

/** A tab's page as shown to the user who visited it. */
export interface BrowserTabPage {
  url: string;
  title?: string;
}

/** A view's saved page for `userId` (default: the signed-in user), or null if another account visited it. */
export function readSavedBrowserPage(
  viewId: string,
  userId: string | null = getCurrentUserId(),
): BrowserTabPage | null {
  if (!userId) return null;
  const { conversationId, tabId } = parseBrowserViewId(viewId);
  const saved = readSessionWorkspaceState(conversationId).browserPages?.[tabId];
  if (saved?.owner !== userId) return null;
  return saved.title === undefined ? { url: saved.url } : { url: saved.url, title: saved.title };
}

/** Save a view's page for the signed-in user while its tab is open; other pages are ignored. */
export function saveBrowserPage(viewId: string, url: string | undefined, title?: string): void {
  const owner = getCurrentUserId();
  if (!owner) return;
  const { conversationId, tabId } = parseBrowserViewId(viewId);
  const state = readSessionWorkspaceState(conversationId);
  const openTabs = mainBrowserTabs(conversationId)?.tabs ?? state.openBrowsers ?? [];
  if (!openTabs.includes(tabId)) return;
  const saved = state.browserPages?.[tabId];
  // An in-page navigation or redirect without a title keeps the page's known title.
  const knownTitle = saved?.owner === owner && saved.url === url ? saved.title : undefined;
  const page = sanitizeSavedBrowserPage({ url, title: title || knownTitle, owner });
  if (!page) return;
  if (saved?.owner === owner && saved.url === page.url && saved.title === page.title) return;
  writeSessionWorkspaceState(conversationId, {
    browserPages: { ...state.browserPages, [tabId]: page },
  });
}

/** Save every view's navigations, background tabs and agent navigations included; returns the unsubscribe. */
export function startSavingBrowserPages(): () => void {
  if (typeof window === "undefined") return () => {};
  const bridge = (window as unknown as { omnigentDesktop?: BrowserPagesBridge }).omnigentDesktop;
  if (!bridge?.onBrowserUrlChanged) return () => {};
  let active = true;
  // Keep unowned events in memory only, until this app's identity resolves.
  const pending = new Map<string, BrowserUrlChange>();
  const savePending = () => {
    if (!active || !getCurrentUserId()) return;
    for (const change of pending.values()) {
      saveBrowserPage(change.conversationId, change.url, change.title);
    }
    pending.clear();
  };
  const unsubscribeIdentity = subscribeIdentity(savePending);
  void resolveIdentity()
    .then(savePending)
    .catch(() => {});
  const unsubscribe = bridge.onBrowserUrlChanged((change) => {
    if (!active || !isPersistableBrowserUrl(change.url)) return;
    if (!getCurrentUserId()) {
      const previous = pending.get(change.conversationId);
      pending.set(change.conversationId, {
        ...change,
        title: change.title || (previous?.url === change.url ? previous.title : undefined),
      });
      return;
    }
    pending.delete(change.conversationId);
    saveBrowserPage(change.conversationId, change.url, change.title);
  });
  const unsubscribeClosed = bridge.onBrowserViewClosed?.(({ conversationId }) => {
    pending.delete(conversationId);
  });
  return () => {
    active = false;
    pending.clear();
    unsubscribeIdentity();
    unsubscribe();
    unsubscribeClosed?.();
  };
}
