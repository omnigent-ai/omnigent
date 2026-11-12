// What the tab rail's close (hooks/useBrowserTabs.ts) also does while the
// agent tools keep the tabs in main (tabStore.ts): keep a tab an agent or link navigated
// after the close began, and re-read tabs changed elsewhere. Main closes the
// page in whichever window has it, and moves agents off the closed tab.
// Imports nothing from the local tab store, so that store can import it.
import { useEffect } from "react";
import { loadTabs, mainBrowserTabs, onBrowserTabsChanged } from "./tabStore";

/** One rail close, begun before its native close is requested. */
export interface RailClose {
  /** Inside the change that drops the tab: whether to keep it instead,
   *  because it was navigated since the close began. */
  superseded: (current: unknown) => boolean;
  /** For main's close: the tab's navigation count when the close began, so
   *  main keeps the page if an agent or link navigated it since. */
  expectedNavs?: { tabId: string; count: number };
}

const navigationsOf = (record: unknown, tabId: string): number =>
  (record as { navs?: Record<string, number> } | null)?.navs?.[tabId] ?? 0;

export function startRailClose(conversationId: string, tabId: string): RailClose {
  const before = mainBrowserTabs(conversationId);
  return {
    superseded: (current) =>
      before !== null && navigationsOf(current, tabId) !== navigationsOf(before, tabId),
    ...(before === null ? {} : { expectedNavs: { tabId, count: navigationsOf(before, tabId) } }),
  };
}

/** Load the session's tabs into main (so its pages stay single across
 *  windows), and call `reread` when they change, here or in another window. */
export function useBrowserTabsChanged(conversationId: string, reread: () => void): void {
  useEffect(() => {
    if (mainBrowserTabs(conversationId)) {
      void loadTabs(conversationId)
        .then(() => reread())
        .catch(() => {});
    }
    return onBrowserTabsChanged((changed) => {
      if (changed === conversationId) reread();
    });
    // `reread` reads the latest state itself; resubscribing per render isn't needed.
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- See above.
  }, [conversationId]);
}
