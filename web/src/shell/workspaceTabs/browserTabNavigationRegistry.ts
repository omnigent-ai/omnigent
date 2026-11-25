// One session's browser tabs and their navigation owners: an owner per
// browser-view tab while it is open, the shell's navigation events routed to the
// owner of their view, and each new owner restored from what its view shows now.

import { onBrowserPageControl, supersedeBrowserPageSubmission } from "@/lib/browserPageControls";
import { browserViewId } from "@/lib/browserTabs";

import {
  type BrowserNavigationBridge,
  type BrowserTabHost,
  BrowserTabNavigation,
  type BrowserViewProbe,
} from "./browserTabNavigation";
import type { TabId } from "./workspacePlace";
import { isUnknownTab, type WorkspaceTab } from "./workspaceTab";
import type { WorkspaceTabsStore } from "./workspaceTabsStore";

/** What a shell offers for page history: the owners' bridge, plus probing and events. */
export interface BrowserNavigationShell extends BrowserNavigationBridge {
  /** What a view shows now; null when the shell can't say, which leaves the tab as saved. */
  probe: (viewKey: string) => Promise<BrowserViewProbe | null>;
  onUrlChanged: (listener: (event: { viewKey: string; url: string }) => void) => () => void;
}

interface OwnedTab {
  owner: BrowserTabNavigation;
  stopPageControls: () => void;
}

/**
 * Keep `store`'s browser tabs' owners until the returned detach, such as when the
 * session or the sidebar design changes; detaching ends every pending landing.
 */
export function attachBrowserTabNavigation(
  store: WorkspaceTabsStore,
  conversationId: string,
  shell: BrowserNavigationShell,
): () => void {
  const owned = new Map<TabId, OwnedTab>();

  const hostFor = (tabId: TabId, viewKey: string): BrowserTabHost => ({
    isSelected: () => store.isSelected(tabId),
    history: () => store.history.state,
    apply: (change) => {
      store.applyBrowserTransition(tabId, change);
    },
    waitingChanged: () => store.noteBrowserWaiting(),
    landingStarted: () => supersedeBrowserPageSubmission(viewKey),
  });

  const release = (tabId: TabId) => {
    const entry = owned.get(tabId);
    if (!entry) return;
    owned.delete(tabId);
    entry.stopPageControls();
    entry.owner.dispose();
  };

  const own = (tab: WorkspaceTab, viewKey: string) => {
    const owner = new BrowserTabNavigation(tab.id, viewKey, shell, hostFor(tab.id, viewKey));
    owned.set(tab.id, {
      owner,
      stopPageControls: onBrowserPageControl(viewKey, (event) => {
        if (event.kind === "takeover") owner.userActed(event.submission);
        else owner.submissionAccepted(event.submission, event.url);
      }),
    });
    const token = owner.probeToken();
    const savedUrl = tab.place.kind === "web" ? tab.place.url : undefined;
    void shell.probe(viewKey).then(
      (view) => {
        if (view && owned.get(tab.id)?.owner === owner) owner.restore(view, token, savedUrl);
      },
      () => {},
    );
  };

  const sync = () => {
    const open = new Set<TabId>();
    for (const tab of store.tabs()) {
      if (isUnknownTab(tab) || tab.binding?.kind !== "browser-view") continue;
      open.add(tab.id);
      const viewKey = browserViewId(conversationId, tab.binding.viewKey);
      const existing = owned.get(tab.id);
      if (existing?.owner.viewKey === viewKey) continue;
      release(tab.id);
      own(tab, viewKey);
    }
    for (const tabId of [...owned.keys()]) if (!open.has(tabId)) release(tabId);
  };

  const ownerOfView = (viewKey: string) => {
    for (const { owner } of owned.values()) if (owner.viewKey === viewKey) return owner;
    return undefined;
  };

  const unsubscribers = [
    shell.onUrlChanged((event) => ownerOfView(event.viewKey)?.onUrlChanged(event)),
    store.subscribe(sync),
  ];
  store.setBrowserNavigation((tabId) => owned.get(tabId)?.owner);
  sync();

  return () => {
    for (const unsubscribe of unsubscribers) unsubscribe();
    store.setBrowserNavigation(null);
    for (const tabId of [...owned.keys()]) release(tabId);
  };
}
